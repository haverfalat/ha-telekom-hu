"""Async Telekom web client with rotating, persisted refresh cookies."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from http.cookiejar import Cookie
from ssl import SSLContext
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://www.telekom.hu"
COOKIE_NAMES = frozenset(
    {
        "mt-auth-token",
        "mt-op-session-token",
        "mt-op-refresh-token",
        "mt-context-id",
        "mt-context-id-hash",
        "mt-device-id",
        "iPlanetDirectoryPro",
    }
)


class TelekomApiError(Exception):
    """Temporary service or response error; never contains credentials."""


class TelekomAuthError(TelekomApiError):
    """The account requires interactive sign-in again."""


class TelekomAccountError(TelekomApiError):
    """The selected account does not contain the requested subscription."""


def normalize_phone(value: str) -> str:
    value = re.sub(r"[\s()-]", "", value)
    if value.startswith("0036"):
        value = "+" + value[2:]
    elif value.startswith("06"):
        value = "+36" + value[2:]
    elif value.startswith("36"):
        value = "+" + value
    if not re.fullmatch(r"\+36\d{9}", value):
        raise ValueError("Invalid Hungarian phone number")
    return value


def validate_session(value: str | dict) -> dict:
    """Accept a helper export, dropping unrelated cookies and fields."""
    try:
        data = json.loads(value) if isinstance(value, str) else value
        cookies = data["cookies"]
        if not isinstance(cookies, list) or len(cookies) > 200:
            raise ValueError
        result = []
        for c in cookies:
            domain = c.get("domain", "").lstrip(".").lower()
            if c.get("name") not in COOKIE_NAMES or not (
                domain == "telekom.hu" or domain.endswith(".telekom.hu")
            ):
                continue
            if not isinstance(c.get("value"), str) or len(c["value"]) > 65536:
                raise ValueError
            expires = c.get("expires", -1)
            if not isinstance(expires, (int, float)):
                raise ValueError
            result.append(
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c["domain"],
                    "path": c.get("path", "/"),
                    "secure": True,
                    "expires": expires,
                    "httpOnly": True,
                }
            )
        if not any(c["name"] == "mt-op-refresh-token" and c["value"] for c in result):
            raise ValueError
        party_id = data.get("party_id")
        if party_id is not None and not isinstance(party_id, str):
            raise ValueError
        return {"cookies": result, "party_id": party_id}
    except (ValueError, TypeError, KeyError, AttributeError) as err:
        raise ValueError(
            "Invalid session export; a Telekom refresh cookie is required"
        ) from err


def _assets(value: Any) -> list[dict]:
    found = {}

    def visit(node):
        if isinstance(node, dict):
            asset_id = node.get("manageableAssetId")
            if isinstance(asset_id, str) and asset_id:
                found.setdefault(asset_id, node)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return list(found.values())


def _number(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and number >= 0 else None
    except InvalidOperation:
        return None


def _gb(value: Any, unit: Any) -> float | None:
    number = _number(value)
    # Match Telekom's web client: MB / 1024 = GB.
    factors = {
        "B": Decimal(1024) ** -3,
        "KB": Decimal(1024) ** -2,
        "MB": Decimal(1024) ** -1,
        "GB": Decimal(1),
        "TB": Decimal(1024),
    }
    factor = factors.get(str(unit).upper())
    return float(number * factor) if number is not None and factor is not None else None


def _timestamp(value: Any) -> str | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).isoformat() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError):
        return None


def parse_balances(payload: Any) -> dict[str, dict]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get("dataBalance"), list
    ):
        raise TelekomApiError("Unrecognised data-balance response")
    result = {}
    for target in ("ON_NET", "ROAMING_EU"):
        matches = [
            b
            for b in payload["dataBalance"]
            if isinstance(b, dict) and b.get("usageTarget") == target
        ]
        # Ambiguous buckets must not silently be added together or overwritten.
        if len(matches) > 1:
            raise TelekomApiError("Multiple balance records for one usage target")
        if not matches:
            continue
        b = matches[0]
        remaining = _gb(b.get("balanceValue"), b.get("balanceUnit"))
        total = _gb(b.get("limitValue"), b.get("limitUnit"))
        unlimited = b.get("limitValue") == "Korlátlan"
        if unlimited:
            remaining = total = None
        valid = remaining is not None and total is not None and remaining <= total
        result[target] = {
            "remaining": remaining,
            "total": total,
            "used": round(total - remaining, 8) if valid else None,
            "remaining_percent": round(remaining / total * 100, 2)
            if valid and total > 0
            else None,
            "expires_at": _timestamp(b.get("expirationDate")),
            "unlimited": unlimited,
        }
    return result


class TelekomApi:
    """Own one HTTP session; serialize updates so rotated cookies are never lost."""

    def __init__(
        self,
        session: dict,
        *,
        on_session: Callable[[dict], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ssl_context: SSLContext | None = None,
    ):
        self._state = validate_session(session)
        self._on_session = on_session
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            verify=ssl_context or True,
            base_url=BASE_URL,
            timeout=30,
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json", "Referer": BASE_URL + "/fiokom/"},
        )
        for c in self._state["cookies"]:
            expires = int(c["expires"]) if c["expires"] > 0 else None
            domain = c["domain"]
            self._http.cookies.jar.set_cookie(
                Cookie(
                    0,
                    c["name"],
                    c["value"],
                    None,
                    False,
                    domain,
                    domain.startswith("."),
                    domain.startswith("."),
                    c["path"],
                    True,
                    True,
                    expires,
                    expires is None,
                    None,
                    None,
                    {"HttpOnly": True},
                    False,
                )
            )

    def export_session(self) -> dict:
        cookies = [
            {
                "name": c.name,
                "value": c.value,
                "domain": c.domain,
                "path": c.path,
                "secure": True,
                "expires": c.expires or -1,
                "httpOnly": True,
            }
            for c in self._http.cookies.jar
            if c.name in COOKIE_NAMES
        ]
        return {"cookies": cookies, "party_id": self._state.get("party_id")}

    async def _persist(self) -> None:
        current = self.export_session()
        if current != self._state:
            self._state = deepcopy(current)
            if self._on_session:
                # Persist before the next network request, including refresh rotation.
                await self._on_session(deepcopy(current))

    def _cookie(self, name: str) -> Cookie | None:
        return next(
            (
                c
                for c in self._http.cookies.jar
                if c.name == name and (c.expires is None or c.expires > time.time())
            ),
            None,
        )

    def _needs_refresh(self) -> bool:
        cookie = self._cookie("mt-op-session-token")
        if cookie is None:
            return True
        if cookie.expires is not None and cookie.expires < time.time() + 60:
            return True
        try:
            part = cookie.value.split(".")[1]
            exp = json.loads(
                base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            ).get("exp")
            return isinstance(exp, (int, float)) and exp < time.time() + 60
        except (ValueError, IndexError, TypeError, AttributeError):
            return False

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {}
        if party := self._state.get("party_id"):
            headers["x-selected-party-id"] = party
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as err:
            raise TelekomApiError("Cannot connect to Telekom") from err
        await self._persist()
        return response

    async def _refresh(self) -> None:
        if self._cookie("mt-op-refresh-token") is None:
            raise TelekomAuthError("Telekom sign-in is required")
        r = await self._send(
            "POST", "/belepes/api/oidc/refresh-session", params={"client_id": "onecare"}
        )
        if r.status_code in (400, 401, 403) or r.is_redirect:
            raise TelekomAuthError("Telekom sign-in is required")
        if not r.is_success:
            raise TelekomApiError(f"Telekom refresh failed (HTTP {r.status_code})")
        if self._cookie("mt-op-session-token") is None:
            raise TelekomAuthError("Telekom did not renew the session")

    async def _get(self, path: str) -> Any:
        if self._needs_refresh():
            await self._refresh()
        r = await self._send("GET", "/fiokom/api/" + path)
        if r.status_code == 401:
            await self._refresh()
            r = await self._send("GET", "/fiokom/api/" + path)
        if r.status_code == 401 or r.is_redirect:
            raise TelekomAuthError("Telekom sign-in is required")
        if not r.is_success:
            raise TelekomApiError(f"Telekom request failed (HTTP {r.status_code})")
        try:
            return r.json()
        except ValueError as err:
            raise TelekomApiError("Unrecognised Telekom response") from err

    async def async_fetch(self, phone: str) -> dict:
        """Fetch one selected subscription, refreshing first if needed."""
        phone = normalize_phone(phone)
        async with self._lock:
            if not self._state.get("party_id"):
                accounts = await self._get("customer-selection")
                entities = (
                    accounts.get("relatedEntities", [])
                    if isinstance(accounts, dict)
                    else []
                )
                if len(entities) != 1 or not entities[0].get("partyId"):
                    raise TelekomAccountError(
                        "Select your account in the browser before exporting the session"
                    )
                self._state["party_id"] = entities[0]["partyId"]
                # Party changes must be persisted as well as cookie changes.
                if self._on_session:
                    await self._on_session(self.export_session())
            assets = _assets(await self._get("dashboard/groups"))
            matches = []
            for asset in assets:
                try:
                    if normalize_phone(str(asset.get("phoneNumber", ""))) == phone:
                        matches.append(asset)
                except ValueError:
                    continue
            if len(matches) != 1:
                raise TelekomAccountError("Subscription not found in this account")
            product = quote(matches[0]["manageableAssetId"], safe="")
            payload = await self._get("balance/data-balances/" + product)
            refresh_cookie = self._cookie("mt-op-refresh-token")
            return {
                "allowances": parse_balances(payload),
                "refresh_expires_at": datetime.fromtimestamp(
                    refresh_cookie.expires, UTC
                ).isoformat()
                if refresh_cookie and refresh_cookie.expires
                else None,
            }

    async def async_close(self) -> None:
        await self._http.aclose()

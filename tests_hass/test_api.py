import json
import time

import httpx
import pytest

from custom_components.telekom_hu.api import (
    TelekomApi,
    TelekomApiError,
    TelekomAuthError,
    parse_balances,
    validate_session,
)

PHONE = "+36301234567"


def session(*, expired=False):
    return {
        "party_id": "test-party",
        "cookies": [
            {
                "name": "mt-op-session-token",
                "value": "old-access",
                "domain": "www.telekom.hu",
                "path": "/",
                "expires": time.time() - 10 if expired else time.time() + 1800,
            },
            {
                "name": "mt-op-refresh-token",
                "value": "old-refresh",
                "domain": "www.telekom.hu",
                "path": "/",
                "expires": time.time() + 2505600,
            },
        ],
    }


def account_response(request):
    assert request.headers["x-selected-party-id"] == "test-party"
    if request.url.path.endswith("dashboard/groups"):
        return httpx.Response(
            200,
            json={
                "owned": [
                    {
                        "products": [
                            {"manageableAssetId": "test/id=", "phoneNumber": PHONE}
                        ]
                    }
                ]
            },
        )
    assert request.url.raw_path.endswith(b"balance/data-balances/test%2Fid%3D")
    return httpx.Response(
        200,
        json={
            "dataBalance": [
                {
                    "usageTarget": "ON_NET",
                    "balanceValue": "512",
                    "balanceUnit": "MB",
                    "limitValue": "2",
                    "limitUnit": "GB",
                    "expirationDate": "2026-11-10T22:59:59.000Z",
                },
                {
                    "usageTarget": "ROAMING_EU",
                    "balanceValue": "1",
                    "balanceUnit": "GB",
                    "limitValue": "2",
                    "limitUnit": "GB",
                },
            ]
        },
    )


@pytest.mark.asyncio
async def test_expired_access_refresh_rotation_survives_restart():
    saved = []
    calls = []

    async def persist(state):
        saved.append(state)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("refresh-session"):
            assert request.method == "POST"
            assert request.url.params["client_id"] == "onecare"
            assert "old-refresh" in request.headers["cookie"]
            return httpx.Response(
                200,
                headers=[
                    (
                        "set-cookie",
                        "mt-op-session-token=new-access; Path=/; Secure; HttpOnly; Max-Age=1800",
                    ),
                    (
                        "set-cookie",
                        "mt-op-refresh-token=new-refresh; Path=/; Secure; HttpOnly; Max-Age=2505600",
                    ),
                ],
            )
        assert "new-access" in request.headers["cookie"]
        assert saved, "Rotated credentials must be saved before subsequent API reads"
        return account_response(request)

    api = TelekomApi(
        session(expired=True),
        transport=httpx.MockTransport(handler),
        on_session=persist,
    )
    try:
        result = await api.async_fetch(PHONE)
        assert result["allowances"]["ON_NET"]["remaining"] == 0.5
        assert result["allowances"]["ON_NET"]["remaining_percent"] == 25
        assert result["allowances"]["ROAMING_EU"]["remaining"] == 1
        assert sum(p.endswith("refresh-session") for p in calls) == 1
    finally:
        await api.async_close()
    exported = saved[-1]
    assert any(
        c["name"] == "mt-op-refresh-token" and c["value"] == "new-refresh"
        for c in exported["cookies"]
    )

    def resumed_handler(request):
        assert "new-refresh" in request.headers["cookie"]
        assert "new-access" in request.headers["cookie"]
        return account_response(request)

    resumed = TelekomApi(exported, transport=httpx.MockTransport(resumed_handler))
    try:
        assert (await resumed.async_fetch(PHONE))["allowances"]["ON_NET"]["used"] == 1.5
    finally:
        await resumed.async_close()


@pytest.mark.asyncio
async def test_401_refreshes_and_retries_once():
    calls = {"refresh": 0, "dashboard": 0}

    def handler(request):
        if request.url.path.endswith("refresh-session"):
            calls["refresh"] += 1
            return httpx.Response(
                200,
                headers={
                    "set-cookie": "mt-op-session-token=new; Path=/; Secure; Max-Age=1800"
                },
            )
        if request.url.path.endswith("dashboard/groups"):
            calls["dashboard"] += 1
            if calls["dashboard"] == 1:
                return httpx.Response(401)
        return account_response(request)

    api = TelekomApi(session(), transport=httpx.MockTransport(handler))
    try:
        await api.async_fetch(PHONE)
        assert calls == {"refresh": 1, "dashboard": 2}
    finally:
        await api.async_close()


@pytest.mark.asyncio
async def test_revoked_refresh_requires_reauthentication():
    api = TelekomApi(
        session(expired=True),
        transport=httpx.MockTransport(lambda r: httpx.Response(401)),
    )
    try:
        with pytest.raises(TelekomAuthError):
            await api.async_fetch(PHONE)
    finally:
        await api.async_close()


@pytest.mark.asyncio
async def test_server_outage_does_not_invalidate_authentication():
    api = TelekomApi(
        session(expired=True),
        transport=httpx.MockTransport(lambda r: httpx.Response(503)),
    )
    try:
        with pytest.raises(TelekomApiError) as err:
            await api.async_fetch(PHONE)
        assert not isinstance(err.value, TelekomAuthError)
    finally:
        await api.async_close()


def test_session_import_filters_domains_and_analytics():
    data = session()
    data["cookies"] += [
        {"name": "_ga", "value": "analytics", "domain": ".telekom.hu"},
        {"name": "mt-op-refresh-token", "value": "evil", "domain": "eviltelekom.hu"},
    ]
    clean = validate_session(json.dumps(data))
    assert len(clean["cookies"]) == 2
    with pytest.raises(ValueError):
        validate_session('{"cookies": []}')


def test_unlimited_and_missing_are_not_zero():
    data = parse_balances(
        {"dataBalance": [{"usageTarget": "ON_NET", "limitValue": "Korlátlan"}]}
    )
    assert data["ON_NET"]["unlimited"] is True
    assert data["ON_NET"]["remaining"] is None
    assert data["ON_NET"]["remaining_percent"] is None
    assert "ROAMING_EU" not in data
    with pytest.raises(TelekomApiError):
        parse_balances(
            {"dataBalance": [{"usageTarget": "ON_NET"}, {"usageTarget": "ON_NET"}]}
        )

"""Read-only Telekom web API client using an explicitly supplied login session.

Login can be completed manually in a real browser, including SMS/CAPTCHA. After
session export this client uses httpx only; it does not launch a browser.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from .client import TelekomError, AuthenticationRequired, normalize_phone
from .usage import Quantity, SchemaError

WEB_BASE = 'https://www.telekom.hu'


def _telekom_cookie(cookie: dict) -> bool:
    domain = cookie.get('domain', '').lstrip('.').lower()
    return domain == 'telekom.hu' or domain.endswith('.telekom.hu')


def save_web_session(path: str | Path, cookies: list[dict], *, party_id: str | None = None):
    """Persist only Telekom cookies, never unrelated browser cookies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.web-session-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'cookies': [c for c in cookies if _telekom_cookie(c)],
                       'party_id': party_id}, f)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def party_id_from_selected_entity(value: str | None) -> str | None:
    """Match the web app's double-base64 selected-party representation."""
    if not value:
        return None
    try:
        entity = json.loads(base64.b64decode(value, validate=True))
        return base64.b64decode(entity['entityId'], validate=True).decode()
    except (ValueError, KeyError, TypeError, UnicodeError):
        return None


def assets_from_dashboard(payload) -> list[dict]:
    """Retain server identifiers unchanged, including base64-encoded identifiers."""
    result = {}
    def visit(value):
        if isinstance(value, dict):
            asset_id = value.get('manageableAssetId')
            if isinstance(asset_id, str) and asset_id:
                result.setdefault(asset_id, value)
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    return list(result.values())


def parse_data_balances(payload: dict) -> list[dict]:
    """Preserve domestic and EU balances independently; never add them together."""
    if not isinstance(payload, dict) or not isinstance(payload.get('dataBalance'), list):
        raise SchemaError('Expected web response with a dataBalance array')
    result = []
    for item in payload['dataBalance']:
        if not isinstance(item, dict):
            raise SchemaError('dataBalance entries must be objects')
        remaining = (Quantity.parse({'value': item['balanceValue'], 'unit': item['balanceUnit']})
                     if item.get('balanceValue') is not None and item.get('balanceUnit') else None)
        total = (Quantity.parse({'value': item['limitValue'], 'unit': item['limitUnit']})
                 if item.get('limitValue') is not None and item.get('limitUnit') else None)
        result.append({
            'usage_target': item.get('usageTarget'),
            'remaining': {'value': remaining.value, 'unit': remaining.unit} if remaining else None,
            'total': {'value': total.value, 'unit': total.unit} if total else None,
            'unlimited': item.get('limitValue') == 'Korlátlan',
            'expires_at': item.get('expirationDate'),
        })
    return result


class TelekomWebClient:
    def __init__(self, cookies: list[dict], *, party_id: str | None = None,
                 user_agent: str | None = None, transport=None):
        self.http = httpx.Client(base_url=WEB_BASE, timeout=30, follow_redirects=False,
                                 transport=transport, headers={
                                     'Accept': 'application/json',
                                     'Referer': WEB_BASE + '/fiokom/',
                                     **({'User-Agent': user_agent} if user_agent else {}),
                                     **({'x-selected-party-id': party_id} if party_id else {}),
                                 })
        # CookieJar preserves Domain/Path/Secure and prevents sending Secure
        # cookies over plaintext. The API base is fixed to Telekom HTTPS.
        from http.cookiejar import Cookie
        for c in cookies:
            if not _telekom_cookie(c):
                continue
            domain = c['domain']
            expires = c.get('expires')
            expires = int(expires) if expires and expires > 0 else None
            self.http.cookies.jar.set_cookie(Cookie(
                version=0, name=c['name'], value=c['value'], port=None,
                port_specified=False, domain=domain,
                domain_specified=domain.startswith('.'),
                domain_initial_dot=domain.startswith('.'), path=c.get('path', '/'),
                path_specified=True, secure=c.get('secure', True), expires=expires,
                discard=expires is None, comment=None, comment_url=None,
                rest={'HttpOnly': c.get('httpOnly', False)}, rfc2109=False,
            ))

    @classmethod
    def load(cls, path: str | Path, **kwargs):
        data = json.loads(Path(path).read_text())
        return cls(data['cookies'], party_id=data.get('party_id'), **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.http.close()

    def _get(self, path: str, **kwargs):
        response = self.http.get('/fiokom/api/' + path, **kwargs)
        if response.status_code == 401 or response.is_redirect:
            raise AuthenticationRequired(response.status_code, 'WEB_LOGIN_REQUIRED')
        try:
            result = response.json()
        except ValueError:
            raise TelekomError(response.status_code, 'NON_JSON_WEB_RESPONSE') from None
        if response.is_error:
            code = result.get('errorCode', result.get('code', 'WEB_API_ERROR')) if isinstance(result, dict) else 'WEB_API_ERROR'
            raise TelekomError(response.status_code, code)
        return result

    def profile(self):
        return self._get('profile')

    def dashboard(self):
        self.ensure_party()
        return self._get('dashboard/groups')

    def accounts(self):
        return self._get('customer-selection')

    def ensure_party(self):
        if self.http.headers.get('x-selected-party-id'):
            return
        entities = self.accounts().get('relatedEntities', [])
        if len(entities) != 1 or not entities[0].get('partyId'):
            raise ValueError('Select an account first: supply its party_id when constructing the web client')
        self.http.headers['x-selected-party-id'] = entities[0]['partyId']

    def assets(self):
        return assets_from_dashboard(self.dashboard())

    def data_balances(self, product_id: str):
        if not product_id:
            raise ValueError('A product identifier from your account is required')
        self.ensure_party()
        return self._get('balance/data-balances/' + quote(product_id, safe=''))

    def usage_for_phone(self, phone: str, *, raw: bool = False):
        phone = normalize_phone(phone)
        matches = []
        for asset in self.assets():
            try:
                if normalize_phone(str(asset.get('phoneNumber', ''))) == phone:
                    matches.append(asset)
            except ValueError:
                continue
        if len(matches) != 1:
            raise ValueError('The signed-in account must contain exactly one subscription matching that phone')
        balances = self.data_balances(matches[0]['manageableAssetId'])
        return balances if raw else {'phone': phone, 'allowances': parse_data_balances(balances)}

    def prepaid_balance(self, product_id: str):
        return self._get('balance/prepaid-main-balance/' + quote(product_id, safe=''))

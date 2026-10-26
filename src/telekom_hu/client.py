"""Experimental HTTP client for the recovered Hungarian Android OneApp contract.

Nonce creation is live-verified. SMS/token/account calls require further live
verification: this app version requests Play Integrity during login.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from .usage import parse_consumption

BASE_URL = 'https://oneapp.telekom.hu/oneapp-bff/'
VERSION = '26.2618.91 (71028) @ 71028-28e7eb04 (A26.2618.91)'


class TelekomError(RuntimeError):
    def __init__(self, status: int, code: str, request_id: str | None = None):
        self.status, self.code, self.request_id = status, code, request_id
        super().__init__(f'Telekom HTTP {status}: {code}' +
                         (f' (request {request_id})' if request_id else ''))


class AuthenticationRequired(TelekomError):
    pass


def normalize_phone(phone: str) -> str:
    phone = re.sub(r'[\s()-]', '', phone)
    if phone.startswith('0036'):
        phone = '+' + phone[2:]
    elif phone.startswith('06'):
        phone = '+36' + phone[2:]
    elif phone.startswith('36'):
        phone = '+' + phone
    if not re.fullmatch(r'\+36\d{9}', phone):
        raise ValueError('Use a Hungarian phone number such as +36301234567')
    return phone


class TelekomClient:
    def __init__(self, *, device_id: str | None = None, authorization_key: str | None = None, transport=None):
        self.device_id = device_id or str(uuid4())
        self.session_id = str(uuid4())
        self.phone = None
        self.otp_nonce = None
        self.access_token = None
        self.refresh_token = None
        self.subject = None
        self.authorization_key = authorization_key or os.environ.get('TELEKOM_AUTH_KEY')
        self.http = httpx.Client(
            base_url=BASE_URL, timeout=30, follow_redirects=False, transport=transport,
            headers={'Accept': 'application/json', 'Accept-Language': 'hu',
                     'X-Client-Version': VERSION, 'X-Request-Session-Id': self.session_id,
                     'natco-code': 'hu', 'natco-key': 'ktmEVbICclGjFzohIInikGRDOoGpwYaB',
                     'services-used': 'GMS', 'build-type': 'release',
                     'User-Agent': 'okhttp/4.12.0', 'App-Widget': 'false',
                     'country': 'HU', 'channel': 'OneApp', 'appVersion': 'A26.2618.91',
                     'isPreviewBuild': 'false', 'deviceId': self.device_id,
                     'language': 'hu', 'environment': 'production',
                     'osName': 'Android', 'osVersion': '35', 'OS': 'Android/15'},
        )

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _request(self, method, path, *, authenticated=False, **kwargs):
        if authenticated and not self.access_token:
            raise AuthenticationRequired(401, 'LOGIN_REQUIRED')
        headers = {'X-Request-Tracking-Id': str(uuid4()),
                   'Authorization': f'Bearer {self.access_token}' if self.access_token else
                                    self.authorization_key or self.device_id}
        response = self.http.request(method, path, headers=headers, **kwargs)
        try:
            data = response.json()
        except ValueError:
            raise TelekomError(response.status_code, 'NON_JSON_RESPONSE') from None
        if response.is_error:
            data = data if isinstance(data, dict) else {}
            error = AuthenticationRequired if response.status_code == 401 else TelekomError
            raise error(response.status_code, data.get('code', 'UNKNOWN_ERROR'), data.get('id'))
        if not response.is_success:
            raise TelekomError(response.status_code, 'UNEXPECTED_REDIRECT')
        return data

    def create_attestation_nonce(self, token_nonce: str | None = None) -> dict:
        """Live-verified: server returns a nonce; it is NOT an Integrity token."""
        return self._request('POST', 'attestation/create-nonce', json={
            'deviceId': self.device_id, 'tokenNonce': token_nonce or str(uuid4())})

    def request_sms(self, phone: str, *, attestation_nonce: str | None = None,
                    attestation_statement: str | None = None, oauth: dict | None = None) -> dict:
        """Experimental: recovered contract, currently rejected with HTTP 417.

        Only supply a legitimate attestation statement for this app/device.
        No token forgery, CAPTCHA service, or browser is included.
        """
        phone = normalize_phone(phone)
        if bool(attestation_nonce) != bool(attestation_statement):
            raise ValueError('Supply both attestation nonce and statement together')
        body = {'serviceId': phone, 'serviceType': 'phoneNumber', 'context': 'login',
                'device': {'id': self.device_id, 'os': 'Android'},
                'enableProfilePin': False}
        if attestation_statement:
            body.update(attestationNonce=attestation_nonce, attestationStatement=attestation_statement)
        if oauth is not None:
            body['oauth'] = oauth
        data = self._request('POST', 'pin/', params={'enablePlayIntegrity': 'true'}, json=body)
        if not isinstance(data, dict) or not data.get('nonce'):
            raise TelekomError(200, 'MISSING_OTP_NONCE')
        self.phone, self.otp_nonce = phone, data['nonce']
        return data

    def verify_sms(self, code: str, *, attestation_nonce=None, attestation_statement=None,
                   oauth=None) -> dict:
        """Experimental PIN verification; live success has not been established."""
        if not self.phone or not self.otp_nonce:
            raise ValueError('Request an SMS successfully before verifying a code')
        if not re.fullmatch(r'\d{4,8}', code):
            raise ValueError('Code must contain 4–8 digits')
        if bool(attestation_nonce) != bool(attestation_statement):
            raise ValueError('Supply both attestation nonce and statement together')
        body = {'serviceId': self.phone, 'serviceType': 'phoneNumber', 'context': 'login',
                'nonce': self.otp_nonce, 'PIN': code, 'enableProfilePin': False,
                'device': {'id': self.device_id, 'os': 'Android'}}
        if attestation_statement:
            body.update(attestationNonce=attestation_nonce, attestationStatement=attestation_statement)
        if oauth is not None:
            body['oauth'] = oauth
        data = self._request('PUT', 'pin/', params={'enablePlayIntegrity': 'true'}, json=body)
        self._accept_tokens(data)
        self.otp_nonce = None
        return data

    def _accept_tokens(self, data):
        if not isinstance(data, dict) or not data.get('accessToken'):
            raise AuthenticationRequired(401, 'TOKEN_NOT_ISSUED_OR_ADDITIONAL_AUTH_REQUIRED')
        self.access_token = data['accessToken']
        self.refresh_token = data.get('refreshToken', self.refresh_token)

    def refresh(self):
        if not self.refresh_token:
            raise AuthenticationRequired(401, 'NO_REFRESH_TOKEN')
        data = self._request('POST', 'token/', json={'deviceId': self.device_id,
                             'refreshToken': self.refresh_token, 'genCenToken': False})
        self._accept_tokens(data)
        return data

    def profiles(self, subject: str):
        self.subject = subject
        return self._request('GET', 'profiles/', authenticated=True, params={
            'sub': subject, 'deviceId': self.device_id, 'hybridEnabled': 'false',
            'loyaltyEnabled': 'false', 'subscriptionServiceEnabled': 'false',
            'genCenToken': 'false', 'devicesWithEMI': 'false'})

    def dashboard(self, profile_id: str):
        return self._request('GET', f'dashboard/profile/{quote(profile_id, safe="")}',
                             authenticated=True, params={'deviceId': self.device_id,
                             'subscriptionServiceEnabled': 'false', 'devicesWithEMI': 'false',
                             'groupType': 'billingAccount', 'enableIndividualByParty': 'false'})

    def usage_raw(self, product_id: str, profile_id: str):
        return self._request('GET', f'dashboard/product/{quote(product_id, safe="")}/',
                             authenticated=True, params={'enableFreeUnit': 'true',
                             'showUnlimited': 'true', 'profileId': profile_id, 'priority': 'high'})

    def usage(self, product_id: str, profile_id: str):
        return parse_consumption(self.usage_raw(product_id, profile_id))

    def extra_data(self, profile_id: str):
        return self._request('GET', 'dashboard/extraData', authenticated=True,
                             params={'profileId': profile_id})

    def save(self, path: str | Path):
        """Atomic, owner-only local session. Never includes the SMS code."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {key: getattr(self, key) for key in ('device_id', 'phone', 'otp_nonce',
                 'access_token', 'refresh_token', 'subject')}
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.session-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @classmethod
    def load(cls, path: str | Path, **kwargs):
        data = json.loads(Path(path).read_text())
        client = cls(device_id=data['device_id'], **kwargs)
        for key in ('phone', 'otp_nonce', 'access_token', 'refresh_token', 'subject'):
            setattr(client, key, data.get(key))
        return client

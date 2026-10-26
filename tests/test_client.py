import json
import stat

import httpx
import pytest

from telekom_hu import TelekomClient, TelekomError, AuthenticationRequired, Quantity, SchemaError, parse_consumption
from telekom_hu.client import normalize_phone


def test_mock_sms_token_usage_lifecycle(tmp_path):
    """Synthetic contract test, not evidence of live SMS login."""
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/pin/'):
            body = json.loads(request.content)
            assert body['serviceId'] == '+36301234567'
            assert body['device']['id'] == 'test-device'
            if request.method == 'POST':
                assert 'PIN' not in body
                return httpx.Response(200, json={'nonce': 'otp-test'})
            assert body['nonce'] == 'otp-test'
            assert body['PIN'] == '987654'
            return httpx.Response(200, json={'accessToken': 'test-access', 'refreshToken': 'test-refresh'})
        assert request.headers['Authorization'] == 'Bearer test-access'
        assert request.url.params['profileId'] == 'test-profile'
        return httpx.Response(200, json={'consumption': {'remaining': {'value': 512, 'unit': 'MB'},
                                                        'max': {'value': 2, 'unit': 'GB'}}})
    transport = httpx.MockTransport(handler)
    session = tmp_path/'session.json'
    with TelekomClient(device_id='test-device', transport=transport) as c:
        c.request_sms('06 30 123 4567')
        c.save(session)
    assert stat.S_IMODE(session.stat().st_mode) == 0o600
    with TelekomClient.load(session, transport=transport) as c:
        c.verify_sms('987654')
        assert c.usage('test-product', 'test-profile').remaining.bytes() == 512*1024**2
        c.save(session)
    assert '987654' not in session.read_text()
    assert len({r.headers['X-Request-Tracking-Id'] for r in calls}) == 3


def test_rejected_sms_does_not_set_pending_login():
    transport = httpx.MockTransport(lambda r: httpx.Response(417, json={'code': 'EXPECTATION_FAILED'}))
    with TelekomClient(transport=transport) as c:
        with pytest.raises(TelekomError, match='EXPECTATION_FAILED'):
            c.request_sms('+36301234567')
        assert c.otp_nonce is None
        with pytest.raises(ValueError):
            c.verify_sms('123456')


def test_no_account_request_without_authentication():
    with TelekomClient(transport=httpx.MockTransport(lambda r: pytest.fail('Network call'))) as c:
        with pytest.raises(AuthenticationRequired):
            c.usage_raw('test', 'test')


def test_missing_usage_is_not_zero():
    assert parse_consumption({'consumption': None}) is None
    with pytest.raises(SchemaError):
        parse_consumption({'error': 'not logged in'})
    assert Quantity('Korlátlan', 'GB').bytes() is None
    assert Quantity('-1', 'GB').bytes() is None
    assert parse_consumption({'consumption': {'consumptionGroupType': 'VOICE',
                                             'remaining': {'value': 60, 'unit': 'min'}}}) is None


@pytest.mark.parametrize('phone', ['0036301234567', '36301234567', '06 30 123 4567', '+36 (30) 123-4567'])
def test_phone_normalization(phone):
    assert normalize_phone(phone) == '+36301234567'


def test_nonce_request_contract():
    def handler(r):
        assert json.loads(r.content) == {'deviceId': 'test-device', 'tokenNonce': 'test-token-nonce'}
        assert r.url.path == '/oneapp-bff/attestation/create-nonce'
        return httpx.Response(200, json={'nonce': 'test-nonce'})
    with TelekomClient(device_id='test-device', transport=httpx.MockTransport(handler)) as c:
        assert c.create_attestation_nonce('test-token-nonce') == {'nonce': 'test-nonce'}

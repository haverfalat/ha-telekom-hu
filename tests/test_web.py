import base64
import json
import stat

import httpx
import pytest

from telekom_hu import AuthenticationRequired
from telekom_hu.web import (TelekomWebClient, assets_from_dashboard,
                            party_id_from_selected_entity, save_web_session, parse_data_balances)


def test_web_session_replays_only_telekom_cookies(tmp_path):
    path = tmp_path/'web.json'
    cookies = [
        {'name': 'mt-op-session-token', 'value': 'synthetic-session', 'domain': 'www.telekom.hu', 'path': '/', 'secure': True},
        {'name': 'unrelated', 'value': 'synthetic-other', 'domain': 'google.com', 'path': '/'},
        {'name': 'suffix-attack', 'value': 'bad', 'domain': 'eviltelekom.hu', 'path': '/'},
    ]
    save_web_session(path, cookies, party_id='synthetic-party')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(json.loads(path.read_text())['cookies']) == 1
    def handler(request):
        assert request.headers['cookie'] == 'mt-op-session-token=synthetic-session'
        assert request.headers['x-selected-party-id'] == 'synthetic-party'
        assert request.url.raw_path == b'/fiokom/api/balance/data-balances/base64%2Fid%3D'
        return httpx.Response(200, json={'dataBalance': []})
    with TelekomWebClient.load(path, transport=httpx.MockTransport(handler)) as client:
        assert client.data_balances('base64/id=') == {'dataBalance': []}


def test_expired_web_session_reports_login_required():
    with TelekomWebClient([], transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={'authenticationUrl': 'https://www.telekom.hu/belepes'}))) as c:
        with pytest.raises(AuthenticationRequired, match='WEB_LOGIN_REQUIRED'):
            c.profile()


def test_asset_discovery_deduplicates_without_decoding():
    asset = {'manageableAssetId': 'aWQ=', 'type': 'postpaid'}
    assert assets_from_dashboard({'owned': [{'groups': [{'products': [asset]}]}],
                                  'managed': [asset]}) == [asset]


def test_selected_party_decoding():
    value = base64.b64encode(json.dumps({'entityId': base64.b64encode(b'party-id').decode()}).encode()).decode()
    assert party_id_from_selected_entity(value) == 'party-id'
    assert party_id_from_selected_entity('invalid') is None


def test_phone_lookup_and_single_account_selection():
    def handler(r):
        if r.url.path.endswith('/customer-selection'):
            return httpx.Response(200, json={'relatedEntities': [{'partyId': 'test-party'}]})
        assert r.headers['x-selected-party-id'] == 'test-party'
        if r.url.path.endswith('/dashboard/groups'):
            return httpx.Response(200, json={'owned': [{'products': [
                {'manageableAssetId': 'test-product', 'phoneNumber': '+36301234567'}]}]})
        assert r.url.path.endswith('/balance/data-balances/test-product')
        return httpx.Response(200, json={'dataBalance': [
            {'usageTarget': 'ON_NET', 'balanceValue': '4.5', 'balanceUnit': 'GB',
             'limitValue': '10', 'limitUnit': 'GB'},
            {'usageTarget': 'ROAMING_EU', 'balanceValue': '4.5', 'balanceUnit': 'GB',
             'limitValue': '10', 'limitUnit': 'GB'}]})
    with TelekomWebClient([], transport=httpx.MockTransport(handler)) as c:
        result = c.usage_for_phone('06 30 123 4567')
        assert len(result['allowances']) == 2
        assert result['allowances'][0]['remaining'] == {'value': '4.5', 'unit': 'GB'}


def test_multiple_accounts_require_selection():
    with TelekomWebClient([], transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
        'relatedEntities': [{'partyId': 'a'}, {'partyId': 'b'}]}))) as c:
        with pytest.raises(ValueError, match='Select an account'):
            c.assets()


def test_missing_balance_stays_unknown_and_unlimited_is_explicit():
    result = parse_data_balances({'dataBalance': [{'usageTarget': 'ON_NET', 'limitValue': 'Korlátlan'}]})
    assert result[0]['remaining'] is None
    assert result[0]['unlimited'] is True

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telekom_hu import account_key, session_store
from custom_components.telekom_hu.const import DOMAIN
from custom_components.telekom_hu.diagnostics import async_get_config_entry_diagnostics

from .test_api import PHONE, session

DATA = {
    "allowances": {
        "ON_NET": {
            "remaining": 4.5,
            "total": 10,
            "used": 5.5,
            "remaining_percent": 45,
            "expires_at": "2026-11-10T22:59:59+00:00",
            "unlimited": False,
        },
        "ROAMING_EU": {
            "remaining": 3.0,
            "total": 10,
            "used": 7.0,
            "remaining_percent": 30,
            "expires_at": "2026-11-10T22:59:59+00:00",
            "unlimited": False,
        },
    },
    "refresh_expires_at": "2026-11-24T10:18:11+00:00",
}


@pytest.mark.asyncio
async def test_config_flow_stores_tokens_privately(hass):
    with (
        patch(
            "custom_components.telekom_hu.config_flow.TelekomConfigFlow._validate",
            AsyncMock(return_value=session()),
        ),
        patch(
            "custom_components.telekom_hu.async_setup_entry",
            AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": PHONE, "session_json": json.dumps(session())}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"] == {"phone": PHONE}
        assert (await session_store(hass, PHONE).async_load())["cookies"]


@pytest.mark.asyncio
async def test_sensor_setup_options_and_unload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telekom test",
        data={"phone": PHONE},
        unique_id=account_key(PHONE),
    )
    entry.add_to_hass(hass)
    await session_store(hass, PHONE).async_save(session())
    with patch(
        "custom_components.telekom_hu.api.TelekomApi.async_fetch",
        AsyncMock(return_value=DATA),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        registry = er.async_get(hass)
        remaining_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{PHONE}_ON_NET_remaining"
        )
        state = hass.states.get(remaining_id)
        assert state.state == "4.5"
        assert state.attributes["unit_of_measurement"] == "GB"
        assert len(er.async_entries_for_config_entry(registry, entry.entry_id)) == 11
        assert "cookies" not in str(state.attributes)
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert PHONE not in json.dumps(diagnostics)
        assert "old-refresh" not in json.dumps(diagnostics)
        options = await hass.config_entries.options.async_init(entry.entry_id)
        assert options["type"] is FlowResultType.FORM
        await hass.config_entries.options.async_configure(
            options["flow_id"], {"update_interval": 30}
        )
        await hass.async_block_till_done()
        assert entry.options["update_interval"] == 30
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_missing_session_starts_reauth(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telekom test",
        data={"phone": PHONE},
        unique_id=account_key(PHONE),
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(
        f["handler"] == DOMAIN and f["context"]["source"] == "reauth" for f in flows
    )


@pytest.mark.asyncio
async def test_reauth_updates_existing_entry_without_duplicates(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telekom test",
        data={"phone": PHONE},
        unique_id=account_key(PHONE),
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.telekom_hu.config_flow.TelekomConfigFlow._validate",
            AsyncMock(return_value=session()),
        ),
        patch(
            "homeassistant.config_entries.ConfigEntries.async_reload",
            AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"session_json": json.dumps(session())}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        assert await session_store(hass, PHONE).async_load()

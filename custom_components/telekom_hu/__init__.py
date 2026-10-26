"""Magyar Telekom mobile-data sensors."""

from __future__ import annotations

import hashlib

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.util.ssl import SSL_ALPN_HTTP11, client_context

from .api import TelekomApi
from .const import CONF_PHONE, DOMAIN, STORAGE_VERSION
from .coordinator import TelekomCoordinator

PLATFORMS = [Platform.SENSOR]


def account_key(phone: str) -> str:
    return hashlib.sha256(phone.encode()).hexdigest()[:32]


def session_store(hass: HomeAssistant, phone: str) -> Store:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.{account_key(phone)}", private=True)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = session_store(hass, entry.data[CONF_PHONE])
    session = await store.async_load()
    if session is None:
        raise ConfigEntryAuthFailed("Sign in to Telekom and import a session")
    try:
        api = TelekomApi(
            session,
            on_session=store.async_save,
            ssl_context=client_context(alpn_protocols=SSL_ALPN_HTTP11),
        )
    except ValueError as err:
        raise ConfigEntryAuthFailed("Import a new Telekom session") from err
    coordinator = TelekomCoordinator(hass, entry, api)
    try:
        await coordinator.async_config_entry_first_refresh()
    except BaseException:
        await api.async_close()
        raise
    entry.runtime_data = coordinator
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await api.async_close()
        raise
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    async def _stop(event):
        await api.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.api.async_close()
        return True
    return False


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await session_store(hass, entry.data[CONF_PHONE]).async_remove()

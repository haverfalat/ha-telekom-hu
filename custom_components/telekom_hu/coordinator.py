"""Coordinated Telekom polling."""

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TelekomApi, TelekomApiError, TelekomAuthError
from .const import CONF_INTERVAL, CONF_PHONE, DEFAULT_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class TelekomCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: TelekomApi):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                minutes=entry.options.get(CONF_INTERVAL, DEFAULT_INTERVAL)
            ),
            always_update=False,
        )
        self.api = api
        self.phone = entry.data[CONF_PHONE]

    async def _async_update_data(self) -> dict:
        try:
            return await self.api.async_fetch(self.phone)
        except TelekomAuthError as err:
            raise ConfigEntryAuthFailed("Telekom requires a new sign-in") from err
        except TelekomApiError as err:
            raise UpdateFailed(str(err)) from err

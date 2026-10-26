"""Import a browser session and reauthenticate when Telekom revokes it."""

from typing import Any

import probatio
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util.ssl import SSL_ALPN_HTTP11, client_context

from . import account_key, session_store
from .api import (
    TelekomAccountError,
    TelekomApi,
    TelekomApiError,
    TelekomAuthError,
    normalize_phone,
    validate_session,
)
from .const import (
    CONF_INTERVAL,
    CONF_PHONE,
    CONF_SESSION_JSON,
    DEFAULT_INTERVAL,
    DOMAIN,
)


def _session_field():
    return selector.TextSelector(selector.TextSelectorConfig(multiline=True))


class TelekomConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return TelekomOptionsFlow()

    async def _validate(self, session_json: str, phone: str) -> dict:
        session = validate_session(session_json)
        api = TelekomApi(
            session, ssl_context=client_context(alpn_protocols=SSL_ALPN_HTTP11)
        )
        try:
            await api.async_fetch(phone)
            return api.export_session()
        finally:
            await api.async_close()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors = {}
        if user_input is not None:
            try:
                phone = normalize_phone(user_input[CONF_PHONE])
                session = await self._validate(user_input[CONF_SESSION_JSON], phone)
            except ValueError:
                errors["base"] = "invalid_input"
            except TelekomAuthError:
                errors["base"] = "invalid_auth"
            except TelekomAccountError:
                errors["base"] = "account_mismatch"
            except TelekomApiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(account_key(phone))
                self._abort_if_unique_id_configured()
                await session_store(self.hass, phone).async_save(session)
                return self.async_create_entry(
                    title=f"Telekom {phone}", data={CONF_PHONE: phone}
                )
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_PHONE): str,
                    probatio.Required(CONF_SESSION_JSON): _session_field(),
                }
            ),
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                session = await self._validate(
                    user_input[CONF_SESSION_JSON], entry.data[CONF_PHONE]
                )
            except ValueError:
                errors["base"] = "invalid_input"
            except TelekomAuthError:
                errors["base"] = "invalid_auth"
            except TelekomAccountError:
                errors["base"] = "account_mismatch"
            except TelekomApiError:
                errors["base"] = "cannot_connect"
            else:
                await session_store(self.hass, entry.data[CONF_PHONE]).async_save(
                    session
                )
                return self.async_update_reload_and_abort(
                    entry, data_updates={}, reload_even_if_entry_is_unchanged=True
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            data_schema=probatio.Schema(
                {probatio.Required(CONF_SESSION_JSON): _session_field()}
            ),
        )


class TelekomOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_INTERVAL, DEFAULT_INTERVAL
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=1440,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

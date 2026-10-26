"""Diagnostics without cookies, phone numbers, account IDs, or usage values."""

from .const import CONF_INTERVAL, DEFAULT_INTERVAL


async def async_get_config_entry_diagnostics(hass, entry):
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    return {
        "integration_version": "0.2.0",
        "poll_interval_minutes": entry.options.get(CONF_INTERVAL, DEFAULT_INTERVAL),
        "last_update_success": coordinator.last_update_success,
        "usage_targets": sorted(data.get("allowances", {})),
        "refresh_deadline_available": bool(data.get("refresh_expires_at")),
    }

"""Domestic and EU allowance sensors; shared quotas are never summed."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfInformation
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TelekomCoordinator

PARALLEL_UPDATES = 0
TARGETS = {"ON_NET": "Domestic", "ROAMING_EU": "EU roaming"}
FIELDS = {
    "remaining": "data remaining",
    "used": "data used",
    "total": "data allowance",
    "remaining_percent": "data remaining percentage",
    "expires_at": "allowance expiry",
}


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    # Fixed target sensors become available when their bucket appears later.
    entities = [
        TelekomSensor(coordinator, target, field)
        for target in TARGETS
        for field in FIELDS
    ]
    entities.append(TelekomSensor(coordinator, None, "refresh_expires_at"))
    async_add_entities(entities)


class TelekomSensor(CoordinatorEntity[TelekomCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, target, field):
        super().__init__(coordinator)
        self.target, self.field = target, field
        self._attr_unique_id = f"{coordinator.phone}_{target or 'session'}_{field}"
        self._attr_name = (
            f"{TARGETS[target]} {FIELDS[field]}"
            if target
            else "Session renewal deadline"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.phone)},
            name=f"Telekom {coordinator.phone}",
            manufacturer="Magyar Telekom",
            model="Mobile subscription",
            entry_type=DeviceEntryType.SERVICE,
        )
        if field in ("expires_at", "refresh_expires_at"):
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
            self._attr_icon = "mdi:calendar-clock"
        elif field == "remaining_percent":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_icon = "mdi:percent"
        else:
            self._attr_device_class = SensorDeviceClass.DATA_SIZE
            self._attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 2
            self._attr_icon = "mdi:signal-cellular-3"
        if target is None:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self):
        return super().available and (
            self.target is None
            or self.target in self.coordinator.data.get("allowances", {})
        )

    @property
    def native_value(self):
        data = self.coordinator.data
        value = (
            data.get(self.field)
            if self.target is None
            else data.get("allowances", {}).get(self.target, {}).get(self.field)
        )
        if value and self.field in ("expires_at", "refresh_expires_at"):
            return datetime.fromisoformat(value)
        return value

    @property
    def extra_state_attributes(self):
        if self.target is None:
            return {"automatic_renewal": True, "permanent_session_guaranteed": False}
        return {
            "usage_target": self.target,
            "unlimited": self.coordinator.data.get("allowances", {})
            .get(self.target, {})
            .get("unlimited", False),
        }

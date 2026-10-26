"""Mobile OneApp consumption records. Keep independent allowance buckets separate."""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class SchemaError(ValueError):
    """The server returned an unrecognised consumption schema."""


@dataclass(frozen=True)
class Quantity:
    value: str
    unit: str

    @classmethod
    def parse(cls, obj: Any) -> "Quantity | None":
        if obj is None:
            return None
        if not isinstance(obj, dict) or obj.get('value') is None or not obj.get('unit'):
            raise SchemaError('Consumption quantity requires value and unit')
        return cls(str(obj['value']), str(obj['unit']))

    def bytes(self, *, base: int = 1024) -> int | None:
        # Preserve the original unit/value too; use no conversion for unknown units.
        units = {'B': 0, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4}
        power = units.get(self.unit.upper())
        if power is None:
            return None
        try:
            value = Decimal(self.value)
        except InvalidOperation:
            return None
        if not value.is_finite() or value < 0:
            return None
        return int(value * base ** power)


@dataclass(frozen=True)
class DataAllowance:
    name: str | None
    remaining: Quantity | None
    total: Quantity | None
    used: Quantity | None
    remaining_percentage: float | None
    updated_at: str | None
    expires_at: str | None
    group: str | None

    def to_dict(self) -> dict:
        result = asdict(self)
        result['remaining_bytes_binary'] = self.remaining.bytes() if self.remaining else None
        return result


def parse_consumption(payload: dict) -> DataAllowance | None:
    """Parse the `consumption` object used by the Android dashboard.

    A missing consumption means unavailable, never zero. No sum across domestic,
    roaming, shared, or promotional quotas is attempted.
    """
    if not isinstance(payload, dict) or 'consumption' not in payload:
        raise SchemaError('Expected dashboard response with a consumption field')
    c = payload['consumption']
    if c is None:
        return None
    if not isinstance(c, dict):
        raise SchemaError('consumption must be an object or null')
    if c.get('consumptionGroupType') and str(c['consumptionGroupType']).lower() != 'data':
        return None
    return DataAllowance(
        name=c.get('name'), remaining=Quantity.parse(c.get('remaining')),
        total=Quantity.parse(c.get('max')), used=Quantity.parse(c.get('used')),
        remaining_percentage=c.get('remainingPercentage'), updated_at=c.get('updatedTime'),
        expires_at=c.get('validUntil') or c.get('expiryTime'), group=c.get('consumptionGroupType'),
    )

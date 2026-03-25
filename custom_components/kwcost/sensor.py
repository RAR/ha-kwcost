"""Sensor entities for Kilowatt Cost."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback, Event, EventStateChangedData
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_JURISDICTION,
    CONF_SCHEDULE,
    CONF_TOU_SCHEDULE,
    CONF_GRID_ENERGY_IN,
    CONF_GRID_ENERGY_OUT,
)
from .coordinator import KwcostRateCoordinator, KwcostTouCoordinator

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Shared device info for all sensors in this config entry."""
    jurisdiction = entry.data[CONF_JURISDICTION]
    schedule = entry.data[CONF_SCHEDULE]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=f"Kilowatt Cost {jurisdiction} {schedule}",
        manufacturer="kwcost.com",
        model=f"{jurisdiction} {schedule}",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://kwcost.com",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Kilowatt Cost sensors from a config entry."""
    coordinators = hass.data[DOMAIN][entry.entry_id]
    rate_coordinator: KwcostRateCoordinator = coordinators["rate"]
    tou_coordinator: KwcostTouCoordinator | None = coordinators.get("tou")

    entities: list[SensorEntity] = [
        KwcostBaseRateSensor(rate_coordinator, entry),
        KwcostEffectiveRateSensor(rate_coordinator, entry),
        KwcostScheduleNameSensor(rate_coordinator, entry),
        KwcostBaseFacilityChargeSensor(rate_coordinator, entry),
    ]

    if tou_coordinator is not None:
        entities.extend(
            [
                KwcostTouPeriodSensor(tou_coordinator, entry),
                KwcostTouSeasonSensor(tou_coordinator, entry),
            ]
        )

    # Cost tracking sensors based on user-selected energy sensors
    grid_in_entity = entry.data.get(CONF_GRID_ENERGY_IN)
    if grid_in_entity:
        entities.append(
            KwcostGridCostSensor(
                hass, rate_coordinator, entry, grid_in_entity,
                tou_coordinator=tou_coordinator,
            )
        )

    grid_out_entity = entry.data.get(CONF_GRID_ENERGY_OUT)
    if grid_out_entity and tou_coordinator is not None:
        entities.append(
            KwcostGridExportCreditSensor(
                hass, rate_coordinator, tou_coordinator, entry, grid_out_entity
            )
        )
    elif grid_out_entity:
        # No TOU — use flat rate for export credit too
        entities.append(
            KwcostGridCostSensor(
                hass, rate_coordinator, entry, grid_out_entity, is_export=True
            )
        )

    async_add_entities(entities)


class KwcostBaseRateSensor(CoordinatorEntity[KwcostRateCoordinator], SensorEntity):
    """Base energy rate per kWh (before riders/taxes)."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = f"$/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(
        self, coordinator: KwcostRateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_base_rate"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "base_rate"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        summary = (
            self.coordinator.data.get("rate", {})
            .get("effective_rate_summary", {})
        )
        return summary.get("base_rate_per_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        details = self.coordinator.data.get("rate", {}).get("details", {})
        charges = details.get("energy_charges_per_kwh", [])
        return {
            "energy_tiers": charges,
            "effective_date": details.get("effective_date"),
        }


class KwcostEffectiveRateSensor(CoordinatorEntity[KwcostRateCoordinator], SensorEntity):
    """Effective rate in cents/kWh."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "¢/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash"

    def __init__(
        self, coordinator: KwcostRateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_effective_rate"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "effective_rate"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return (
            self.coordinator.data.get("rate", {})
            .get("effective_rate_summary", {})
            .get("effective_cents_per_kwh")
        )


class KwcostScheduleNameSensor(CoordinatorEntity[KwcostRateCoordinator], SensorEntity):
    """Current rate schedule name."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:file-document-outline"

    def __init__(
        self, coordinator: KwcostRateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_schedule_name"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "schedule_name"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return (
            self.coordinator.data.get("rate", {})
            .get("details", {})
            .get("name")
        )


class KwcostBaseFacilityChargeSensor(
    CoordinatorEntity[KwcostRateCoordinator], SensorEntity
):
    """Monthly basic facilities charge."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:home-currency-usd"

    def __init__(
        self, coordinator: KwcostRateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_facility_charge"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "facility_charge"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        return (
            self.coordinator.data.get("rate", {})
            .get("details", {})
            .get("basic_facilities_charge_dollars")
        )


class KwcostTouPeriodSensor(CoordinatorEntity[KwcostTouCoordinator], SensorEntity):
    """Current TOU period (on_peak, off_peak, etc.)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:clock-outline"

    def __init__(
        self, coordinator: KwcostTouCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tou_period"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "tou_period"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("period")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "schedule": self.coordinator.data.get("schedule"),
            "season": self.coordinator.data.get("season"),
            "datetime": self.coordinator.data.get("datetime"),
        }


class KwcostTouSeasonSensor(CoordinatorEntity[KwcostTouCoordinator], SensorEntity):
    """Current TOU season (summer, winter, etc.)."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:weather-sunny"

    def __init__(
        self, coordinator: KwcostTouCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tou_season"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "tou_season"

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("season")


def _get_flat_rate(coordinator: KwcostRateCoordinator) -> float | None:
    """Get the flat base rate $/kWh from coordinator data."""
    if not coordinator.data:
        return None
    return (
        coordinator.data.get("rate", {})
        .get("effective_rate_summary", {})
        .get("base_rate_per_kwh")
    )


def _get_tou_rate(
    rate_coordinator: KwcostRateCoordinator,
    tou_coordinator: KwcostTouCoordinator,
) -> float | None:
    """Get the rate for the current TOU period."""
    if not rate_coordinator.data or not tou_coordinator.data:
        return None
    period = tou_coordinator.data.get("period")
    if not period:
        return _get_flat_rate(rate_coordinator)
    rate_data = rate_coordinator.data.get("rate", {}).get("details", {})
    energy = rate_data.get("energy_charges_per_kwh")
    # TOU schedules have a dict of period → rate
    if isinstance(energy, dict):
        return energy.get(period)
    # Flat schedule — just use the single tier rate
    if isinstance(energy, list) and energy:
        return energy[0].get("rate")
    return None


class KwcostGridCostSensor(RestoreEntity, SensorEntity):
    """Tracks cumulative cost of grid energy imported (or exported at flat rate).

    TOU-aware when a TOU coordinator is provided — uses the current period's
    rate for each energy delta.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:currency-usd"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: HomeAssistant,
        rate_coordinator: KwcostRateCoordinator,
        entry: ConfigEntry,
        source_entity: str,
        is_export: bool = False,
        tou_coordinator: KwcostTouCoordinator | None = None,
    ) -> None:
        self.hass = hass
        self._rate_coordinator = rate_coordinator
        self._tou_coordinator = tou_coordinator
        self._source_entity = source_entity
        self._is_export = is_export
        self._accumulated_cost: float = 0.0
        self._last_energy_value: float | None = None
        self._unsub: Any = None

        suffix = "grid_export_cost" if is_export else "grid_cost"
        self._attr_unique_id = f"{entry.entry_id}_{suffix}"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "grid_export_cost" if is_export else "grid_cost"

    async def async_added_to_hass(self) -> None:
        """Restore state and start tracking source sensor."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._accumulated_cost = float(last_state.state)
            attrs = last_state.attributes
            if attrs.get("last_energy_value") is not None:
                self._last_energy_value = float(attrs["last_energy_value"])

        self._unsub = async_track_state_change_event(
            self.hass, [self._source_entity], self._handle_energy_change
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()

    @callback
    def _handle_energy_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle state change on the source energy sensor."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return

        try:
            new_energy = float(new_state.state)
        except (ValueError, TypeError):
            return

        rate = _get_flat_rate(self._rate_coordinator)
        if self._tou_coordinator is not None:
            tou_rate = _get_tou_rate(self._rate_coordinator, self._tou_coordinator)
            if tou_rate is not None:
                rate = tou_rate
        if rate is None:
            return

        if self._last_energy_value is not None:
            delta = new_energy - self._last_energy_value
            if delta > 0:
                self._accumulated_cost += delta * rate
            elif delta < 0:
                # Meter reset — start fresh accumulation from this point
                pass

        self._last_energy_value = new_energy
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return round(self._accumulated_cost, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rate = _get_flat_rate(self._rate_coordinator)
        tou_period = None
        if self._tou_coordinator is not None:
            tou_rate = _get_tou_rate(self._rate_coordinator, self._tou_coordinator)
            if tou_rate is not None:
                rate = tou_rate
            if self._tou_coordinator.data:
                tou_period = self._tou_coordinator.data.get("period")
        attrs: dict[str, Any] = {
            "source_entity": self._source_entity,
            "current_rate_per_kwh": rate,
            "last_energy_value": self._last_energy_value,
        }
        if tou_period is not None:
            attrs["current_tou_period"] = tou_period
        return attrs


class KwcostGridExportCreditSensor(RestoreEntity, SensorEntity):
    """Tracks cumulative credit for grid energy exported, TOU-aware.

    Uses the current TOU period rate when calculating the credit for each
    energy delta, so on-peak exports get valued at the higher rate.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:solar-power-variant"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: HomeAssistant,
        rate_coordinator: KwcostRateCoordinator,
        tou_coordinator: KwcostTouCoordinator,
        entry: ConfigEntry,
        source_entity: str,
    ) -> None:
        self.hass = hass
        self._rate_coordinator = rate_coordinator
        self._tou_coordinator = tou_coordinator
        self._source_entity = source_entity
        self._accumulated_credit: float = 0.0
        self._last_energy_value: float | None = None
        self._unsub: Any = None

        self._attr_unique_id = f"{entry.entry_id}_grid_export_credit_tou"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "grid_export_credit"

    async def async_added_to_hass(self) -> None:
        """Restore state and start tracking source sensor."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._accumulated_credit = float(last_state.state)
            attrs = last_state.attributes
            if attrs.get("last_energy_value") is not None:
                self._last_energy_value = float(attrs["last_energy_value"])

        self._unsub = async_track_state_change_event(
            self.hass, [self._source_entity], self._handle_energy_change
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()

    @callback
    def _handle_energy_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle state change on the source energy sensor."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return

        try:
            new_energy = float(new_state.state)
        except (ValueError, TypeError):
            return

        # Use TOU-aware rate
        rate = _get_tou_rate(self._rate_coordinator, self._tou_coordinator)
        if rate is None:
            rate = _get_flat_rate(self._rate_coordinator)
        if rate is None:
            return

        if self._last_energy_value is not None:
            delta = new_energy - self._last_energy_value
            if delta > 0:
                self._accumulated_credit += delta * rate
            elif delta < 0:
                # Meter reset
                pass

        self._last_energy_value = new_energy
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return round(self._accumulated_credit, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tou_period = None
        if self._tou_coordinator.data:
            tou_period = self._tou_coordinator.data.get("period")
        current_rate = _get_tou_rate(
            self._rate_coordinator, self._tou_coordinator
        )
        return {
            "source_entity": self._source_entity,
            "current_rate_per_kwh": current_rate,
            "current_tou_period": tou_period,
            "last_energy_value": self._last_energy_value,
        }

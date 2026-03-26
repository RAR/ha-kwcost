"""Sensor entities for Kilowatt Cost."""

from __future__ import annotations

import logging
from datetime import datetime
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
    CONF_INCLUDE_RIDERS,
    CONF_OPTIONAL_RIDERS,
    CONF_NAMEPLATE_KW,
    CONF_BILLING_DAY,
)
from .coordinator import KwcostRateCoordinator, KwcostTouCoordinator, KwcostTariffCoordinator

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
    tariff_coordinator: KwcostTariffCoordinator | None = coordinators.get("tariff")

    entities: list[SensorEntity] = [
        KwcostBaseRateSensor(rate_coordinator, entry, tou_coordinator),
        KwcostEffectiveRateSensor(rate_coordinator, entry, tou_coordinator),
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

    if tariff_coordinator is not None:
        entities.append(KwcostTariffForecastSensor(tariff_coordinator, entry))

    # Cost tracking sensors based on user-selected energy sensors
    grid_in_entity = entry.data.get(CONF_GRID_ENERGY_IN)
    include_riders = entry.data.get(CONF_INCLUDE_RIDERS, True)
    optional_riders = entry.data.get(CONF_OPTIONAL_RIDERS, [])
    nameplate_kw = entry.data.get(CONF_NAMEPLATE_KW, 0.0)
    grid_cost_sensor: KwcostGridCostSensor | None = None
    if grid_in_entity:
        grid_cost_sensor = KwcostGridCostSensor(
            hass, rate_coordinator, entry, grid_in_entity,
            tou_coordinator=tou_coordinator,
            include_riders=include_riders,
        )
        entities.append(grid_cost_sensor)

    grid_out_entity = entry.data.get(CONF_GRID_ENERGY_OUT)
    if grid_out_entity and tou_coordinator is not None:
        entities.append(
            KwcostGridExportCreditSensor(
                hass, rate_coordinator, tou_coordinator, entry, grid_out_entity,
                include_riders=include_riders,
                optional_riders=optional_riders,
            )
        )
    elif grid_out_entity:
        # No TOU — use flat rate for export credit too
        entities.append(
            KwcostGridCostSensor(
                hass, rate_coordinator, entry, grid_out_entity, is_export=True,
                include_riders=include_riders,
            )
        )

    # Optional rider monthly charges sensor
    if optional_riders:
        entities.append(
            KwcostOptionalRiderSensor(
                rate_coordinator, entry, optional_riders, nameplate_kw
            )
        )

    # Monthly bill estimate sensor
    billing_day = entry.data.get(CONF_BILLING_DAY, 1)
    if grid_cost_sensor is not None:
        entities.append(
            KwcostMonthlyBillSensor(
                hass, rate_coordinator, entry, grid_cost_sensor, billing_day
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
        self,
        coordinator: KwcostRateCoordinator,
        entry: ConfigEntry,
        tou_coordinator: KwcostTouCoordinator | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._tou_coordinator = tou_coordinator
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
        # Flat schedule: top-level base_rate_per_kwh
        if "base_rate_per_kwh" in summary:
            return summary["base_rate_per_kwh"]
        # TOU schedule: per-period dict — use current TOU period
        period = None
        if self._tou_coordinator and self._tou_coordinator.data:
            period = self._tou_coordinator.data.get("period")
        if period and period in summary:
            return summary[period].get("base_rate_per_kwh")
        # Fallback: return the first period's rate
        for key, val in summary.items():
            if isinstance(val, dict) and "base_rate_per_kwh" in val:
                return val["base_rate_per_kwh"]
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        details = self.coordinator.data.get("rate", {}).get("details", {})
        charges = details.get("energy_charges_per_kwh", {})
        attrs: dict[str, Any] = {
            "effective_date": details.get("effective_date"),
        }
        if isinstance(charges, list):
            attrs["energy_tiers"] = charges
        elif isinstance(charges, dict):
            attrs["energy_rates_by_period"] = charges
        if self._tou_coordinator and self._tou_coordinator.data:
            attrs["current_tou_period"] = self._tou_coordinator.data.get("period")
        return attrs


class KwcostEffectiveRateSensor(CoordinatorEntity[KwcostRateCoordinator], SensorEntity):
    """Effective rate in cents/kWh."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "¢/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash"

    def __init__(
        self,
        coordinator: KwcostRateCoordinator,
        entry: ConfigEntry,
        tou_coordinator: KwcostTouCoordinator | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._tou_coordinator = tou_coordinator
        self._attr_unique_id = f"{entry.entry_id}_effective_rate"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "effective_rate"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        summary = (
            self.coordinator.data.get("rate", {})
            .get("effective_rate_summary", {})
        )
        # Flat schedule
        if "effective_cents_per_kwh" in summary:
            return summary["effective_cents_per_kwh"]
        # TOU schedule: per-period dict
        period = None
        if self._tou_coordinator and self._tou_coordinator.data:
            period = self._tou_coordinator.data.get("period")
        if period and period in summary:
            return summary[period].get("effective_cents_per_kwh")
        for key, val in summary.items():
            if isinstance(val, dict) and "effective_cents_per_kwh" in val:
                return val["effective_cents_per_kwh"]
        return None


class KwcostScheduleNameSensor(CoordinatorEntity[KwcostRateCoordinator], SensorEntity):
    """Current rate schedule name."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:file-document-outline"

    def __init__(
        self, coordinator: KwcostRateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "jurisdiction": self._entry.data.get(CONF_JURISDICTION),
            "category": self._entry.data.get("category"),
            "schedule_code": self._entry.data.get(CONF_SCHEDULE),
            "tou_schedule": self._entry.data.get(CONF_TOU_SCHEDULE, ""),
            "include_riders": self._entry.data.get(CONF_INCLUDE_RIDERS, True),
        }
        optional = self._entry.data.get(CONF_OPTIONAL_RIDERS, [])
        if optional:
            attrs["optional_riders"] = optional
        nameplate = self._entry.data.get(CONF_NAMEPLATE_KW)
        if nameplate:
            attrs["nameplate_capacity_kw"] = nameplate
        return attrs


class KwcostBaseFacilityChargeSensor(
    CoordinatorEntity[KwcostRateCoordinator], SensorEntity
):
    """Monthly basic facilities charge."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:cash-lock"

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


def _get_rider_adder(coordinator: KwcostRateCoordinator) -> float:
    """Sum all mandatory rider rates and return the total adder in $/kWh."""
    if not coordinator.data:
        return 0.0
    riders_data = coordinator.data.get("riders", {})
    mandatory = riders_data.get("mandatory_riders", {})
    total_cents = 0.0
    for group_data in mandatory.values():
        riders = group_data.get("riders", {})
        for rider in riders.values():
            rate = rider.get("rate_cents_per_kwh", 0.0)
            if isinstance(rate, (int, float)):
                total_cents += rate
    return total_cents / 100.0  # convert ¢/kWh → $/kWh


def _get_export_credit_rate(
    coordinator: KwcostRateCoordinator,
    optional_rider_codes: list[str],
) -> float | None:
    """Get per-kWh export credit rate from an optional solar/net metering rider.

    Checks for RSC, NMB, NM in order of priority and returns the credit
    value if the rider is selected and present in the coordinator data.
    Returns None if no applicable rider credit rate is found.
    """
    if not coordinator.data or not optional_rider_codes:
        return None
    optional = coordinator.data.get("riders", {}).get("optional_riders", {})
    # Check selected riders in priority order
    for code in ("RSC", "NMB", "NM"):
        if code not in optional_rider_codes:
            continue
        rider = optional.get(code, {})
        charges = rider.get("charges", {})
        if isinstance(charges, dict):
            # Flat dict: {"solar_energy_credit_per_kwh": -0.021543, ...}
            for key, value in charges.items():
                if "credit" in key and isinstance(value, (int, float)) and value < 0:
                    return abs(value)
        elif isinstance(charges, list):
            # List of charge objects: [{"type": "credit", "unit": "per_kwh", "value": ...}]
            for charge in charges:
                if isinstance(charge, dict) and charge.get("type") == "credit" and charge.get("unit") == "per_kwh":
                    return charge["value"]
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
        include_riders: bool = True,
    ) -> None:
        self.hass = hass
        self._rate_coordinator = rate_coordinator
        self._tou_coordinator = tou_coordinator
        self._source_entity = source_entity
        self._is_export = is_export
        self._include_riders = include_riders
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

        if self._include_riders:
            rate += _get_rider_adder(self._rate_coordinator)

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
        rider_adder = _get_rider_adder(self._rate_coordinator) if self._include_riders else 0.0
        all_in_rate = (rate + rider_adder) if rate is not None else None
        attrs: dict[str, Any] = {
            "source_entity": self._source_entity,
            "base_rate_per_kwh": rate,
            "rider_adder_per_kwh": round(rider_adder, 6),
            "current_rate_per_kwh": round(all_in_rate, 6) if all_in_rate is not None else None,
            "include_riders": self._include_riders,
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
        include_riders: bool = True,
        optional_riders: list[str] | None = None,
    ) -> None:
        self.hass = hass
        self._rate_coordinator = rate_coordinator
        self._tou_coordinator = tou_coordinator
        self._source_entity = source_entity
        self._include_riders = include_riders
        self._optional_riders = optional_riders or []
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

        # Prefer rider-specific export credit rate (RSC/NMB/NM)
        rider_credit = _get_export_credit_rate(
            self._rate_coordinator, self._optional_riders
        )
        if rider_credit is not None:
            rate = rider_credit
        else:
            # Fall back to TOU-aware retail rate
            rate = _get_tou_rate(self._rate_coordinator, self._tou_coordinator)
            if rate is None:
                rate = _get_flat_rate(self._rate_coordinator)
            if rate is None:
                return
            if self._include_riders:
                rate += _get_rider_adder(self._rate_coordinator)

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

        rider_credit = _get_export_credit_rate(
            self._rate_coordinator, self._optional_riders
        )
        if rider_credit is not None:
            effective_rate = rider_credit
            rate_source = "rider_credit"
        else:
            effective_rate = _get_tou_rate(
                self._rate_coordinator, self._tou_coordinator
            )
            rider_adder = _get_rider_adder(self._rate_coordinator) if self._include_riders else 0.0
            if effective_rate is not None:
                effective_rate += rider_adder
            rate_source = "retail_rate"

        return {
            "source_entity": self._source_entity,
            "credit_rate_per_kwh": round(effective_rate, 6) if effective_rate is not None else None,
            "rate_source": rate_source,
            "optional_riders": self._optional_riders,
            "include_riders": self._include_riders,
            "current_tou_period": tou_period,
            "last_energy_value": self._last_energy_value,
        }


class KwcostOptionalRiderSensor(
    CoordinatorEntity[KwcostRateCoordinator], SensorEntity
):
    """Shows selected optional rider details and estimated monthly fixed charges."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-panel"

    def __init__(
        self,
        coordinator: KwcostRateCoordinator,
        entry: ConfigEntry,
        optional_rider_codes: list[str],
        nameplate_kw: float,
    ) -> None:
        super().__init__(coordinator)
        self._rider_codes = optional_rider_codes
        self._nameplate_kw = nameplate_kw
        self._attr_unique_id = f"{entry.entry_id}_optional_riders"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "optional_rider_charges"

    @property
    def native_value(self) -> float | None:
        """Estimated monthly fixed charges from selected optional riders."""
        if not self.coordinator.data:
            return None
        optional = self.coordinator.data.get("riders", {}).get("optional_riders", {})
        total = 0.0
        for code in self._rider_codes:
            rider = optional.get(code, {})
            # Fixed charges are a flat dict: {"daily_service_charge_dollars": 0.48, ...}
            fixed = rider.get("fixed_charges", {})
            if isinstance(fixed, dict):
                for _key, value in fixed.items():
                    if isinstance(value, (int, float)):
                        total += value
            # Also check charges dict for per-kW items
            charges = rider.get("charges", {})
            if isinstance(charges, dict):
                for key, value in charges.items():
                    if "per_kw" in key and isinstance(value, (int, float)) and self._nameplate_kw > 0:
                        total += value * self._nameplate_kw
        return round(total, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {"selected_riders": self._rider_codes}
        optional = self.coordinator.data.get("riders", {}).get("optional_riders", {})
        attrs: dict[str, Any] = {
            "selected_riders": self._rider_codes,
            "nameplate_capacity_kw": self._nameplate_kw,
        }
        for code in self._rider_codes:
            rider = optional.get(code, {})
            if rider:
                attrs[f"{code}_name"] = rider.get("name", code)
                charges = rider.get("charges", {})
                if isinstance(charges, dict):
                    for key, value in charges.items():
                        attr_key = f"{code}_{key}"
                        attrs[attr_key] = value
                fixed = rider.get("fixed_charges", {})
                if isinstance(fixed, dict):
                    for key, value in fixed.items():
                        attr_key = f"{code}_{key}"
                        attrs[attr_key] = value
                min_bill = rider.get("minimum_bill_dollars")
                if min_bill:
                    attrs[f"{code}_minimum_bill"] = min_bill
        return attrs


class KwcostTariffForecastSensor(
    CoordinatorEntity[KwcostTariffCoordinator], SensorEntity
):
    """EVCC-compatible tariff forecast with hourly prices in attributes.

    The state is the current hour's price in $/kWh.  The `forecast`
    attribute contains the full array of {start, end, value} slots
    that EVCC and similar consumers can read directly.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(
        self,
        coordinator: KwcostTariffCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_tariff_forecast"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "tariff_forecast"

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        # First slot is the current hour's price
        return self.coordinator.data[0]["value"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "forecast": self.coordinator.data,
            "slots": len(self.coordinator.data),
            "schedule": self.coordinator.tou_schedule,
            "jurisdiction": self.coordinator.jurisdiction,
            "rate_schedule": self.coordinator.rate_schedule,
        }


class KwcostMonthlyBillSensor(RestoreEntity, SensorEntity):
    """Estimated monthly bill — facilities charge + accumulated energy costs.

    Resets on the configured billing day each month.
    """

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "$"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:receipt-text"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: HomeAssistant,
        rate_coordinator: KwcostRateCoordinator,
        entry: ConfigEntry,
        grid_cost_sensor: KwcostGridCostSensor,
        billing_day: int = 1,
    ) -> None:
        self.hass = hass
        self._rate_coordinator = rate_coordinator
        self._grid_cost_sensor = grid_cost_sensor
        self._billing_day = billing_day
        self._last_reset_month: int | None = None
        self._energy_cost_at_reset: float = 0.0

        self._attr_unique_id = f"{entry.entry_id}_monthly_bill"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "monthly_bill"

    async def async_added_to_hass(self) -> None:
        """Restore state."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            attrs = last_state.attributes
            self._last_reset_month = attrs.get("last_reset_month")
            self._energy_cost_at_reset = attrs.get("energy_cost_at_reset", 0.0)

    def _check_reset(self) -> None:
        """Reset if we've passed the billing day in a new month."""
        now = datetime.now()
        # Determine current billing month: if we're past billing day, it's this month.
        # If before billing day, it's last month's cycle.
        if now.day >= self._billing_day:
            billing_month = now.month
        else:
            billing_month = now.month - 1 if now.month > 1 else 12

        if self._last_reset_month != billing_month:
            self._last_reset_month = billing_month
            self._energy_cost_at_reset = self._grid_cost_sensor.native_value or 0.0

    @property
    def native_value(self) -> float | None:
        self._check_reset()
        facility = (
            self._rate_coordinator.data.get("rate", {})
            .get("details", {})
            .get("basic_facilities_charge_dollars", 0.0)
        ) if self._rate_coordinator.data else 0.0

        current_energy_cost = self._grid_cost_sensor.native_value or 0.0
        energy_since_reset = current_energy_cost - self._energy_cost_at_reset

        return round(facility + max(energy_since_reset, 0.0), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        facility = (
            self._rate_coordinator.data.get("rate", {})
            .get("details", {})
            .get("basic_facilities_charge_dollars", 0.0)
        ) if self._rate_coordinator.data else 0.0

        current_energy_cost = self._grid_cost_sensor.native_value or 0.0
        energy_since_reset = max(current_energy_cost - self._energy_cost_at_reset, 0.0)

        return {
            "facilities_charge": facility,
            "energy_cost": round(energy_since_reset, 2),
            "billing_day": self._billing_day,
            "last_reset_month": self._last_reset_month,
            "energy_cost_at_reset": self._energy_cost_at_reset,
        }

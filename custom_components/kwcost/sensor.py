"""Sensor entities for Kilowatt Cost."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
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
                grid_in_entity=grid_in_entity,
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

    @callback
    def _handle_tou_update(self) -> None:
        """Re-evaluate state when TOU period changes."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to both rate and TOU coordinator updates."""
        await super().async_added_to_hass()
        if self._tou_coordinator:
            self.async_on_remove(
                self._tou_coordinator.async_add_listener(self._handle_tou_update)
            )

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

    @callback
    def _handle_tou_update(self) -> None:
        """Re-evaluate state when TOU period changes."""
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to both rate and TOU coordinator updates."""
        await super().async_added_to_hass()
        if self._tou_coordinator:
            self.async_on_remove(
                self._tou_coordinator.async_add_listener(self._handle_tou_update)
            )

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

    async def async_recalculate_from_history(self, api_client, tou_schedule: str | None = None, days: int = 30) -> dict:
        """Recalculate accumulated cost by replaying energy history with correct rates.

        Queries HA's recorder for all state changes on the source energy entity,
        resolves TOU periods for each timestamp via the API, and applies current rates.
        Returns a summary dict.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import state_changes_during_period

        _LOGGER.info("Recalculating grid cost from history for %s (%d days)", self._source_entity, days)

        # Get all history for the source energy entity
        now = datetime.now()
        start = now - timedelta(days=days)

        history = await get_instance(self.hass).async_add_executor_job(
            state_changes_during_period,
            self.hass,
            start,
            now,
            self._source_entity,
        )

        states = history.get(self._source_entity, [])
        if not states:
            _LOGGER.warning("No history found for %s", self._source_entity)
            return {"error": "No history found", "entity": self._source_entity}

        # Get current rates from coordinator
        flat_rate = _get_flat_rate(self._rate_coordinator)
        rider_adder = _get_rider_adder(self._rate_coordinator) if self._include_riders else 0.0

        # Build rate lookup by TOU period from coordinator data
        period_rates = {}
        if self._rate_coordinator.data:
            summary = (
                self._rate_coordinator.data.get("rate", {})
                .get("effective_rate_summary", {})
            )
            if "base_rate_per_kwh" not in summary:
                # TOU schedule — summary has per-period rates
                for period_name, period_data in summary.items():
                    if isinstance(period_data, dict) and "base_rate_per_kwh" in period_data:
                        period_rates[period_name] = period_data["base_rate_per_kwh"]

        # Cache TOU lookups by hour to avoid excessive API calls
        tou_cache: dict[str, str] = {}  # "YYYY-MM-DD HH" -> period

        accumulated = 0.0
        last_value: float | None = None
        processed = 0
        skipped = 0

        for state in states:
            if state.state in (None, "unknown", "unavailable", ""):
                skipped += 1
                continue
            try:
                energy = float(state.state)
            except (ValueError, TypeError):
                skipped += 1
                continue

            if last_value is not None:
                delta = energy - last_value
                if delta > 0:
                    # Determine rate for this timestamp
                    rate = flat_rate
                    if tou_schedule and period_rates and state.last_changed:
                        hour_key = state.last_changed.strftime("%Y-%m-%d %H")
                        if hour_key not in tou_cache:
                            try:
                                dt_str = state.last_changed.isoformat()
                                result = await api_client.async_tou_lookup(
                                    tou_schedule, dt_str
                                )
                                tou_cache[hour_key] = result.get("period", "off_peak")
                            except Exception:
                                tou_cache[hour_key] = "off_peak"
                        period = tou_cache[hour_key]
                        if period in period_rates:
                            rate = period_rates[period]

                    if rate is not None:
                        all_in_rate = rate + rider_adder
                        accumulated += delta * all_in_rate
                        processed += 1
                elif delta < 0:
                    pass  # meter reset

            last_value = energy

        old_cost = self._accumulated_cost
        self._accumulated_cost = accumulated
        self._last_energy_value = last_value
        self.async_write_ha_state()

        summary = {
            "entity": self.entity_id,
            "source_entity": self._source_entity,
            "history_states": len(states),
            "processed_deltas": processed,
            "skipped": skipped,
            "old_cost": round(old_cost, 2),
            "new_cost": round(accumulated, 2),
            "difference": round(accumulated - old_cost, 2),
            "tou_periods_resolved": len(tou_cache),
        }
        _LOGGER.info("Recalculation complete: %s", summary)
        return summary

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
    """Tracks cumulative credit for grid energy exported with per-TOU-period netting.

    Within each TOU period, exports offset imports at the full retail rate (1:1).
    Once exports exceed imports for the current period, additional exports are
    credited at the Net Excess Energy Credit (NEEC) rate from the rider.
    Counters reset when the TOU period changes.
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
        grid_in_entity: str | None = None,
        include_riders: bool = True,
        optional_riders: list[str] | None = None,
    ) -> None:
        self.hass = hass
        self._rate_coordinator = rate_coordinator
        self._tou_coordinator = tou_coordinator
        self._source_entity = source_entity
        self._grid_in_entity = grid_in_entity
        self._include_riders = include_riders
        self._optional_riders = optional_riders or []
        self._accumulated_credit: float = 0.0
        self._last_export_value: float | None = None
        self._last_import_value: float | None = None
        self._unsub_export: Any = None
        self._unsub_import: Any = None
        self._unsub_tou: Any = None

        # Per-period netting state
        self._current_period: str | None = None
        self._period_imports: float = 0.0
        self._period_exports: float = 0.0

        self._attr_unique_id = f"{entry.entry_id}_grid_export_credit_tou"
        self._attr_device_info = _device_info(entry)
        self._attr_translation_key = "grid_export_credit"

    async def async_added_to_hass(self) -> None:
        """Restore state and start tracking energy sensors and TOU changes."""
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._accumulated_credit = float(last_state.state)
            attrs = last_state.attributes
            if attrs.get("last_export_value") is not None:
                self._last_export_value = float(attrs["last_export_value"])
            # Legacy restore: check old attribute name
            elif attrs.get("last_energy_value") is not None:
                self._last_export_value = float(attrs["last_energy_value"])
            if attrs.get("last_import_value") is not None:
                self._last_import_value = float(attrs["last_import_value"])
            if attrs.get("period_imports") is not None:
                self._period_imports = float(attrs["period_imports"])
            if attrs.get("period_exports") is not None:
                self._period_exports = float(attrs["period_exports"])
            self._current_period = attrs.get("current_tou_period")

        # Track export energy sensor
        self._unsub_export = async_track_state_change_event(
            self.hass, [self._source_entity], self._handle_export_change
        )

        # Track import energy sensor for per-period netting
        if self._grid_in_entity:
            self._unsub_import = async_track_state_change_event(
                self.hass, [self._grid_in_entity], self._handle_import_change
            )

        # Track TOU period changes to reset netting counters
        self._unsub_tou = self._tou_coordinator.async_add_listener(
            self._handle_tou_update
        )

        # Initialize current period
        if self._tou_coordinator.data and self._current_period is None:
            self._current_period = self._tou_coordinator.data.get("period")

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_export:
            self._unsub_export()
        if self._unsub_import:
            self._unsub_import()
        if self._unsub_tou:
            self._unsub_tou()

    async def async_recalculate_from_history(self, api_client, tou_schedule: str | None = None, days: int = 30) -> dict:
        """Recalculate export credit by replaying import+export history with per-period netting.

        Queries HA's recorder for both import and export energy entity histories,
        merges them chronologically, and replays the netting logic with correct rates.
        """
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import state_changes_during_period

        _LOGGER.info("Recalculating export credit from history for %s (%d days)", self._source_entity, days)

        now = datetime.now()
        start = now - timedelta(days=days)

        # Get export history
        export_history = await get_instance(self.hass).async_add_executor_job(
            state_changes_during_period, self.hass, start, now, self._source_entity,
        )
        export_states = export_history.get(self._source_entity, [])

        # Get import history for netting
        import_states = []
        if self._grid_in_entity:
            import_history = await get_instance(self.hass).async_add_executor_job(
                state_changes_during_period, self.hass, start, now, self._grid_in_entity,
            )
            import_states = import_history.get(self._grid_in_entity, [])

        if not export_states:
            _LOGGER.warning("No export history found for %s", self._source_entity)
            return {"error": "No export history found", "entity": self._source_entity}

        # Get rates
        retail_rates = {}
        if self._rate_coordinator.data:
            summary = (
                self._rate_coordinator.data.get("rate", {})
                .get("effective_rate_summary", {})
            )
            if "base_rate_per_kwh" in summary:
                retail_rates["_flat"] = summary["base_rate_per_kwh"]
            else:
                for period_name, period_data in summary.items():
                    if isinstance(period_data, dict) and "base_rate_per_kwh" in period_data:
                        retail_rates[period_name] = period_data["base_rate_per_kwh"]

        rider_adder = _get_rider_adder(self._rate_coordinator) if self._include_riders else 0.0
        neec_rate = _get_export_credit_rate(self._rate_coordinator, self._optional_riders)

        # Merge import+export events chronologically
        events = []
        last_import = None
        for s in import_states:
            if s.state in (None, "unknown", "unavailable", ""):
                continue
            try:
                val = float(s.state)
            except (ValueError, TypeError):
                continue
            if last_import is not None:
                delta = val - last_import
                if delta > 0:
                    events.append(("import", s.last_changed, delta))
            last_import = val

        last_export = None
        for s in export_states:
            if s.state in (None, "unknown", "unavailable", ""):
                continue
            try:
                val = float(s.state)
            except (ValueError, TypeError):
                continue
            if last_export is not None:
                delta = val - last_export
                if delta > 0:
                    events.append(("export", s.last_changed, delta))
            last_export = val

        events.sort(key=lambda e: e[1])

        # TOU cache
        tou_cache: dict[str, str] = {}

        async def get_period(dt: datetime) -> str:
            if not tou_schedule:
                return "_flat"
            hour_key = dt.strftime("%Y-%m-%d %H")
            if hour_key not in tou_cache:
                try:
                    result = await api_client.async_tou_lookup(tou_schedule, dt.isoformat())
                    tou_cache[hour_key] = result.get("period", "off_peak")
                except Exception:
                    tou_cache[hour_key] = "off_peak"
            return tou_cache[hour_key]

        # Replay with per-period netting
        accumulated_credit = 0.0
        current_period = None
        period_imports = 0.0
        period_exports = 0.0
        processed = 0

        for event_type, dt, delta in events:
            period = await get_period(dt)

            # Period changed — reset netting
            if period != current_period:
                current_period = period
                period_imports = 0.0
                period_exports = 0.0

            if event_type == "import":
                period_imports += delta
            elif event_type == "export":
                period_exports += delta

                # Get retail rate for this period
                rate = retail_rates.get(period) or retail_rates.get("_flat")
                if rate is not None:
                    rate += rider_adder

                remaining_offset = max(period_imports - (period_exports - delta), 0)

                if remaining_offset >= delta:
                    if rate is not None:
                        accumulated_credit += delta * rate
                elif remaining_offset > 0:
                    if rate is not None:
                        accumulated_credit += remaining_offset * rate
                    excess = delta - remaining_offset
                    accumulated_credit += excess * (neec_rate if neec_rate else 0.0)
                else:
                    accumulated_credit += delta * (neec_rate if neec_rate else 0.0)

                processed += 1

        old_credit = self._accumulated_credit
        self._accumulated_credit = accumulated_credit
        self._last_export_value = last_export
        self._last_import_value = last_import
        self._current_period = current_period
        self._period_imports = period_imports
        self._period_exports = period_exports
        self.async_write_ha_state()

        result = {
            "entity": self.entity_id,
            "export_entity": self._source_entity,
            "import_entity": self._grid_in_entity,
            "history_events": len(events),
            "processed_exports": processed,
            "old_credit": round(old_credit, 2),
            "new_credit": round(accumulated_credit, 2),
            "difference": round(accumulated_credit - old_credit, 2),
            "tou_periods_resolved": len(tou_cache),
        }
        _LOGGER.info("Export credit recalculation complete: %s", result)
        return result

    @callback
    def _handle_tou_update(self) -> None:
        """Reset period netting counters when TOU period changes."""
        if not self._tou_coordinator.data:
            return
        new_period = self._tou_coordinator.data.get("period")
        if new_period and new_period != self._current_period:
            _LOGGER.debug(
                "TOU period changed %s -> %s, resetting netting counters "
                "(was: %.3f kWh imports, %.3f kWh exports)",
                self._current_period, new_period,
                self._period_imports, self._period_exports,
            )
            self._current_period = new_period
            self._period_imports = 0.0
            self._period_exports = 0.0
            self.async_write_ha_state()

    @callback
    def _handle_import_change(self, event: Event[EventStateChangedData]) -> None:
        """Track imports in the current TOU period for netting."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            new_energy = float(new_state.state)
        except (ValueError, TypeError):
            return

        if self._last_import_value is not None:
            delta = new_energy - self._last_import_value
            if delta > 0:
                self._period_imports += delta
            elif delta < 0:
                # Meter reset
                pass
        self._last_import_value = new_energy

    @callback
    def _handle_export_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle export energy change with per-period netting."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return

        try:
            new_energy = float(new_state.state)
        except (ValueError, TypeError):
            return

        if self._last_export_value is not None:
            delta = new_energy - self._last_export_value
            if delta > 0:
                self._period_exports += delta

                # Determine how much of this delta gets retail vs NEEC rate
                retail_rate = _get_tou_rate(self._rate_coordinator, self._tou_coordinator)
                if retail_rate is None:
                    retail_rate = _get_flat_rate(self._rate_coordinator)
                if retail_rate is None:
                    self._last_export_value = new_energy
                    return
                if self._include_riders:
                    retail_rate += _get_rider_adder(self._rate_coordinator)

                neec_rate = _get_export_credit_rate(
                    self._rate_coordinator, self._optional_riders
                )

                # How many kWh of exports can still offset imports at retail?
                remaining_offset = max(self._period_imports - (self._period_exports - delta), 0)

                if remaining_offset >= delta:
                    # All of this delta offsets imports → full retail credit
                    self._accumulated_credit += delta * retail_rate
                elif remaining_offset > 0:
                    # Partially offsets, rest at NEEC
                    self._accumulated_credit += remaining_offset * retail_rate
                    excess = delta - remaining_offset
                    self._accumulated_credit += excess * (neec_rate if neec_rate else 0.0)
                else:
                    # All excess → NEEC rate
                    self._accumulated_credit += delta * (neec_rate if neec_rate else 0.0)
            elif delta < 0:
                # Meter reset
                pass

        self._last_export_value = new_energy
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return round(self._accumulated_credit, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tou_period = None
        if self._tou_coordinator.data:
            tou_period = self._tou_coordinator.data.get("period")

        retail_rate = _get_tou_rate(self._rate_coordinator, self._tou_coordinator)
        if retail_rate is None:
            retail_rate = _get_flat_rate(self._rate_coordinator)
        rider_adder = _get_rider_adder(self._rate_coordinator) if self._include_riders else 0.0
        if retail_rate is not None:
            retail_rate += rider_adder

        neec_rate = _get_export_credit_rate(
            self._rate_coordinator, self._optional_riders
        )

        remaining_offset = max(self._period_imports - self._period_exports, 0)
        netting_status = "offset" if self._period_exports <= self._period_imports else "excess"

        return {
            "source_entity": self._source_entity,
            "grid_in_entity": self._grid_in_entity,
            "retail_rate_per_kwh": round(retail_rate, 6) if retail_rate is not None else None,
            "neec_rate_per_kwh": round(neec_rate, 6) if neec_rate is not None else None,
            "current_rate": "retail" if netting_status == "offset" else "neec",
            "optional_riders": self._optional_riders,
            "include_riders": self._include_riders,
            "current_tou_period": tou_period,
            "period_imports": round(self._period_imports, 3),
            "period_exports": round(self._period_exports, 3),
            "period_remaining_offset": round(remaining_offset, 3),
            "netting_status": netting_status,
            "last_export_value": self._last_export_value,
            "last_import_value": self._last_import_value,
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

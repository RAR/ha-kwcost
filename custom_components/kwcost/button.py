"""Button entities for Kilowatt Cost."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, CONF_JURISDICTION, CONF_SCHEDULE, CONF_TOU_SCHEDULE, CONF_GRID_ENERGY_IN, CONF_GRID_ENERGY_OUT

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
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
    """Set up Kilowatt Cost buttons."""
    entities: list[ButtonEntity] = []

    # Always add refresh button
    entities.append(KwcostRefreshRatesButton(hass, entry))

    # Only add recalculate button if grid energy sensors are configured
    has_grid = entry.data.get(CONF_GRID_ENERGY_IN) or entry.data.get(CONF_GRID_ENERGY_OUT)
    if has_grid:
        entities.append(KwcostRecalculateButton(hass, entry))

    async_add_entities(entities)


class KwcostRefreshRatesButton(ButtonEntity):
    """Button to force-refresh rate and rider data from the API."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "refresh_rates"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_refresh_rates"
        self._attr_device_info = _device_info(entry)
        self._attr_name = "Refresh Rates"

    async def async_press(self) -> None:
        """Force refresh all coordinators from the API."""
        _LOGGER.info("Refresh rates button pressed for %s", self._entry.entry_id)
        coordinators = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

        for name in ("rate", "tou", "tariff"):
            coordinator = coordinators.get(name)
            if coordinator is not None:
                await coordinator.async_request_refresh()
                _LOGGER.info("Refreshed %s coordinator", name)


class KwcostRecalculateButton(ButtonEntity):
    """Button to trigger recalculation of cost sensors from history."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calculator-variant"
    _attr_translation_key = "recalculate_costs"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_recalculate"
        self._attr_device_info = _device_info(entry)
        self._attr_name = "Recalculate Costs"

    async def async_press(self) -> None:
        """Trigger cost recalculation from history."""
        _LOGGER.info("Recalculate button pressed for %s", self._entry.entry_id)
        await self.hass.services.async_call(
            DOMAIN,
            "recalculate_costs",
            {"days": 30},
            blocking=True,
        )

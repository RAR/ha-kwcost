"""The Kilowatt Cost integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KwcostApiClient
from .const import (
    DOMAIN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_JURISDICTION,
    CONF_CATEGORY,
    CONF_SCHEDULE,
    CONF_TOU_SCHEDULE,
)
from .coordinator import KwcostRateCoordinator, KwcostTouCoordinator, KwcostTariffCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kilowatt Cost from a config entry."""
    session = async_get_clientsession(hass)
    client = KwcostApiClient(
        session,
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )

    rate_coordinator = KwcostRateCoordinator(
        hass,
        client,
        entry.data[CONF_JURISDICTION],
        entry.data[CONF_CATEGORY],
        entry.data[CONF_SCHEDULE],
    )
    await rate_coordinator.async_config_entry_first_refresh()

    coordinators: dict = {"rate": rate_coordinator}

    tou_schedule = entry.data.get(CONF_TOU_SCHEDULE, "")
    if tou_schedule:
        tou_coordinator = KwcostTouCoordinator(hass, client, tou_schedule)
        await tou_coordinator.async_config_entry_first_refresh()
        coordinators["tou"] = tou_coordinator

        tariff_coordinator = KwcostTariffCoordinator(
            hass,
            client,
            tou_schedule,
            entry.data[CONF_JURISDICTION],
            entry.data[CONF_CATEGORY],
            entry.data[CONF_SCHEDULE],
        )
        await tariff_coordinator.async_config_entry_first_refresh()
        coordinators["tariff"] = tariff_coordinator

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinators

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the entry."""
    await hass.config_entries.async_reload(entry.entry_id)

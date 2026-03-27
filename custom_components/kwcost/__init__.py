"""The Kilowatt Cost integration."""

from __future__ import annotations

import logging
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KwcostApiClient
from .const import (
    DOMAIN,
    CONF_API_KEY,
    CONF_JURISDICTION,
    CONF_CATEGORY,
    CONF_SCHEDULE,
    CONF_TOU_SCHEDULE,
)
from .coordinator import KwcostRateCoordinator, KwcostTouCoordinator, KwcostTariffCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

SERVICE_RECALCULATE = "recalculate_costs"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Kilowatt Cost from a config entry."""
    session = async_get_clientsession(hass)
    client = KwcostApiClient(session, entry.data[CONF_API_KEY])

    rate_coordinator = KwcostRateCoordinator(
        hass,
        client,
        entry.data[CONF_JURISDICTION],
        entry.data[CONF_CATEGORY],
        entry.data[CONF_SCHEDULE],
    )
    await rate_coordinator.async_config_entry_first_refresh()

    coordinators: dict = {"rate": rate_coordinator, "client": client}

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

    # Register services (once per domain)
    if not hass.services.has_service(DOMAIN, SERVICE_RECALCULATE):
        async def handle_recalculate(call: ServiceCall) -> ServiceResponse:
            """Recalculate cost sensors from HA history."""
            from .sensor import KwcostGridCostSensor, KwcostGridExportCreditSensor

            days = call.data.get("days", 30)
            results = []
            for eid, data in hass.data.get(DOMAIN, {}).items():
                if not isinstance(data, dict) or "client" not in data:
                    continue
                api_client = data["client"]
                tou_sched = None
                # Find the TOU schedule from the config entry
                for entry in hass.config_entries.async_entries(DOMAIN):
                    if entry.entry_id == eid:
                        tou_sched = entry.data.get(CONF_TOU_SCHEDULE, "")
                        break

                # Find all cost sensors for this entry
                from homeassistant.helpers import entity_registry as er
                entity_reg = er.async_get(hass)
                entity_platform = hass.data.get("entity_platform", {}).get(DOMAIN, [])

                # Collect sensor entities from the platform
                for platform in entity_platform:
                    for entity in platform.entities.values():
                        if entity.registry_entry and entity.registry_entry.config_entry_id == eid:
                            if isinstance(entity, (KwcostGridCostSensor, KwcostGridExportCreditSensor)):
                                result = await entity.async_recalculate_from_history(
                                    api_client, tou_sched or None, days=days
                                )
                                results.append(result)

            return {"results": results}

        hass.services.async_register(
            DOMAIN,
            SERVICE_RECALCULATE,
            handle_recalculate,
            schema=vol.Schema({
                vol.Optional("days", default=30): vol.All(int, vol.Range(min=1, max=365)),
            }),
            supports_response=SupportsResponse.OPTIONAL,
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload the entry."""
    await hass.config_entries.async_reload(entry.entry_id)

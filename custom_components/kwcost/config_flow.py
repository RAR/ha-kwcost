"""Config flow for Kilowatt Cost integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import KwcostApiClient, KwcostAuthError, KwcostApiError
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    DOMAIN,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_JURISDICTION,
    CONF_CATEGORY,
    CONF_SCHEDULE,
    CONF_TOU_SCHEDULE,
    CONF_STATE,
    CONF_MUNICIPALITY,
    CONF_GRID_ENERGY_IN,
    CONF_GRID_ENERGY_OUT,
    CONF_INCLUDE_RIDERS,
)

_LOGGER = logging.getLogger(__name__)


class KwcostConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Kilowatt Cost."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._client: KwcostApiClient | None = None
        self._email: str = ""
        self._password: str = ""
        self._jurisdictions: dict[str, Any] = {}
        self._tou_schedules: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Collect API credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._email = user_input[CONF_EMAIL]
            self._password = user_input[CONF_PASSWORD]

            session = async_get_clientsession(self.hass)
            self._client = KwcostApiClient(session, self._email, self._password)

            try:
                await self._client.async_validate()
            except KwcostAuthError:
                errors["base"] = "invalid_auth"
            except (KwcostApiError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                # Fetch jurisdictions and TOU schedules for step 2
                try:
                    rates_data = await self._client.async_get_jurisdictions()
                    self._jurisdictions = rates_data.get("jurisdictions", {})
                    self._tou_schedules = await self._client.async_get_tou_schedules()
                except (KwcostApiError, aiohttp.ClientError):
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_schedule()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Select jurisdiction, category, schedule, and TOU schedule."""
        errors: dict[str, str] = {}

        if user_input is not None:
            jurisdiction = user_input[CONF_JURISDICTION]
            category = user_input[CONF_CATEGORY]
            schedule = user_input[CONF_SCHEDULE]
            tou_schedule = user_input.get(CONF_TOU_SCHEDULE, "")

            # Derive state from jurisdiction data
            jur_data = self._jurisdictions.get(jurisdiction, {})
            states = jur_data.get("states", [])
            state = states[0] if states else ""

            await self.async_set_unique_id(
                f"{self._email}_{jurisdiction}_{schedule}"
            )
            self._abort_if_unique_id_configured()

            # Store schedule data and proceed to energy sensor step
            self._schedule_data = {
                CONF_JURISDICTION: jurisdiction,
                CONF_CATEGORY: category,
                CONF_SCHEDULE: schedule,
                CONF_TOU_SCHEDULE: tou_schedule,
                CONF_STATE: state,
                CONF_MUNICIPALITY: user_input.get(CONF_MUNICIPALITY, ""),
            }
            return await self.async_step_energy()

        # Build dropdown options from fetched data
        jurisdiction_options = {
            code: f"{code} — {info.get('name', code)}"
            for code, info in self._jurisdictions.items()
        }

        # Collect all categories and schedules across jurisdictions
        category_options = {"residential": "Residential", "business": "Business"}

        schedule_options: dict[str, str] = {}
        for jur_info in self._jurisdictions.values():
            schedules = jur_info.get("schedules", {})
            for cat_schedules in schedules.values():
                if isinstance(cat_schedules, dict):
                    for code, name in cat_schedules.items():
                        schedule_options[code] = f"{code} — {name}"

        tou_options = {"": "(None — no TOU tracking)"}
        for sched_key, sched_info in self._tou_schedules.items():
            desc = sched_info.get("description", sched_key) if isinstance(sched_info, dict) else sched_key
            tou_options[sched_key] = desc

        return self.async_show_form(
            step_id="schedule",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_JURISDICTION): vol.In(jurisdiction_options),
                    vol.Required(CONF_CATEGORY, default="residential"): vol.In(
                        category_options
                    ),
                    vol.Required(CONF_SCHEDULE): vol.In(schedule_options),
                    vol.Optional(CONF_TOU_SCHEDULE, default=""): vol.In(
                        tou_options
                    ),
                    vol.Optional(CONF_MUNICIPALITY, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Optionally select energy sensors for cost tracking."""
        if user_input is not None:
            data = {
                CONF_EMAIL: self._email,
                CONF_PASSWORD: self._password,
                **self._schedule_data,
            }
            data[CONF_INCLUDE_RIDERS] = user_input.get(CONF_INCLUDE_RIDERS, True)
            if user_input.get(CONF_GRID_ENERGY_IN):
                data[CONF_GRID_ENERGY_IN] = user_input[CONF_GRID_ENERGY_IN]
            if user_input.get(CONF_GRID_ENERGY_OUT):
                data[CONF_GRID_ENERGY_OUT] = user_input[CONF_GRID_ENERGY_OUT]
            return self.async_create_entry(
                title=f"{self._schedule_data[CONF_JURISDICTION]} {self._schedule_data[CONF_SCHEDULE]}",
                data=data,
            )

        energy_selector = EntitySelector(
            EntitySelectorConfig(
                domain="sensor",
                device_class="energy",
            )
        )

        return self.async_show_form(
            step_id="energy",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_INCLUDE_RIDERS, default=True): bool,
                    vol.Optional(CONF_GRID_ENERGY_IN): energy_selector,
                    vol.Optional(CONF_GRID_ENERGY_OUT): energy_selector,
                }
            ),
            errors={},
        )

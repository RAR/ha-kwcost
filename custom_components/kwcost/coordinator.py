"""Data update coordinators for Kilowatt Cost."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import KwcostApiClient, KwcostApiError
from .const import DOMAIN, UPDATE_INTERVAL_RATES, UPDATE_INTERVAL_TOU, UPDATE_INTERVAL_TARIFF

_LOGGER = logging.getLogger(__name__)


class KwcostRateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches rate schedule + riders data (every 24h)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: KwcostApiClient,
        jurisdiction: str,
        category: str,
        schedule: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_rates_{jurisdiction}_{schedule}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_RATES),
        )
        self.client = client
        self.jurisdiction = jurisdiction
        self.category = category
        self.schedule = schedule

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            rate_data = await self.client.async_get_rate(
                self.jurisdiction, self.category, self.schedule
            )
            riders_data = await self.client.async_get_riders(
                self.jurisdiction, self.category, self.schedule
            )
            return {"rate": rate_data, "riders": riders_data}
        except KwcostApiError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except KwcostApiError as err:
            raise UpdateFailed(f"API error: {err}") from err


class KwcostTouCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches current TOU period (every 5 min)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: KwcostApiClient,
        tou_schedule: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tou_{tou_schedule}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_TOU),
        )
        self.client = client
        self.tou_schedule = tou_schedule

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.async_get_tou_now(self.tou_schedule)
        except KwcostApiError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except KwcostApiError as err:
            raise UpdateFailed(f"API error: {err}") from err


class KwcostTariffCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Fetches hourly tariff forecast for EVCC (every 1h)."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: KwcostApiClient,
        tou_schedule: str,
        jurisdiction: str,
        category: str,
        rate_schedule: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tariff_{jurisdiction}_{rate_schedule}",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_TARIFF),
        )
        self.client = client
        self.tou_schedule = tou_schedule
        self.jurisdiction = jurisdiction
        self.category = category
        self.rate_schedule = rate_schedule

    async def _async_update_data(self) -> list[dict[str, Any]]:
        try:
            return await self.client.async_get_tariff_forecast(
                self.tou_schedule,
                self.jurisdiction,
                self.category,
                self.rate_schedule,
            )
        except KwcostApiError as err:
            raise UpdateFailed(f"Authentication failed: {err}") from err
        except KwcostApiError as err:
            raise UpdateFailed(f"API error: {err}") from err

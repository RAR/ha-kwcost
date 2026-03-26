"""API client for the Kilowatt Cost API."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientSession

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)


class KwcostApiError(Exception):
    """Base exception for API errors."""


class KwcostApiClient:
    """Async client for the kwcost API."""

    def __init__(self, session: ClientSession, api_key: str) -> None:
        self._session = session
        self._api_key = api_key

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an API request with API key auth."""
        headers = {"x-api-key": self._api_key}
        resp = await self._session.request(
            method, f"{API_BASE_URL}{path}", headers=headers, **kwargs
        )

        if resp.status == 403:
            raise KwcostApiError("Invalid or missing API key")

        if resp.status >= 400:
            text = await resp.text()
            raise KwcostApiError(f"API error {resp.status}: {text}")

        return await resp.json()

    async def async_get_jurisdictions(self) -> dict[str, Any]:
        """GET /rates/ — list all jurisdictions."""
        return await self._request("GET", "/rates/")

    async def async_get_rate(
        self, jurisdiction: str, category: str, schedule: str
    ) -> dict[str, Any]:
        """GET /rates/{jurisdiction}/{category}/{schedule}."""
        return await self._request(
            "GET", f"/rates/{jurisdiction}/{category}/{schedule}"
        )

    async def async_get_riders(
        self, jurisdiction: str, category: str | None = None, schedule: str | None = None
    ) -> dict[str, Any]:
        """GET /rates/{jurisdiction}/riders."""
        params: dict[str, str] = {}
        if category:
            params["category"] = category
        if schedule:
            params["schedule"] = schedule
        return await self._request(
            "GET", f"/rates/{jurisdiction}/riders", params=params
        )

    async def async_get_tou_schedules(self) -> dict[str, Any]:
        """GET /tou/schedules — list all TOU schedules."""
        return await self._request("GET", "/tou/schedules")

    async def async_get_tou_now(self, schedule: str) -> dict[str, Any]:
        """GET /tou/now?schedule=... — current TOU period."""
        return await self._request("GET", "/tou/now", params={"schedule": schedule})

    async def async_calculate_cost(
        self,
        jurisdiction: str,
        category: str,
        schedule: str,
        total_kwh: float,
        include_riders: bool = True,
        include_taxes: bool = False,
        state: str | None = None,
        municipality: str | None = None,
    ) -> dict[str, Any]:
        """POST /calculate/cost — estimate bill cost."""
        body: dict[str, Any] = {
            "jurisdiction": jurisdiction,
            "category": category,
            "schedule": schedule,
            "total_kwh": total_kwh,
            "include_riders": include_riders,
            "include_taxes": include_taxes,
        }
        if state:
            body["state"] = state
        if municipality:
            body["municipality"] = municipality
        return await self._request("POST", "/calculate/cost", json=body)

    async def async_get_tariff_forecast(
        self,
        tou_schedule: str,
        jurisdiction: str,
        category: str,
        rate_schedule: str,
        hours: int = 48,
    ) -> list[dict[str, Any]]:
        """GET /tou/tariff/forecast — EVCC-compatible hourly price forecast."""
        return await self._request(
            "GET",
            "/tou/tariff/forecast",
            params={
                "schedule": tou_schedule,
                "jurisdiction": jurisdiction,
                "category": category,
                "rate_schedule": rate_schedule,
                "hours": str(hours),
            },
        )

    async def async_validate(self) -> bool:
        """Validate API key by making a test request."""
        await self._request("GET", "/rates/")
        return True

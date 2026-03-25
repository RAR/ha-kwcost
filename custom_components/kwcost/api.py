"""API client for the Kilowatt Cost API."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientSession, ClientResponseError

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)


class KwcostApiError(Exception):
    """Base exception for API errors."""


class KwcostAuthError(KwcostApiError):
    """Authentication failed."""


class KwcostApiClient:
    """Async client for the kwcost API."""

    def __init__(
        self, session: ClientSession, email: str, password: str, api_key: str = ""
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._api_key = api_key
        self._token: str | None = None

    async def _authenticate(self) -> None:
        """Log in and store the id_token."""
        resp = await self._session.post(
            f"{API_BASE_URL}/auth/login",
            json={"email": self._email, "password": self._password},
        )
        if resp.status == 401:
            raise KwcostAuthError("Invalid email or password")
        if resp.status == 403:
            raise KwcostAuthError("Email not confirmed")
        resp.raise_for_status()
        data = await resp.json()
        self._token = data["id_token"]

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an authenticated API request, refreshing token on 401."""
        if self._token is None:
            await self._authenticate()

        headers = {"Authorization": f"Bearer {self._token}"}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        resp = await self._session.request(
            method, f"{API_BASE_URL}{path}", headers=headers, **kwargs
        )

        if resp.status == 401:
            # Token expired — re-authenticate and retry once
            await self._authenticate()
            headers = {"Authorization": f"Bearer {self._token}"}
            if self._api_key:
                headers["x-api-key"] = self._api_key
            resp = await self._session.request(
                method, f"{API_BASE_URL}{path}", headers=headers, **kwargs
            )

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
        """Validate credentials by attempting login."""
        await self._authenticate()
        return True

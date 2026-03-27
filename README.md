# Kilowatt Cost - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for the [kwcost.com](https://kwcost.com) electricity rate API. Monitor Duke Energy rate schedules, time-of-use periods, energy costs, and solar export credits directly in Home Assistant.

## Features

- **Real-Time TOU Rates** — base and effective rates update automatically when the TOU period changes (on-peak, off-peak, discount)
- **TOU Period & Season Tracking** — current period and season with 5-minute polling
- **Tariff Forecast** — EVCC-compatible 48-hour hourly price forecast, auto-refreshes every hour
- **Grid Energy Cost Tracking** — cumulative cost of grid imports, TOU-aware with mandatory rider adders
- **Solar Export Credit with Per-Period Netting** — exports offset imports at full retail rate within each TOU period; excess exports credit at the Net Excess Energy Credit (NEEC) rate from your rider (RSC/NMB)
- **Monthly Bill Estimate** — facilities charge + accumulated energy costs, resets on your billing day
- **Optional Rider Charges** — estimated monthly fixed charges for selected riders (RSC, NMB, etc.) with nameplate capacity support
- **Facility Charge & Schedule Info** — monthly basic facilities charge, schedule name, effective date, energy tier details
- **Multi-Entry Support** — configure multiple jurisdictions or schedules simultaneously

## Supported Jurisdictions

Duke Energy Carolinas (DEC), Duke Energy Progress (DEP), Duke Energy Florida (DEF), Duke Energy Ohio (DEO), Duke Energy Kentucky (DEK), Duke Energy Indiana (DEI)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu -> **Custom repositories**
3. Add repository URL: `https://github.com/RAR/ha-kwcost`
4. Select category: **Integration**
5. Click **Add**, then install **Kilowatt Cost**
6. Restart Home Assistant

### Manual

1. Copy the `custom_components/kwcost` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings -> Devices & Services -> Add Integration**
2. Search for **Kilowatt Cost**
3. Enter your [kwcost.com](https://kwcost.com) API key (free signup at the site)
4. Select your jurisdiction, rate category, and schedule
5. Optionally select a TOU schedule for time-of-use rate tracking
6. Optionally select grid energy sensors (import/export) for cost tracking
7. Optionally configure riders, nameplate capacity, and billing day
8. Sensors will be created automatically

## Sensors

| Sensor | Description | Updates |
|--------|-------------|---------|
| Base Rate | Base energy rate in $/kWh (TOU-aware) | On rate or TOU period change |
| Effective Rate | Effective rate in c/kWh (TOU-aware) | On rate or TOU period change |
| Rate Schedule | Name of the current rate schedule | 24 hours |
| Basic Facilities Charge | Monthly fixed charge in $ | 24 hours |
| TOU Period | Current time-of-use period (on_peak, off_peak, discount, critical_peak) | 5 minutes |
| TOU Season | Current TOU season (summer, non_summer) | 5 minutes |
| Tariff Forecast | Current hour price in $/kWh with 48h forecast in attributes | 1 hour |
| Grid Energy Cost | Cumulative cost of grid imports in $, TOU-aware | On energy sensor change |
| Grid Export Credit | Cumulative export credit in $ with per-period netting | On energy sensor change |
| Optional Rider Charges | Estimated monthly fixed charges from selected riders | 24 hours |
| Monthly Bill Estimate | Facilities + energy costs, resets on billing day | On energy sensor change |

## Solar Export Credit & Per-Period Netting

When you configure grid import and export energy sensors along with an optional solar rider (RSC, NMB, or NM), the export credit sensor implements Duke Energy's per-TOU-period netting:

1. **Within each TOU period**, exports offset imports at the **full retail rate** (1:1 credit)
2. Once exports exceed imports for the current period, additional exports are credited at the **NEEC rate** (e.g., 4.53c/kWh for DEC RSC)
3. **Netting counters reset** when the TOU period changes (e.g., off_peak -> on_peak)

The sensor attributes show the netting state in real time:

| Attribute | Description |
|-----------|-------------|
| `period_imports` | kWh imported in current TOU period |
| `period_exports` | kWh exported in current TOU period |
| `period_remaining_offset` | kWh of exports that can still offset at retail rate |
| `netting_status` | `offset` (getting retail rate) or `excess` (getting NEEC rate) |
| `retail_rate_per_kwh` | Current period's full retail rate |
| `neec_rate_per_kwh` | Net Excess Energy Credit rate from rider |
| `current_rate` | Which rate is currently being applied (`retail` or `neec`) |

## EVCC Integration

The **Tariff Forecast** sensor exposes a `forecast` attribute containing an array of `{start, end, value}` objects for the next 48 hours. Configure EVCC to read it via the HA REST API:

```yaml
tariffs:
  currency: USD
  grid:
    type: custom
    forecast:
      source: http
      uri: https://your-ha-instance/api/states/sensor.kilowatt_cost_dec_rstc_tariff_forecast
      method: GET
      auth:
        type: bearer
        token: <HA_LONG_LIVED_TOKEN>
      jq: '.attributes.forecast | tostring'
```

Replace the URI with your HA instance URL and entity ID (check Developer Tools -> States). Generate a long-lived access token from your HA profile page.

## Requirements

- A [kwcost.com](https://kwcost.com) API key (free signup at the site)
- Home Assistant 2024.1 or later

## License

MIT

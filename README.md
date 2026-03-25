# Kilowatt Cost - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for the [kwcost.com](https://kwcost.com) electricity rate API. Monitor Duke Energy rate schedules, time-of-use periods, and energy costs directly in Home Assistant.

## Features

- **Base & Effective Rates** — current $/kWh and ¢/kWh including mandatory riders
- **TOU Period Tracking** — real-time on-peak / off-peak / shoulder period status (5-minute polling)
- **Tariff Forecast** — EVCC-compatible hourly price forecast (48-hour rolling, auto-refresh every hour)
- **Facility Charge** — monthly basic facilities charge for your schedule
- **Rate Schedule Info** — schedule name, effective date, energy tier details as attributes
- **Multi-entry Support** — configure multiple jurisdictions or schedules simultaneously

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu → **Custom repositories**
3. Add repository URL: `https://github.com/RAR/ha-kwcost`
4. Select category: **Integration**
5. Click **Add**, then install **Kilowatt Cost**
6. Restart Home Assistant

### Manual

1. Copy the `custom_components/kwcost` folder into your Home Assistant `config/custom_components/` directory
2. Restart Home Assistant

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Kilowatt Cost**
3. Enter your kwcost.com credentials (email, password, and optional API key)
4. Select your jurisdiction, rate category, schedule, and optional TOU schedule
5. Sensors will be created automatically

## Sensors

| Sensor | Description | Update Interval |
|--------|-------------|-----------------|
| Base Rate | Base energy rate in $/kWh | 24 hours |
| Effective Rate | Effective rate in ¢/kWh (with mandatory riders) | 24 hours |
| Rate Schedule | Name of the current rate schedule | 24 hours |
| Basic Facilities Charge | Monthly fixed charge in $ | 24 hours |
| TOU Period | Current time-of-use period (on_peak, off_peak, etc.) | 5 minutes |
| TOU Season | Current TOU season (summer, winter, etc.) | 5 minutes |
| Tariff Forecast | Current hour price in $/kWh; `forecast` attribute has 48h EVCC-compatible array | 1 hour |

### EVCC Integration

When a TOU schedule is configured, the **Tariff Forecast** sensor exposes a `forecast`
attribute containing an array of `{start, end, value}` objects — one per hour for
the next 48 hours. Point your EVCC `tariff` configuration at this attribute:

```yaml
tariffs:
  grid:
    type: custom
    price:
      source: http
      uri: http://homeassistant.local:8123/api/states/sensor.kilowatt_cost_dec_retc_tariff_forecast
      headers:
        Authorization: Bearer <HA_LONG_LIVED_TOKEN>
      jq: .attributes.forecast
```

## Requirements

- A [kwcost.com](https://kwcost.com) account (free signup at the site)
- Home Assistant 2024.1 or later

## License

MIT

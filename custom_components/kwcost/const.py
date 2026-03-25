"""Constants for the Kilowatt Cost integration."""

DOMAIN = "kwcost"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_JURISDICTION = "jurisdiction"
CONF_CATEGORY = "category"
CONF_SCHEDULE = "schedule"
CONF_TOU_SCHEDULE = "tou_schedule"
CONF_STATE = "state"
CONF_MUNICIPALITY = "municipality"
CONF_GRID_ENERGY_IN = "grid_energy_in_entity"
CONF_GRID_ENERGY_OUT = "grid_energy_out_entity"
CONF_INCLUDE_RIDERS = "include_riders"
CONF_OPTIONAL_RIDERS = "optional_riders"
CONF_NAMEPLATE_KW = "nameplate_capacity_kw"

API_BASE_URL = "https://api.kwcost.com"

# Update intervals (seconds)
UPDATE_INTERVAL_RATES = 86400  # 24 hours — rates rarely change
UPDATE_INTERVAL_TOU = 300  # 5 minutes — TOU period can change

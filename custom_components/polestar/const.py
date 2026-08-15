"""Constants for the Polestar integration."""

DOMAIN = "polestar"
CONF_DEMO = "demo"
CONF_VIN = "vin"

PLATFORMS = [
    "sensor",
    "binary_sensor",
    "device_tracker",
    "lock",
    "switch",
    "number",
    "button",
    "select",
    "time",
    "calendar",
    "update",
]

# The fast tier's poll interval. Every other tier is a fixed multiple of it,
# so one setting scales the whole integration up or down together.
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 300  # seconds
MIN_UPDATE_INTERVAL = 60
MAX_UPDATE_INTERVAL = 1800

TIER_MULTIPLIERS: dict[str, int] = {
    "fast": 1,
    "medium": 3,
    "slow": 8,
    "very_slow": 30,
}

# Live gRPC subscriptions are an optional latency accelerator on top of
# polling, not the freshness mechanism. Off by default: polling alone keeps
# every entity current, and a persistent subscription per attribute is the
# part of the old design that proved hardest to operate.
CONF_ENABLE_STREAMS = "enable_streams"
DEFAULT_ENABLE_STREAMS = False
STREAM_RETRY_DELAY = 30  # seconds (initial, doubles each retry)
STREAM_MAX_RETRIES = 10

# Used when neither the user nor the car has supplied a climate target.
DEFAULT_CLIMATE_TEMPERATURE = 21.0

SERVICE_START_CLIMATE = "start_climate"
SERVICE_SET_CHARGE_TIMER = "set_charge_timer"
SERVICE_CLEAR_CHARGE_TIMER = "clear_charge_timer"
SERVICE_CREATE_CHARGE_LOCATION = "create_charge_location"
SERVICE_UPDATE_CHARGE_LOCATION = "update_charge_location"
SERVICE_DELETE_CHARGE_LOCATION = "delete_charge_location"
SERVICE_SCHEDULE_OTA = "schedule_ota"
SERVICE_CANCEL_OTA = "cancel_ota"
SERVICE_DELETE_CLIMATE_TIMER = "delete_climate_timer"

ATTR_ENTITY_ID = "entity_id"
ATTR_VIN = "vin"
ATTR_LOCATION_ID = "location_id"
ATTR_TIMER_ID = "timer_id"

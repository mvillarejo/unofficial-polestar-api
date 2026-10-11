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

# How often the single coordinator polls every attribute together while the
# car is idle (parked, locked, not charging, climate off).
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 1800  # seconds
MIN_UPDATE_INTERVAL = 60
MAX_UPDATE_INTERVAL = 3600

# Poll interval while the car is active. With streams on they carry the live
# data and the poll only backs them up; without streams the poll is all there
# is. Never slower than the idle interval the user chose.
ACTIVE_UPDATE_INTERVAL_STREAMING = 600  # seconds
ACTIVE_UPDATE_INTERVAL_POLLING = 120  # seconds
# A command keeps the car "active" for this long after it was sent.
COMMAND_ACTIVE_WINDOW = 300  # seconds

# Live gRPC subscriptions are the primary responsiveness mechanism, on top of
# the slower poll timer above. On by default; can still be turned off from
# Options for a car/account where persistent subscriptions prove unreliable.
CONF_ENABLE_STREAMS = "enable_streams"
DEFAULT_ENABLE_STREAMS = True
STREAM_RETRY_DELAY = 30  # seconds (initial, doubles each retry)
STREAM_MIN_RESUBSCRIBE_INTERVAL = 30  # seconds between subscribes after a clean close
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

"""Constants for the parallel CouchMate Core Dev Preview channel."""

DOMAIN = "couchmate_dev"
STORAGE_KEY = "couchmate_dev"
STORAGE_VERSION = 1

CONF_ENTITIES = "entities"
CONF_EXCLUDED_ENTITIES = "excluded_entities"
CONF_AREAS = "areas"
CONF_DEVICES = "devices"
CONF_ROOM_TEMPERATURES = "room_temperatures"
CONF_ROOM_HUMIDITIES = "room_humidities"
CONF_ROOM_CLIMATES = "room_climates"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_SELECTION_MODEL = "selection_model"
SELECTION_MODEL_VERSION = 2
CONF_FILTER_MODE = "filter_mode"

FILTER_MODE_INCLUDE = "include"
FILTER_MODE_EXCLUDE = "exclude"

WS_TYPE_SUBSCRIBE_FILTERED = f"{DOMAIN}/subscribe_filtered"
WS_TYPE_GET_ENTITIES = f"{DOMAIN}/get_entities"
WS_TYPE_UPDATE_ENTITIES = f"{DOMAIN}/update_entities"
# CouchMate pairing API
PAIRING_SESSION_LIFETIME_SECONDS = 300
PAIRING_CLIENT_STORAGE_VERSION = 1
PAIRING_CLIENT_STORAGE_KEY = "couchmate_dev.paired_clients"
PAIRING_MANAGER = "pairing_manager"

# Versioned CouchMate configuration is intentionally stored separately from the
# existing entity-selection and pairing stores. This keeps every v1 client
# path byte-for-byte compatible and prevents legacy selection updates from
# overwriting dashboard settings.
CONFIGURATION_STORAGE_KEY = "couchmate_dev.configuration"
CONFIGURATION_STORAGE_VERSION = 1
CONFIGURATION_SCHEMA_VERSION = 1
CONFIGURATION_MANAGER = "configuration_manager"
DEFAULT_PROFILE_ID = "default"

# Room backgrounds are private Core assets. They are served only through
# authenticated CouchMate endpoints and never through Home Assistant's
# publicly reachable /local directory.
BACKGROUND_MANAGER = "background_manager"
BACKGROUND_DIRECTORY = "couchmate_dev/backgrounds"
BACKGROUND_MAX_UPLOAD_BYTES = 10 * 1024 * 1024

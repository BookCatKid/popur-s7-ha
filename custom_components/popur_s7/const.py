"""Constants for the Popur integration."""

from datetime import timedelta
from typing import Final

from homeassistant.util.hass_dict import HassKey

DOMAIN: Final = "popur_s7"

CONF_INSTALL_ID: Final = "install_id"
CONF_HOST: Final = "host"
CONF_DISCOVERED_HOSTS: Final = "discovered_hosts"
DHCP_PROBE_TIMES: HassKey[dict[tuple[str, str], float]] = HassKey("popur_s7_dhcp_probe_times")

# Live device state refresh over the local channel; MQTT pushes cover
# real-time changes, this catches anything that is never pushed.
LOCAL_REFRESH_INTERVAL: Final = timedelta(seconds=60)

# Cloud-side data (settings-DP shadow, pets, usage records) refreshes on a
# slower cadence — the LAN channel covers live device state between these.
CLOUD_REFRESH_INTERVAL: Final = timedelta(minutes=10)

PET_RECORDS_PAGE_SIZE: Final = 25
# Pages followed per refresh until they reach every household pet's last
# seen record.
PET_RECORDS_MAX_PAGES: Final = 4
# New visits held per pet until its visit entity subscribes (a pet and its
# first visit can show up in the same refresh); the oldest are dropped past it.
PET_VISITS_PENDING_MAX: Final = 20

# Dispatcher signals for the event platform. Activity/fault events are sent
# per device with ``(ActivityEvent, attributes)``; pet visits per pet with
# ``(attributes,)``.
EVENT_TYPE_VISIT_RECORDED: Final = "visit_recorded"


def signal_device_event(dev_id: str) -> str:
    """Dispatcher signal carrying live MQTT activity/fault events for one device."""

    return f"{DOMAIN}_event_{dev_id}"


def signal_pet_visit(pet_id: int) -> str:
    """Dispatcher signal carrying newly recorded visits for one pet."""

    return f"{DOMAIN}_pet_visit_{pet_id}"

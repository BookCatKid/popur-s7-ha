<p align="center">
  <img src="https://raw.githubusercontent.com/BookCatKid/popur-s7-ha/main/banner.png" alt="Popur" width="480">
</p>

<h1 align="center">Popur S7</h1>

<p align="center">
  <strong>Experimental</strong> — local-first Home Assistant integration for the Popur S7 smart litter box.
</p>

<p align="center">
  <a href="https://github.com/BookCatKid/popur-s7-ha"><img src="https://img.shields.io/badge/GitHub-BookCatKid%2Fpopur--s7--ha-blue" alt="GitHub"></a>
  <a href="https://pypi.org/project/pypopur/"><img src="https://img.shields.io/pypi/v/pypopur" alt="PyPI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
</p>

Controls the Popur S7 via LAN (preferred) and cloud (fallback/settings/pets). Built on [`pypopur`](https://github.com/BookCatKid/pypopur).

## Features

- **Local-first** — LAN control over TCP:6668 (Tuya protocol 3.5); routine polling never touches Tuya's servers
- **Real-time events** — MQTT push for instant state updates and event entities for activity, faults and pet visits
- **Pet tracking** — per-pet weight, visit duration, and last-visit sensors
- **Full entity coverage** — 18 sensors, 5 binary sensors, 2 event entities, 19 switches, 14 buttons, 3 selects, 5 numbers per device
- **Cloud fallback** — automatic fallback to cloud APIs when LAN is unreachable; settings DPs and pet data always via cloud
- **Auto-discovery** — finds the S7 at setup and learns IP changes from Home Assistant DHCP discovery. Optional explicit host override.

<!-- ha-marketplace:exclude-start -->
## Install via Marketplace or HACS

Once Popur S7 is listed in the catalog:

**Home Assistant 2026.11 or later:** Open **Settings → Marketplace**, search for **Popur S7**, select **Install**, and follow the setup flow.

**Home Assistant 2026.8–2026.10:** Search for **Popur S7** in HACS and install it. Restart Home Assistant, then add **Popur S7** under **Settings → Devices & services → Add integration**.

[![Open Popur S7 in Home Assistant](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=BookCatKid&repository=popur-s7-ha&category=integration)

## Install manually

```bash
pip install pypopur
cp -r custom_components/popur_s7 /config/custom_components/
```

Restart Home Assistant, then add the integration.

<!-- ha-marketplace:exclude-end -->

## Configuration

| Field | Required | Description |
|-------|----------|-------------|
| **Email** | Yes | Popur app account email |
| **Password** | Yes | Popur app account password |
| **Install ID** | No | App-install identity — leave blank to auto-generate (recommended) |
| **LAN Host** | No | Fixed device IP for single-device setups. Leave blank to let DHCP discovery track IP changes. |

Older versions could save an auto-detected IP as a fixed host. To switch an existing entry back to automatic discovery, choose **Reconfigure** on the Popur S7 integration and clear **Device IP**. Existing fixed hosts are not changed automatically.
| **Poll Interval** | No | LAN poll interval in seconds (default: 60, min: 15) |

## Entities

### Sensors (18 per device)

Machine status, running status, waste drawer level, cat presence, recent activity, cat weight, countdown, daily/total/after-full counts, manual/scheduled/auto counts, use/clean/toilet durations, fault-free time, self-check progress.

### Binary sensors (5)

Bin full, cat present, cleaning, fault, powered.

### Switches (19)

Power, status light, buzzer, anti-interference, auto clean, sentinel, caring, track pet data, auto power cycle, lower speed, reshuffle, auto self-check, notifications, 5 drawer toggles, child lock.

### Buttons (14)

Clean, pause, resume, self-check, sifter open/close, scoop start/pause, drawer open/close, zero bin, recalibrate spin/scale, reboot.

### Selects (3)

Calibration level, device color, reshuffle oscillation.

### Numbers (5)

Clean delay (1–60), radar sensitivity (1–10), radar range (1–5), smooth spread (2–7), cycle count (1–10).

### Per-pet sensors (3 per pet)

Last measured weight, last visit duration, last visit timestamp.

### Event entities

Event entities keep the last event type and its attributes; Home Assistant records the time it was received. They fire **only on live events**: activity and faults come from MQTT pushes, visits from newly fetched cloud records. Polled datapoint snapshots, the first load and MQTT reconnects do not replay activity or fault events. Pet visit events are emitted for newly discovered records after the initial baseline, including during periodic cloud refreshes.

| Entity | Unique ID | Event types | Attributes |
|--------|-----------|-------------|------------|
| Activity (per device) | `<dev_id>_activity` | `cat_exist`, `cat_left`, `cat_done_business`, `manual_clean_completed`, `scheduled_clean_completed`, `automatic_clean_completed`, `bin_full`, `self_check_completed`, `cleaning_started`, `cleaning_paused`, `cleaning_resumed` | `source`, `raw_value`, `received_at` |
| Fault event (per device, diagnostic) | `<dev_id>_fault_event` | `fault_detected`, `fault_cleared` (one event per changed fault bit) | `fault`, `url`, `mask`, `source`, `raw_value`, `received_at` |
| Visit (per pet) | `pet_<pet_id>_visit` | `visit_recorded` | `pet_id`, `weight_g`, `duration_s`, `record_time`, `device_id` |

The activity entity fires on every activity report, even when the same type repeats (each report is a real event). The `Recent activity` sensor is still available and shows the last reported value.

Example automation with the `event.received` trigger:

Replace the example entity IDs below with the actual entity IDs from your Home Assistant installation.

```yaml
- alias: "Litter box: notify when the drawer is full"
  triggers:
    - trigger: event.received
      target:
        entity_id: event.popur_s7_activity
      options:
        event_type:
          - bin_full
  actions:
    - action: notify.send_message
      target:
        entity_id: notify.my_device
      data:
        message: "The litter box waste drawer is full."
```

Alternative with a `state` trigger (works on older releases). The state of an event entity is the timestamp of the last event, so filter on `event_type`, and ignore the transition out of `unavailable` (for example after a restart):

```yaml
- alias: "Litter box: log each visit"
  triggers:
    - trigger: state
      entity_id: event.minou_visit
      not_from:
        - unavailable
  conditions:
    - condition: state
      entity_id: event.minou_visit
      attribute: event_type
      state: visit_recorded
  actions:
    - action: logbook.log
      data:
        name: Minou
        message: >-
          Visit: {{ trigger.to_state.attributes.weight_g }} g,
          {{ trigger.to_state.attributes.duration_s }} s
```

### Diagnostics

Connection sensor — shows `lan` (local control active) or `cloud` (fallback).

## How it works

```
LAN (every poll interval)     ← device state, writes, reads
MQTT (real-time)              ← DP deltas, alerts — instant updates
Cloud (every ~10 min)         ← settings DPs, pets, records
Cloud (on LAN failure)        ← fallback for all operations
```

- **LAN** is the fast path — reads/writes go over the local network when available
- **MQTT** provides real-time push — entities update instantly on state changes, not on poll interval
- **Cloud** fills the gaps — settings DPs the LAN omits, pet profiles/records, firmware info
- **Fallback** — if LAN drops, the transport automatically falls back to cloud and retries LAN periodically

## Pet records

When a cat leaves the litter box (`cat_left` event via MQTT), the integration fetches the latest record ~45 seconds later — near-real-time weight and duration without hammering the cloud API. Each record newer than the last known one for a pet fires that pet's `visit_recorded` event.

## Limitations

- Only tested against one Popur S7 (firmware 4.x) — other devices may differ
- All write operations send real device commands — use with care
- The S7 allows a single LAN session at a time — if the app or another client is connected, the integration falls back to cloud until the session frees up

## License

MIT — same as [pypopur](https://github.com/BookCatKid/pypopur/blob/main/LICENSE).

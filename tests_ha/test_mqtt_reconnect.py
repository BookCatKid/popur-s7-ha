"""MQTT session recovery from the polling tick, and its unload ordering."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant

from .conftest import FakePopur


async def test_failing_poll_still_reconnects_mqtt(
    hass: HomeAssistant, popur: FakePopur, setup_entry
) -> None:
    coordinator = setup_entry.runtime_data.coordinator
    events = popur.events
    assert events is not None
    events.connected = False
    connects: list[int] = []

    async def connect() -> None:
        connects.append(1)
        events.connected = True

    events.connect = connect
    popur.client.refresh = AsyncMock(side_effect=OSError("device unreachable"))

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert connects == [1]
    assert events.connected


async def test_unload_waits_for_a_reconnect_in_its_handshake(
    hass: HomeAssistant, popur: FakePopur, setup_entry
) -> None:
    """A reconnect that finishes after unload closes and detaches its
    session; that must happen before unload returns, not later, when it
    would unsubscribe the topics of the next entry's session."""

    coordinator = setup_entry.runtime_data.coordinator
    events = popur.events
    assert events is not None
    events.connected = False
    release = asyncio.Event()
    ops: list[str] = []

    async def connect() -> None:
        await release.wait()
        events.connected = True
        ops.append("connect")

    async def close() -> None:
        events.connected = False
        ops.append("close")

    events.connect = connect
    events.close = close

    await coordinator.async_refresh()  # starts the reconnect, which blocks
    await asyncio.sleep(0)
    unload = hass.async_create_task(
        hass.config_entries.async_unload(setup_entry.entry_id)
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not unload.done()

    release.set()
    assert await unload
    # The reconnect closed its own session, then unload closed it again;
    # nothing is left to run after unload.
    assert ops == ["connect", "close", "close"]
    assert coordinator._mqtt_reconnect_task.done()

"""Exercise the integration's MQTT recovery against the real pinned SDK.

The normal HA fixtures fake MQTT completely, so they cannot detect when
coordinator polling and the SDK's own link-loss handler both call connect().
"""

from __future__ import annotations

import asyncio
import socket
import threading
from contextlib import suppress
from typing import Any

from homeassistant.core import HomeAssistant
from pypopur.sdk.mqtt_client import MqttWireClient
from pypopur.sdk.mqtt_session import MqttConfigBean, MqttServerManager


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            raise ConnectionError("broker connection closed")
        data.extend(part)
    return bytes(data)


class _LocalMqttBroker:
    """Minimal local MQTT 3.1.1 broker for CONNECT/CONNACK."""

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._connections.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            assert _read_exact(conn, 1) == b"\x10"
            remaining = 0
            multiplier = 1
            while True:
                digit = _read_exact(conn, 1)[0]
                remaining += (digit & 0x7F) * multiplier
                if not digit & 0x80:
                    break
                multiplier *= 128
            _read_exact(conn, remaining)
            conn.sendall(b"\x20\x02\x00\x00")
        except (OSError, ConnectionError, AssertionError):
            return

    def drop_current_connection(self) -> None:
        with self._lock:
            conn = self._connections[-1]
        with suppress(OSError):
            conn.shutdown(socket.SHUT_RDWR)
        conn.close()

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            connections = list(self._connections)
        for conn in connections:
            with suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            conn.close()
        self._listener.close()
        self._thread.join(timeout=2)


class _Credentials:
    def username(self) -> str:
        return "test-user"

    def password(self) -> str:
        return "test-password"

    def user_topic(self) -> str:
        return "test/mb/user"


class _SdkEvents:
    """Only adapt the async integration API; use the real SDK MQTT client."""

    def __init__(self, client: MqttWireClient) -> None:
        self.client = client
        self.poll_connect_started = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self.client.connected

    async def connect(self) -> None:
        self.poll_connect_started.set()
        await asyncio.to_thread(self.client.connect)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)


async def test_polling_does_not_schedule_parallel_sdk_reconnect(
    hass: HomeAssistant, setup_entry: Any, socket_enabled: None
) -> None:
    """The coordinator must not compete with the SDK's reconnect loop.

    pypopur 0.1.4 retries link losses on its reader thread. The SDK now
    serializes connect attempts, but a second HA-managed retry would still
    be redundant and could interfere with recovery. Check that HA does not
    even schedule a competing attempt.
    """
    broker = _LocalMqttBroker()
    attempts = 0
    attempt_lock = threading.Lock()
    reconnect_started = threading.Event()
    release_reconnect = threading.Event()

    def socket_factory(host: str, port: int, timeout: float) -> socket.socket:
        nonlocal attempts
        with attempt_lock:
            attempts += 1
            reconnect = attempts > 1
            if reconnect:
                reconnect_started.set()

        if reconnect:
            if not release_reconnect.wait(timeout=10):
                raise TimeoutError("reconnect was not released")
            # Keep the simulated network unavailable so another client
            # cannot make the retry succeed before the test checks it.
            raise OSError("simulated network outage")
        return socket.create_connection((host, port), timeout=timeout)

    mqtt_client = MqttWireClient(
        MqttServerManager("ha-mqtt-regression"),
        MqttConfigBean(
            client_id="ha-mqtt-regression",
            host="127.0.0.1",
            port=broker.port,
            keep_alive=1,
            time_out=5,
        ),
        _Credentials(),
        sock_factory=socket_factory,
        reconnect_backoff=(0.05, 0.2),
    )
    events = _SdkEvents(mqtt_client)
    coordinator = setup_entry.runtime_data.coordinator

    try:
        await asyncio.to_thread(mqtt_client.connect)
        assert events.connected

        coordinator.events = events
        broker.drop_current_connection()
        assert await asyncio.to_thread(reconnect_started.wait, 5)
        assert not events.connected

        # The SDK is already reconnecting. HA must not schedule an
        # independent retry of the same MQTT session.
        await coordinator.async_refresh()
        assert getattr(coordinator, "_mqtt_reconnect_task", None) is None, (
            "the coordinator scheduled a duplicate connect during SDK recovery"
        )
        assert not events.poll_connect_started.is_set()
        assert attempts == 2  # initial connection + the SDK's own reconnect
    finally:
        coordinator.events = None
        release_reconnect.set()
        try:
            task = getattr(coordinator, "_mqtt_reconnect_task", None)
            if task is not None:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
        finally:
            try:
                await events.close()
            finally:
                try:
                    pinger = mqtt_client._pinger
                    if pinger is not None:
                        await asyncio.to_thread(pinger.join, 2)
                        assert not pinger.is_alive()
                finally:
                    broker.close()

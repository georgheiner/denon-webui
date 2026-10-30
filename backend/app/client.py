"""Hält genau eine Telnet-Verbindung zum Receiver und verteilt Statusänderungen.

- Eine Verbindung für alle Browser (viele Denon-Modelle erlauben nur eine).
- Sendewarteschlange mit Mindestabstand zwischen Befehlen; gleichartige Befehle
  (z. B. absolute Lautstärke) ersetzen sich, solange sie noch nicht gesendet sind.
- Automatischer Reconnect mit Backoff; Heartbeat erkennt tote Verbindungen.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import urllib.request
from collections import OrderedDict
from xml.etree import ElementTree
from collections.abc import Callable

from .protocol import STATUS_QUERIES, ReceiverState, apply_line, build_command

log = logging.getLogger(__name__)

Listener = Callable[[dict], None]

UPNP_DESCRIPTION = "http://{host}:60006/upnp/desc/aios_device/aios_device.xml"


def fetch_model_name(host: str) -> str | None:
    """``<modelName>Denon DRA-800H</modelName>`` → ``"DRA-800H"`` (Hersteller zeigt das Logo)."""
    with urllib.request.urlopen(UPNP_DESCRIPTION.format(host=host), timeout=5) as resp:
        root = ElementTree.fromstring(resp.read())
    for el in root.iter():
        if el.tag.endswith("modelName") and el.text:
            name = el.text.strip()
            return name.removeprefix("Denon ").strip() or name
    return None


class DenonClient:
    def __init__(
        self,
        host: str,
        port: int = 23,
        *,
        command_interval: float = 0.1,
        heartbeat_interval: float = 30.0,
        idle_timeout: float = 90.0,
        connect_timeout: float = 5.0,
        max_backoff: float = 30.0,
        volume_limit: float | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.command_interval = command_interval
        self.heartbeat_interval = heartbeat_interval
        self.idle_timeout = idle_timeout
        self.connect_timeout = connect_timeout
        self.max_backoff = max_backoff

        self.state = ReceiverState()
        if volume_limit is not None:
            self.state.volume_limit = volume_limit
        self._listeners: set[Listener] = set()
        self._pending: OrderedDict[str, str] = OrderedDict()
        self._pending_event = asyncio.Event()
        self._unique = itertools.count()
        self._writer: asyncio.StreamWriter | None = None
        self._last_rx = 0.0
        self._runner: asyncio.Task | None = None
        self._model_task: asyncio.Task | None = None

    # --- öffentliche API ---------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def send(self, cmd: str, value=None) -> None:
        """UI-Befehl einreihen. Wirft CommandError bei ungültigen Werten."""
        if not self.state.connected:
            raise ConnectionError("Keine Verbindung zum Receiver")
        key, raw = build_command(self.state, cmd, value)
        self._enqueue(raw, key)

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="denon-client")
        self._model_task = asyncio.create_task(self._load_model(), name="denon-model")

    async def _load_model(self) -> None:
        """Modellnamen aus der UPnP-Beschreibung lesen (Port 60006). Rein kosmetisch:
        schlägt es fehl, bleibt ``model`` leer und die UI zeigt einen neutralen Titel."""
        while self.state.model is None:
            try:
                model = await asyncio.to_thread(fetch_model_name, self.host)
            except Exception as exc:
                log.debug("Modellname nicht lesbar: %s", exc)
                model = None
            if model:
                self.state.model = model
                self.notify()
                return
            await asyncio.sleep(60)

    async def stop(self) -> None:
        if self._model_task:
            self._model_task.cancel()
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass
        await self._close()

    # --- intern --------------------------------------------------------------

    def _enqueue(self, raw: str, key: str | None = None) -> None:
        if key is None:
            key = f"#{next(self._unique)}"
        self._pending.pop(key, None)  # ersetzte Befehle ans Ende rücken
        self._pending[key] = raw
        self._pending_event.set()

    def notify(self) -> None:
        snapshot = self.state.to_dict()
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # ein kaputter Listener darf den Client nicht stoppen
                log.exception("Listener-Fehler")

    def _set_connected(self, connected: bool) -> None:
        if self.state.connected != connected:
            self.state.connected = connected
            self.notify()

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), self.connect_timeout
                )
            except (OSError, asyncio.TimeoutError) as exc:
                log.warning("Verbindung zu %s:%s fehlgeschlagen: %s – neuer Versuch in %.0fs",
                            self.host, self.port, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue

            log.info("Verbunden mit %s:%s", self.host, self.port)
            backoff = 1.0
            self._writer = writer
            self._last_rx = asyncio.get_running_loop().time()
            self._pending.clear()
            for query in STATUS_QUERIES:
                self._enqueue(query)
            self._set_connected(True)

            tasks = [
                asyncio.create_task(self._read_loop(reader)),
                asyncio.create_task(self._write_loop(writer)),
                asyncio.create_task(self._heartbeat_loop()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception():
                        log.warning("Verbindung unterbrochen: %s", task.exception())
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self._close()
                self._set_connected(False)
            await asyncio.sleep(backoff)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.readuntil(b"\r")
            self._last_rx = asyncio.get_running_loop().time()
            line = data.decode("ascii", errors="replace")
            log.debug("<- %r", line)
            if apply_line(self.state, line):
                self.notify()

    async def _write_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            await self._pending_event.wait()
            while self._pending:
                _, raw = self._pending.popitem(last=False)
                log.debug("-> %r", raw)
                writer.write((raw + "\r").encode("ascii"))
                await writer.drain()
                await asyncio.sleep(self.command_interval)
            self._pending_event.clear()

    async def _heartbeat_loop(self) -> None:
        """Fragt regelmäßig den Power-Status ab; bleibt jede Antwort aus, Verbindung neu aufbauen.

        Fängt auch den beim DRA-800H beobachteten Fall ab, dass die erste Abfrage nach
        längerer Ruhe unbeantwortet bleibt: dann wird der Status nach kurzer Zeit erneut geholt.
        """
        loop = asyncio.get_running_loop()
        await asyncio.sleep(3)
        if self.state.power is None:
            log.info("Keine Antwort auf Statusabfrage, wiederhole")
            for query in STATUS_QUERIES:
                self._enqueue(query, key=f"Q:{query}")
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            if loop.time() - self._last_rx > self.idle_timeout:
                raise ConnectionError("Keine Daten vom Receiver – Verbindung gilt als tot")
            self._enqueue("PW?", key="Q:PW?")

    async def _close(self) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

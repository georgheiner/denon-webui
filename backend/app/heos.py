"""HEOS CLI (Port 1255): liefert "Was läuft gerade" (Titel, Interpret, Zuspielung wie Bluetooth)
und steuert die Wiedergabe (Play/Pause, Vor/Zurück) wie die Transporttasten der Fernbedienung.

Der Receiver schickt nach ``register_for_change_events`` Ereignisse, wenn sich
Titel oder Wiedergabestatus ändern; daraufhin wird der aktuelle Stand neu abgefragt.
Antworten sind JSON-Zeilen mit ``\\r\\n``; Anfrage und Antwort werden nicht gepaart,
jede Zeile wird anhand ihres ``command`` ausgewertet.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace

from .protocol import CommandError, NowPlaying, ReceiverState

log = logging.getLogger(__name__)

HEOS_PORT = 1255
# Zuspielungen, die als Quelle angezeigt werden (HEOS meldet sie als "mid")
KNOWN_SOURCES = {"Bluetooth", "AirPlay"}
UPDATE_CHECK_INTERVAL = 6 * 3600  # Sekunden; Firmware-Update-Prüfung nur gelegentlich


def parse_now_playing(payload: dict) -> NowPlaying:
    mid = payload.get("mid") or ""
    title = (payload.get("song") or payload.get("station") or "").strip()
    return NowPlaying(
        source=mid if mid in KNOWN_SOURCES else None,
        title=title or None,
        artist=(payload.get("artist") or "").strip() or None,
        album=(payload.get("album") or "").strip() or None,
    )


def parse_message(message: str) -> dict[str, str]:
    """``"pid=1&state=play"`` → ``{"pid": "1", "state": "play"}``."""
    return dict(part.split("=", 1) for part in message.split("&") if "=" in part)


class HeosClient:
    def __init__(
        self,
        host: str,
        state: ReceiverState,
        notify: Callable[[], None],
        *,
        port: int = HEOS_PORT,
        heartbeat_interval: float = 30.0,
        max_backoff: float = 60.0,
    ) -> None:
        self.host = host
        self.port = port
        self.state = state
        self.notify = notify
        self.heartbeat_interval = heartbeat_interval
        self.max_backoff = max_backoff
        self.pid: int | None = None
        self._play_state: str | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._runner: asyncio.Task | None = None

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="heos-client")

    async def stop(self) -> None:
        if self._runner:
            self._runner.cancel()
            try:
                await self._runner
            except asyncio.CancelledError:
                pass

    COMMANDS = ("play_state", "next", "previous")

    def control(self, cmd: str, value=None) -> None:
        """UI-Befehl an den HEOS-Player. Wirft CommandError/ConnectionError wie DenonClient.send."""
        if self._writer is None or self.pid is None:
            raise ConnectionError("Keine Verbindung zu HEOS")
        if cmd == "play_state":
            if value not in ("play", "pause", "stop"):
                raise CommandError("play_state erwartet 'play', 'pause' oder 'stop'")
            self._send(f"player/set_play_state?pid={self.pid}&state={value}")
        elif cmd == "next":
            self._send(f"player/play_next?pid={self.pid}")
        elif cmd == "previous":
            self._send(f"player/play_previous?pid={self.pid}")
        else:
            raise CommandError(f"Unbekannter Befehl: {cmd!r}")

    def _send(self, command: str) -> None:
        if self._writer is not None:
            log.debug("HEOS -> %s", command)
            self._writer.write(f"heos://{command}\r\n".encode())

    def _set_now_playing(self, now_playing: NowPlaying | None) -> None:
        if self.state.now_playing != now_playing:
            self.state.now_playing = now_playing
            self.notify()

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 5)
            except (OSError, asyncio.TimeoutError) as exc:
                log.info("HEOS nicht erreichbar (%s), neuer Versuch in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue
            backoff = 1.0
            self._writer = writer
            self._send("system/register_for_change_events?enable=on")
            self._send("player/get_players")
            heartbeat = asyncio.create_task(self._heartbeat())
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("HEOS-Verbindung geschlossen")
                    self._handle(line)
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as exc:
                log.info("HEOS-Verbindung unterbrochen: %s", exc)
            finally:
                heartbeat.cancel()
                self._writer = None
                writer.close()
                self._set_now_playing(None)
            await asyncio.sleep(backoff)

    async def _heartbeat(self) -> None:
        beats = 0
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            self._send("system/heart_beat")
            beats += 1
            if beats * self.heartbeat_interval >= UPDATE_CHECK_INTERVAL:
                beats = 0
                self._check_update()

    def _check_update(self) -> None:
        if self.pid is not None:
            self._send(f"player/check_update?pid={self.pid}")

    def _refresh(self) -> None:
        if self.pid is not None:
            self._send(f"player/get_now_playing_media?pid={self.pid}")
            self._send(f"player/get_play_state?pid={self.pid}")

    def _handle(self, raw: bytes) -> None:
        try:
            data = json.loads(raw)
            heos = data["heos"]
        except (ValueError, KeyError):
            return
        command = heos.get("command", "")
        message = parse_message(heos.get("message", ""))
        log.debug("HEOS <- %s %s", command, heos.get("message", ""))

        if command.startswith("event/"):
            if command in ("event/player_now_playing_changed", "event/player_state_changed",
                           "event/players_changed"):
                if command == "event/players_changed":
                    self._send("player/get_players")
                elif command == "event/player_state_changed" and "state" in message:
                    self._update_play_state(message["state"])
                else:
                    self._refresh()
            return
        if heos.get("result") == "fail":
            log.warning("HEOS lehnt %s ab: %s", command, heos.get("message"))
            if command.startswith("player/set_play_state") or command.startswith("player/play_"):
                self._refresh()  # UI auf den echten Stand zurückholen
            return
        if heos.get("result") != "success" or "command under process" in message:
            return

        if command == "player/get_players":
            players = data.get("payload") or []
            match = next((p for p in players if p.get("ip") == self.host), players[0] if players else None)
            self.pid = match and match.get("pid")
            if match and match.get("version") and self.state.heos_version != match["version"]:
                self.state.heos_version = match["version"]
                self.notify()
            self._refresh()
            self._check_update()
        elif command == "player/get_now_playing_media":
            payload = data.get("payload") or {}
            np = parse_now_playing(payload) if payload else None
            if np:
                np.state = self._play_state
            self._set_now_playing(np)
        elif command == "player/check_update":
            update = (data.get("payload") or {}).get("update")
            available = {"update_exist": True, "update_none": False}.get(update)
            if self.state.update_available != available:
                self.state.update_available = available
                self.notify()
        elif command == "player/get_play_state" and "state" in message:
            self._update_play_state(message["state"])

    def _update_play_state(self, play_state: str) -> None:
        # Getrennt merken: der Status kann vor den Titeldaten eintreffen
        self._play_state = play_state
        current = self.state.now_playing
        if current is not None and current.state != play_state:
            self._set_now_playing(replace(current, state=play_state))

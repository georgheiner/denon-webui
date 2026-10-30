"""Philips-Fernseher über jointSPACE (HTTP-JSON, Port 1925, API-Version 1, ohne Anmeldung).

Getestet mit dem 37PFL9606K/02 (Software Q5551-0.14.99.0). Der Fernseher meldet nichts von
selbst, deshalb wird der Stand regelmäßig abgefragt. Im Standby schaltet er sein Netzwerk ab:
Ausschalten geht über die Taste ``Standby``, Einschalten über das Netzwerk nicht.

Ambilight: Modus ``internal`` folgt dem Bild, ``manual`` zeigt die Farben aus
``ambilight/cached`` – eine Farbe für alle LEDs oder je LED (Seite → Index → r/g/b).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from collections.abc import Callable

from .protocol import CommandError, ReceiverState, TvState

log = logging.getLogger(__name__)

TV_PORT = 1925

# UI-Taste → jointSPACE-Tastencode (POST /1/input/key)
TV_KEYS = {
    "standby": "Standby",
    "up": "CursorUp",
    "down": "CursorDown",
    "left": "CursorLeft",
    "right": "CursorRight",
    "ok": "Confirm",
    "back": "Back",
    "home": "Home",
    "options": "Options",
    "ambilight": "AmbilightOnOff",
}

# Anzeigenamen; der Fernseher meldet englische ("Watch TV", "HDMI Side")
SOURCE_NAMES = {
    "tv": "Antenne",
    "sat": "Satellit",
    "hdmi1": "HDMI 1",
    "hdmi2": "HDMI 2",
    "hdmi3": "HDMI 3",
    "hdmi4": "HDMI 4",
    "hdmiside": "HDMI seitlich",
    "ext1": "SCART",
    "ext2": "SCART 2",
    "ypbpr": "YPbPr",
    "vga": "VGA",
}


# Eigene Szenen als Verlauf (oben, unten): die Atmosphären des Fernsehers sind über
# jointSPACE nicht erreichbar, deshalb nachgebaut
SCENES = {
    "tiefsee": ((0, 40, 160), (0, 170, 170)),
    "lava": ((200, 0, 0), (255, 140, 0)),
    "natur": ((110, 200, 40), (200, 170, 30)),
    "leselicht": ((255, 170, 90), (255, 170, 90)),
}
# Gemessen am 37PFL9606K/02; wird beim Verbinden neu gelesen
DEFAULT_TOPOLOGY = {"left": 4, "top": 7, "right": 4, "bottom": 0}
HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> dict[str, int]:
    return dict(zip("rgb", (round(x + (y - x) * t) for x, y in zip(a, b))))


def scene_colors(scene: str, topology: dict[str, int]) -> dict:
    """Verlauf für ``ambilight/cached``: oben die obere Farbe, an den Seiten nach unten zur
    unteren. Nummerierung der Seiten im Uhrzeigersinn ab unten links (links unten → oben,
    rechts oben → unten)."""
    top, bottom = SCENES[scene]
    layer = {}
    for side in ("left", "right"):
        n = topology.get(side, 0)
        # t = 0 oben, 1 unten
        layer[side] = {
            str(i): _mix(top, bottom, ((n - 1 - i) if side == "left" else i) / max(n - 1, 1))
            for i in range(n)
        }
    layer["top"] = {str(i): _mix(top, bottom, 0) for i in range(topology.get("top", 0))}
    layer["bottom"] = {str(i): _mix(top, bottom, 1) for i in range(topology.get("bottom", 0))}
    return {"layer1": {side: leds for side, leds in layer.items() if leds}}


class TvClient:
    COMMANDS = ("tv_key", "tv_source", "tv_ambilight")

    def __init__(
        self,
        host: str,
        state: ReceiverState,
        notify: Callable[[], None],
        *,
        port: int = TV_PORT,
        poll_interval: float = 5.0,
        timeout: float = 2.0,
    ) -> None:
        self.base = f"http://{host}:{port}/1"
        self.state = state
        self.notify = notify
        self.poll_interval = poll_interval
        self.timeout = timeout
        state.tv = TvState()
        self._poll_now = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._posts: set[asyncio.Task] = set()
        self._topology = dict(DEFAULT_TOPOLOGY)
        self._ambilight_target: str | None = None  # neuester Wunsch; ältere werden übersprungen
        self._ambilight_task: asyncio.Task | None = None

    @property
    def tv(self) -> TvState:
        return self.state.tv

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="tv-client")

    async def stop(self) -> None:
        for task in [self._runner, self._ambilight_task, *self._posts]:
            if task:
                task.cancel()
        if self._runner:
            try:
                await self._runner
            except asyncio.CancelledError:
                pass

    def control(self, cmd: str, value=None) -> None:
        """UI-Befehl an den Fernseher. Wirft CommandError/ConnectionError wie DenonClient.send."""
        if cmd == "tv_key":
            if value not in TV_KEYS:
                raise CommandError(f"Unbekannte Fernseher-Taste: {value!r}")
            path, body = "input/key", {"key": TV_KEYS[value]}
        elif cmd == "tv_source":
            if value not in {s["code"] for s in self.tv.sources}:
                raise CommandError(f"Unbekannter Fernseher-Eingang: {value!r}")
            path, body = "sources/current", {"id": value}
        elif cmd == "tv_ambilight":
            self._check_ambilight(value)
            if not self.tv.connected:
                raise ConnectionError("Fernseher ist aus oder nicht erreichbar")
            self._set_ambilight(value)
            return
        else:
            raise CommandError(f"Unbekannter Befehl: {cmd!r}")
        if not self.tv.connected:
            raise ConnectionError("Fernseher ist aus oder nicht erreichbar")
        task = asyncio.get_running_loop().create_task(self._post(path, body))
        self._posts.add(task)
        task.add_done_callback(self._posts.discard)

    @staticmethod
    def _check_ambilight(value) -> None:
        """``"follow"`` (folgt dem Bild), ``"#rrggbb"`` (eine Farbe) oder ``"scene:<name>"``."""
        if value == "follow" or (isinstance(value, str) and HEX_COLOR.fullmatch(value)):
            return
        if isinstance(value, str) and value.startswith("scene:") and value[6:] in SCENES:
            return
        raise CommandError(f"Unbekannte Ambilight-Einstellung: {value!r}")

    def _set_ambilight(self, value: str) -> None:
        # Beim Ziehen im Farbfeld kommen viele Werte; gesendet wird immer nur der neueste
        self._ambilight_target = value
        self.tv.ambilight = None if value == "follow" else value
        self.tv.ambilight_mode = "internal" if value == "follow" else "manual"
        self.notify()
        if self._ambilight_task is None or self._ambilight_task.done():
            self._ambilight_task = asyncio.get_running_loop().create_task(self._send_ambilight())

    async def _send_ambilight(self) -> None:
        mode = None  # Modus nur bei Wechsel setzen, nicht bei jeder Farbe
        while self._ambilight_target is not None:
            value, self._ambilight_target = self._ambilight_target, None
            try:
                if value == "follow":
                    await asyncio.to_thread(self._request, "ambilight/mode", {"current": "internal"})
                    mode = "internal"
                    continue
                if mode != "manual":
                    await asyncio.to_thread(self._request, "ambilight/mode", {"current": "manual"})
                    mode = "manual"
                if value.startswith("#"):
                    rgb = int(value[1:], 16)
                    body = {"r": rgb >> 16, "g": (rgb >> 8) & 0xFF, "b": rgb & 0xFF}
                else:
                    body = scene_colors(value[6:], self._topology)
                await asyncio.to_thread(self._request, "ambilight/cached", body)
            except OSError as exc:
                log.warning("Ambilight nicht gesetzt: %s", exc)

    # --- intern --------------------------------------------------------------

    def _request(self, path: str, body: dict | None = None) -> dict | None:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}/{path}", data=data, method="POST" if data else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        # POST antwortet mit einer kleinen HTML-Seite "Ok"
        return json.loads(raw) if body is None else None

    async def _post(self, path: str, body: dict) -> None:
        log.debug("TV -> %s %s", path, body)
        try:
            await asyncio.to_thread(self._request, path, body)
        except OSError as exc:
            log.warning("Fernseher lehnt %s ab: %s", path, exc)
        await asyncio.sleep(0.3)
        self._poll_now.set()  # neuen Stand gleich holen statt bis zur nächsten Runde zu warten

    async def _run(self) -> None:
        while True:
            await self._poll()
            try:
                await asyncio.wait_for(self._poll_now.wait(), self.poll_interval)
            except asyncio.TimeoutError:
                pass
            self._poll_now.clear()

    async def _poll(self) -> None:
        tv = self.tv
        before = (tv.connected, tv.model, tv.source, list(tv.sources), tv.ambilight_mode, tv.ambilight)
        try:
            if tv.model is None:
                system = await asyncio.to_thread(self._request, "system")
                tv.model = system.get("model")
                topology = await asyncio.to_thread(self._request, "ambilight/topology")
                self._topology = {side: int(topology.get(side, 0)) for side in DEFAULT_TOPOLOGY}
            if not tv.sources:
                sources = await asyncio.to_thread(self._request, "sources")
                tv.sources = [
                    {"code": code, "name": SOURCE_NAMES.get(code) or info.get("name") or code}
                    for code, info in sources.items()
                ]
            current = await asyncio.to_thread(self._request, "sources/current")
            tv.source = current.get("id")
            if self._ambilight_task is None or self._ambilight_task.done():  # nicht mitten im Setzen
                mode = await asyncio.to_thread(self._request, "ambilight/mode")
                tv.ambilight_mode = mode.get("current")
                if tv.ambilight_mode == "internal":
                    tv.ambilight = None  # z. B. am Fernseher zurückgestellt
            if not tv.connected:
                log.info("Fernseher erreichbar (%s)", tv.model)
            tv.connected = True
        except (OSError, ValueError, AttributeError) as exc:
            if tv.connected:
                log.info("Fernseher nicht mehr erreichbar: %s", exc)
            tv.connected = False
            tv.source = None
        if (tv.connected, tv.model, tv.source, tv.sources, tv.ambilight_mode, tv.ambilight) != before:
            self.notify()

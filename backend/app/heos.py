"""HEOS CLI (Port 1255): liefert "Was läuft gerade" (Titel, Interpret, Zuspielung wie Bluetooth)
und steuert die Wiedergabe (Play/Pause, Vor/Zurück) wie die Transporttasten der Fernbedienung.

Der Receiver schickt nach ``register_for_change_events`` Ereignisse, wenn sich
Titel oder Wiedergabestatus ändern; daraufhin wird der aktuelle Stand neu abgefragt.
Antworten sind JSON-Zeilen mit ``\\r\\n``; Anfrage und Antwort werden nicht gepaart,
jede Zeile wird anhand ihres ``command`` ausgewertet.

Bluetooth liefert kein Cover (``image_url`` leer), als Dauer Unsinn (``duration=2000`` oder ``0``)
und als ``cur_pos`` die Zeit seit Beginn der Bluetooth-Wiedergabe – sie läuft über Titelwechsel
hinweg weiter. Bei Bluetooth zählt das Backend die Position deshalb selbst ab dem Titelwechsel. Cover und Dauer werden dann optional über die iTunes-Suche nachgeschlagen
(schickt Interpret/Titel/Album an Apple; abschaltbar mit ``COVER_LOOKUP=off``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import replace

from .protocol import CommandError, NowPlaying, ReceiverState

log = logging.getLogger(__name__)

HEOS_PORT = 1255
# Zuspielungen, die als Quelle angezeigt werden (HEOS meldet sie als "mid")
KNOWN_SOURCES = {"Bluetooth", "AirPlay"}
UPDATE_CHECK_INTERVAL = 6 * 3600  # Sekunden; Firmware-Update-Prüfung nur gelegentlich


def parse_now_playing(payload: dict) -> NowPlaying:
    """AirPlay meldet der DRA-800H als lokale Quelle (``sid`` 1024) mit einer Zahl als ``mid``
    (Bluetooth: ``mid: "Bluetooth"``). Direkt nach dem Umschalten sind alle Felder leer, später
    kommen Titel/Interpret/Album. Sein eigenes Cover (``…:8015/local-server/albumart/…``) ist nur
    halb gespeichert (untere Hälfte grau) → verworfen, stattdessen wird nachgeschlagen."""
    mid = str(payload.get("mid") or "")
    if payload.get("sid") == 1024 and mid.isdigit():
        mid = "AirPlay"
    image_url = (payload.get("image_url") or "").strip()
    if "/local-server/albumart/" in image_url:
        image_url = ""
    title = (payload.get("song") or payload.get("station") or "").strip()
    return NowPlaying(
        source=mid if mid in KNOWN_SOURCES else None,
        title=title or None,
        artist=(payload.get("artist") or "").strip() or None,
        album=(payload.get("album") or "").strip() or None,
        image_url=image_url or None,
    )


def parse_progress(message: dict[str, str]) -> tuple[float, float | None] | None:
    """``cur_pos``/``duration`` in ms → Sekunden; Dauer None, wenn sie nicht zur Position passt."""
    try:
        pos, dur = int(message["cur_pos"]) / 1000, int(message.get("duration") or 0) / 1000
    except (KeyError, ValueError):
        return None
    return pos, dur if dur > 0 and dur >= pos else None


ITUNES = "https://itunes.apple.com/"


def _itunes(params: dict, endpoint: str = "search") -> list[dict]:
    with urllib.request.urlopen(f"{ITUNES}{endpoint}?{urllib.parse.urlencode(params)}", timeout=5) as resp:
        return json.load(resp).get("results", [])


def _norm(text: str | None) -> str:
    """Vergleichsform: ohne Klammerzusätze wie „(Deluxe)“, ohne Groß/klein und Satzzeichen."""
    text = re.sub(r"[(\[].*?[)\]]", "", text or "").casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def _same(a: str | None, b: str | None) -> bool:
    return bool(_norm(a)) and _norm(a) == _norm(b)


def _contains(whole: str | None, part: str | None) -> bool:
    """Interpret „KALIM & Haftbefehl“ passt zu „Haftbefehl“."""
    return bool(_norm(part)) and _norm(part) in _norm(whole)


def _large(url: str) -> str:
    return url.replace("100x100bb", "600x600bb")


def _seconds(result: dict) -> float | None:
    ms = result.get("trackTimeMillis")
    return ms / 1000 if isinstance(ms, (int, float)) and ms > 0 else None


def lookup_track(artist: str | None, title: str | None, album: str | None = None) -> tuple[str | None, float | None]:
    """(Cover-URL, Dauer in s) per iTunes-Suche – Bluetooth liefert beides nicht. Nur bei eindeutig
    passendem Namen (lieber nichts als etwas Falsches); die Dauer nur, wenn der Titel selbst passt.

    1. Podcast: Sendung heißt wie der Interpret; Folge mit gleichem Titel liefert die Dauer.
    2. Musik über die Songsuche „Interpret Titel“/„Interpret Album“: gleicher Titel aus gleichem
       Album, sonst gleiches Album (nur Cover), sonst gleicher Titel. Die Albumsuche der API findet
       manche Alben gar nicht, der erste Songtreffer ist oft ein anderer Song."""
    if not artist:
        return None, None
    show = next((r for r in _itunes({"term": artist, "media": "podcast", "limit": 5, "country": "DE"})
                 if _same(r.get("collectionName"), artist) and r.get("artworkUrl600")), None)
    if show:
        duration = None
        if title:
            episodes = _itunes({"term": title, "media": "podcast", "entity": "podcastEpisode", "limit": 10, "country": "DE"})
            episode = next((r for r in episodes if _same(r.get("trackName"), title)
                            and _same(r.get("collectionName"), artist)), None)
            duration = episode and _seconds(episode)
        return show["artworkUrl600"], duration
    terms = [f"{artist} {t}" for t in (title, album) if t]
    for term in dict.fromkeys(terms):
        results = [r for r in _itunes({"term": term, "media": "music", "entity": "song", "limit": 25, "country": "DE"})
                   if _contains(r.get("artistName"), artist) and r.get("artworkUrl100")]
        same_title = lambda r: _same(r.get("trackName"), title)
        same_album = lambda r: _same(r.get("collectionName"), album)
        for match in (lambda r: same_title(r) and same_album(r), same_album, same_title):
            hit = next((r for r in results if match(r)), None)
            if hit:
                return _large(hit["artworkUrl100"]), _seconds(hit) if same_title(hit) else None
    return _lookup_via_artist(artist, title, album) if album else (None, None)


def _lookup_via_artist(artist: str, title: str | None, album: str) -> tuple[str | None, float | None]:
    """Letzter Weg: Künstler → seine Alben → Album mit gleichem Namen → dessen Titel. Die Suche
    „SSIO BWL“ liefert nichts, das Album „Alles oder Nix“ steht aber im Katalog."""
    main_artist = re.split(r"\s*(?:&|,|\bfeat\.?|\bx\b)\s*", artist, flags=re.I)[0]
    found = next((r for r in _itunes({"term": main_artist, "entity": "musicArtist", "limit": 5, "country": "DE"})
                  if _same(r.get("artistName"), main_artist)), None)
    if not found:
        return None, None
    albums = _itunes({"id": found["artistId"], "entity": "album", "limit": 200, "country": "DE"}, "lookup")
    hit = next((r for r in albums if r.get("wrapperType") == "collection" and _same(r.get("collectionName"), album)
                and r.get("artworkUrl100")), None)
    if not hit:
        return None, None
    tracks = _itunes({"id": hit["collectionId"], "entity": "song", "country": "DE"}, "lookup")
    track = next((r for r in tracks if r.get("wrapperType") == "track" and _same(r.get("trackName"), title)), None)
    return _large(hit["artworkUrl100"]), track and _seconds(track)


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
        cover_lookup: bool = True,
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
        self.cover_lookup = cover_lookup
        self._covers: dict[tuple, tuple] = {}  # (Interpret, Titel, Album) → (Cover, Dauer); auch Fehltreffer
        self._cover_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="heos-client")

    async def stop(self) -> None:
        if self._cover_task:
            self._cover_task.cancel()
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
            got_line = False
            self._writer = writer
            self._send("system/register_for_change_events?enable=on")
            self._send("player/get_players")
            heartbeat = asyncio.create_task(self._heartbeat())
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("HEOS-Verbindung geschlossen")
                    got_line = True
                    self._handle(line)
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as exc:
                log.info("HEOS-Verbindung unterbrochen: %s", exc)
            finally:
                heartbeat.cancel()
                self._writer = None
                writer.close()
                self._set_now_playing(None)
            # wie beim Telnet-Client: sofort geschlossene Verbindungen verlängern die Pause
            backoff = 1.0 if got_line else min(backoff * 2, self.max_backoff)
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
            if command == "event/player_now_playing_progress":
                self._update_progress(message)
            elif command in ("event/player_now_playing_changed", "event/player_state_changed",
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
                current = self.state.now_playing
                if current and (current.title, current.artist) == (np.title, np.artist):
                    # gleicher Titel: Fortschritt und nachgeschlagenes Cover behalten
                    np.position, np.position_at, np.duration = current.position, current.position_at, current.duration
                    np.image_url = np.image_url or current.image_url
                    np.duration = np.duration or current.duration
                else:
                    if current:
                        # Titelwechsel miterlebt: ab 0 mitzählen, bis der Receiver Fortschritt meldet.
                        # Beim ersten Titel nach dem Verbinden ist die Position unbekannt.
                        np.position, np.position_at = 0.0, time.monotonic()
                    self._find_cover(np)
            self._set_now_playing(np)
        elif command == "player/check_update":
            update = (data.get("payload") or {}).get("update")
            available = {"update_exist": True, "update_none": False}.get(update)
            if self.state.update_available != available:
                self.state.update_available = available
                self.notify()
        elif command == "player/set_play_state" and "state" in message:
            # Bei AirPlay greift der Befehl, get_play_state meldet aber weiter "play" →
            # die Bestätigung des Befehls zählt
            self._update_play_state(message["state"])
        elif command == "player/get_play_state" and "state" in message:
            self._update_play_state(message["state"])

    def _update_progress(self, message: dict[str, str]) -> None:
        progress = parse_progress(message)
        current = self.state.now_playing
        # Bluetooth/AirPlay: cur_pos ist nicht die Position im Titel, Dauer 0 oder Unsinn → ignorieren
        if progress and progress[1] and current is not None and current.source != "Bluetooth":
            # Unbrauchbare Dauer vom Receiver ersetzt keine nachgeschlagene
            self._set_now_playing(replace(current, position=progress[0], duration=progress[1] or current.duration,
                                          position_at=time.monotonic()))

    def _find_cover(self, np: NowPlaying) -> None:
        """Cover und Dauer ergänzen, die HEOS nicht liefert (sofort aus dem Merkzettel oder im Hintergrund)."""
        if np.image_url and np.duration:
            return
        key = (np.artist, np.title, np.album)
        if key in self._covers:
            self._apply_lookup(np, *self._covers[key])
            return
        if not self.cover_lookup or not np.artist:
            return
        if self._cover_task:
            self._cover_task.cancel()
        self._cover_task = asyncio.get_running_loop().create_task(self._load_cover(key))

    @staticmethod
    def _apply_lookup(np: NowPlaying, image_url: str | None, duration: float | None) -> None:
        np.image_url = np.image_url or image_url
        np.duration = np.duration or duration

    async def _load_cover(self, key: tuple) -> None:
        try:
            found = await asyncio.to_thread(lookup_track, *key)
        except (OSError, ValueError) as exc:
            log.info("Cover nicht gefunden (%s): %s", key[0], exc)
            return  # nicht merken: beim nächsten Titelwechsel neu versuchen
        if len(self._covers) > 200:
            self._covers.clear()
        self._covers[key] = found
        current = self.state.now_playing
        if current and (current.artist, current.title, current.album) == key:
            updated = replace(current)
            self._apply_lookup(updated, *found)
            self._set_now_playing(updated)

    def _update_play_state(self, play_state: str) -> None:
        # Getrennt merken: der Status kann vor den Titeldaten eintreffen
        self._play_state = play_state
        current = self.state.now_playing
        if current is not None and current.state != play_state:
            # Position beim Anhalten einfrieren, beim Weiterspielen ab jetzt weiterzählen
            self._set_now_playing(replace(current, state=play_state, position=current.position_now(),
                                          position_at=time.monotonic()))

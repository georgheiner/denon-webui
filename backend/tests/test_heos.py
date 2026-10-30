"""HEOS-Client gegen einen nachgebauten HEOS-Dienst; Payloads wie vom DRA-800H gemeldet."""

import asyncio
import json

import pytest

from app import heos as heos_module
from app.heos import HeosClient, lookup_track, parse_message, parse_now_playing, parse_progress
from app.protocol import CommandError, ReceiverState

BLUETOOTH_PAYLOAD = {
    "type": "station", "song": "Folge 12", "station": "Folge 12", "album": "27. Oktober 2026",
    "artist": "Podcast", "image_url": "", "album_id": "1", "mid": "Bluetooth", "qid": 1, "sid": 1024,
}


def test_parse_now_playing_bluetooth():
    np = parse_now_playing(BLUETOOTH_PAYLOAD)
    assert (np.source, np.title, np.artist, np.album) == ("Bluetooth", "Folge 12", "Podcast", "27. Oktober 2026")


def test_empty_metadata_is_airplay():
    # Gemessen am DRA-800H mit AirPlay vom iPhone
    payload = {"type": "station", "song": "", "station": "", "album": "", "artist": "", "image_url": "",
               "album_id": "1", "mid": "1", "qid": 1, "sid": 1024}
    np = parse_now_playing(payload)
    assert (np.source, np.title, np.artist) == ("AirPlay", None, None)


def test_airplay_with_metadata_drops_half_stored_cover():
    payload = {"type": "station", "song": "63 (feat. Jamule)", "station": "63 (feat. Jamule)", "album": "NULL AUF HUNDERT",
               "artist": "KALIM", "image_url": "http://192.168.0.162:8015/local-server/albumart/6bbdc8545d8d127b9a2bb49620fb102e",
               "album_id": "1", "mid": "5277906", "qid": 1, "sid": 1024}
    np = parse_now_playing(payload)
    assert (np.source, np.title, np.artist, np.image_url) == ("AirPlay", "63 (feat. Jamule)", "KALIM", None)


def test_unknown_mid_is_not_shown_as_source():
    assert parse_now_playing({**BLUETOOTH_PAYLOAD, "mid": "s123456"}).source is None


def test_control_without_connection_raises():
    heos = HeosClient("127.0.0.1", ReceiverState(), lambda: None)
    with pytest.raises(ConnectionError):
        heos.control("next")


def test_parse_progress():
    # Bluetooth vom DRA-800H: Dauer 2 s bei Position 85 s → Dauer unbekannt
    assert parse_progress({"cur_pos": "85000", "duration": "2000"}) == (85.0, None)
    assert parse_progress({"cur_pos": "30000", "duration": "240000"}) == (30.0, 240.0)
    assert parse_progress({"cur_pos": "x"}) is None


def test_lookup_track_prefers_matching_podcast(monkeypatch):
    calls = []

    def fake_itunes(params):
        calls.append(params["media"])
        if params["media"] == "podcast":
            return [{"collectionName": "Ganz anderer Podcast", "artworkUrl600": "falsch.jpg"},
                    {"collectionName": "Dark Agent - Im Netz der Geheimdienste", "artworkUrl600": "richtig.jpg"}]
        return []

    monkeypatch.setattr(heos_module, "_itunes", fake_itunes)
    assert lookup_track("Dark Agent - Im Netz der Geheimdienste", "Folge 6") == ("richtig.jpg", None)
    assert calls == ["podcast", "podcast"]  # Sendung, dann Folge


def test_lookup_track_matches_album_not_first_hit(monkeypatch):
    # Echte Suche nach „Haftbefehl 069“: erster Treffer ist ein anderer Song von einem anderen Album
    def fake_itunes(params):
        if params["media"] == "podcast":
            return [{"collectionName": "Haftbefehl – Die Doku", "artworkUrl600": "doku.jpg"}]
        return [{"trackName": "RADW", "artistName": "Haftbefehl", "collectionName": "Das weisse Album",
                 "artworkUrl100": "weiss/100x100bb.jpg"},
                {"trackName": "CopKKKilla", "artistName": "Haftbefehl", "collectionName": "Unzensiert (Deluxe)",
                 "artworkUrl100": "unzensiert/100x100bb.jpg"}]

    monkeypatch.setattr(heos_module, "_itunes", fake_itunes)
    # Nur das Album passt: Cover ja, Dauer nein (gehört zu einem anderen Song)
    assert lookup_track("Haftbefehl", "069", "Unzensiert (Deluxe)") == ("unzensiert/600x600bb.jpg", None)
    # Weder Titel noch Album passen → kein Cover statt eines falschen
    assert lookup_track("Haftbefehl", "069") == (None, None)


def test_lookup_track_song_with_featured_artist(monkeypatch):
    monkeypatch.setattr(heos_module, "_itunes", lambda p: [] if p["media"] == "podcast" else
                        [{"trackName": "OZEAN", "artistName": "KALIM & Haftbefehl", "artworkUrl100": "https://x/100x100bb.jpg",
                          "trackTimeMillis": 185000}])
    assert lookup_track("Haftbefehl", "Ozean") == ("https://x/600x600bb.jpg", 185.0)


def test_lookup_track_podcast_episode_duration(monkeypatch):
    def fake_itunes(params):
        if params.get("entity") == "podcastEpisode":
            return [{"trackName": "Dark Avenger Folge 6 - Wahrheit vs. Mythos", "collectionName": "Dark Agent", "trackTimeMillis": 2623000},
                    {"trackName": "Dark Agent Folge 6 - Die dunkle Seite der Demokratie", "collectionName": "Dark Agent",
                     "trackTimeMillis": 2098000}]
        return [{"collectionName": "Dark Agent", "artworkUrl600": "show.jpg"}]

    monkeypatch.setattr(heos_module, "_itunes", fake_itunes)
    assert lookup_track("Dark Agent", "Dark Agent Folge 6 - Die dunkle Seite der Demokratie") == ("show.jpg", 2098.0)


def test_parse_message():
    assert parse_message("pid=1&state=play") == {"pid": "1", "state": "play"}
    assert parse_message("") == {}


class FakeHeos:
    def __init__(self):
        self.writers = []
        self.received = []
        self.payload = BLUETOOTH_PAYLOAD

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    def emit(self, obj):
        for w in self.writers:
            w.write((json.dumps(obj) + "\r\n").encode())

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        try:
            while line := await reader.readline():
                cmd = line.decode().strip().removeprefix("heos://")
                self.received.append(cmd)
                path, _, query = cmd.partition("?")
                if path == "player/get_players":
                    self.emit({"heos": {"command": path, "result": "success", "message": ""},
                               "payload": [{"name": "Denon DRA-800H", "pid": 42, "ip": "127.0.0.1", "version": "3.88.614"}]})
                elif path == "player/check_update":
                    self.emit({"heos": {"command": path, "result": "success", "message": "pid=42"},
                               "payload": {"update": "update_none"}})
                elif path == "player/get_play_state":
                    # Status kommt absichtlich vor den Titeldaten
                    self.emit({"heos": {"command": path, "result": "success", "message": "pid=42&state=play"}})
                elif path == "player/get_now_playing_media":
                    self.emit({"heos": {"command": path, "result": "success", "message": "pid=42"},
                               "payload": self.payload})
                await writer.drain()
        except ConnectionError:
            pass


def test_heos_client_tracks_now_playing_and_events():
    async def scenario():
        fake = FakeHeos()
        port = await fake.start()
        state = ReceiverState()
        changes = []
        heos = HeosClient("127.0.0.1", state, lambda: changes.append(state.to_dict()["now_playing"]), port=port,
                          cover_lookup=False)
        await heos.start()
        try:
            for _ in range(100):
                if state.now_playing and state.now_playing.state == "play":
                    break
                await asyncio.sleep(0.02)
            assert state.now_playing.title == "Folge 12"
            assert state.now_playing.state == "play"
            assert state.heos_version == "3.88.614"
            for _ in range(100):
                if state.update_available is not None:
                    break
                await asyncio.sleep(0.02)
            assert state.update_available is False

            fake.emit({"heos": {"command": "event/player_state_changed", "message": "pid=42&state=pause"}})
            for _ in range(100):
                if state.now_playing.state == "pause":
                    break
                await asyncio.sleep(0.02)
            assert changes[-1]["state"] == "pause"

            # Bluetooth: cur_pos zählt seit Beginn der Bluetooth-Wiedergabe, nicht im Titel → ignorieren
            fake.emit({"heos": {"command": "event/player_now_playing_progress",
                                "message": "pid=42&cur_pos=169000&duration=0"}})
            await asyncio.sleep(0.2)
            assert state.now_playing.position is None  # erster Titel nach dem Verbinden: unbekannt

            # Titelwechsel: zählt ab 0
            fake.payload = {**BLUETOOTH_PAYLOAD, "song": "Folge 13", "station": "Folge 13"}
            fake.emit({"heos": {"command": "event/player_now_playing_changed", "message": "pid=42"}})
            for _ in range(100):
                if state.now_playing.title == "Folge 13":
                    break
                await asyncio.sleep(0.02)
            assert state.now_playing.position_now() < 1

            heos.control("play_state", "play")
            heos.control("next")
            heos.control("previous")
            for _ in range(100):
                if "player/play_previous?pid=42" in fake.received:
                    break
                await asyncio.sleep(0.02)
            assert "player/set_play_state?pid=42&state=play" in fake.received
            assert "player/play_next?pid=42" in fake.received
            with pytest.raises(CommandError):
                heos.control("play_state", "laut")
        finally:
            await heos.stop()
            await fake.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_progress_from_streaming_service_is_used():
    async def scenario():
        fake = FakeHeos()
        fake.payload = {**BLUETOOTH_PAYLOAD, "mid": "s123456"}  # kein Bluetooth
        port = await fake.start()
        state = ReceiverState()
        heos = HeosClient("127.0.0.1", state, lambda: None, port=port, cover_lookup=False)
        await heos.start()
        try:
            for _ in range(100):
                if state.now_playing:
                    break
                await asyncio.sleep(0.02)
            fake.emit({"heos": {"command": "event/player_now_playing_progress",
                                "message": "pid=42&cur_pos=30000&duration=240000"}})
            for _ in range(100):
                if state.now_playing.duration:
                    break
                await asyncio.sleep(0.02)
            assert state.now_playing.duration == 240.0
            assert 30 <= state.now_playing.position_now() < 31
        finally:
            await heos.stop()
            await fake.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_lookup_track_via_artist_albums(monkeypatch):
    # „SSIO BWL“ findet die Songsuche nicht, das Album über den Künstler aber schon
    def fake_itunes(params, endpoint="search"):
        if endpoint == "search":
            if params.get("entity") == "musicArtist":
                return [{"artistName": "SSIO", "artistId": 7}]
            return []
        if params["entity"] == "album":
            return [{"wrapperType": "artist"},
                    {"wrapperType": "collection", "collectionName": "Alles oder Nix - Single", "collectionId": 1,
                     "artworkUrl100": "single/100x100bb.jpg"},
                    {"wrapperType": "collection", "collectionName": "Alles oder Nix", "collectionId": 2,
                     "artworkUrl100": "album/100x100bb.jpg"}]
        assert params["id"] == 2
        return [{"wrapperType": "collection"}, {"wrapperType": "track", "trackName": "BWL", "trackTimeMillis": 201000}]

    monkeypatch.setattr(heos_module, "_itunes", fake_itunes)
    assert lookup_track("SSIO", "BWL", "Alles oder Nix") == ("album/600x600bb.jpg", 201.0)

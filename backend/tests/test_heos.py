"""HEOS-Client gegen einen nachgebauten HEOS-Dienst; Payloads wie vom DRA-800H gemeldet."""

import asyncio
import json

import pytest

from app.heos import HeosClient, parse_message, parse_now_playing
from app.protocol import CommandError, ReceiverState

BLUETOOTH_PAYLOAD = {
    "type": "station", "song": "Folge 12", "station": "Folge 12", "album": "27. Oktober 2026",
    "artist": "Podcast", "image_url": "", "album_id": "1", "mid": "Bluetooth", "qid": 1, "sid": 1024,
}


def test_parse_now_playing_bluetooth():
    np = parse_now_playing(BLUETOOTH_PAYLOAD)
    assert (np.source, np.title, np.artist, np.album) == ("Bluetooth", "Folge 12", "Podcast", "27. Oktober 2026")


def test_unknown_mid_is_not_shown_as_source():
    assert parse_now_playing({**BLUETOOTH_PAYLOAD, "mid": "s123456"}).source is None


def test_control_without_connection_raises():
    heos = HeosClient("127.0.0.1", ReceiverState(), lambda: None)
    with pytest.raises(ConnectionError):
        heos.control("next")


def test_parse_message():
    assert parse_message("pid=1&state=play") == {"pid": "1", "state": "play"}
    assert parse_message("") == {}


class FakeHeos:
    def __init__(self):
        self.writers = []
        self.received = []

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
                               "payload": BLUETOOTH_PAYLOAD})
                await writer.drain()
        except ConnectionError:
            pass


def test_heos_client_tracks_now_playing_and_events():
    async def scenario():
        fake = FakeHeos()
        port = await fake.start()
        state = ReceiverState()
        changes = []
        heos = HeosClient("127.0.0.1", state, lambda: changes.append(state.to_dict()["now_playing"]), port=port)
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

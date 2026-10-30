"""TV-Client gegen einen nachgebauten jointSPACE-Dienst; Antworten wie vom 37PFL9606K/02."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.protocol import CommandError, ReceiverState
from app.tv import TvClient, scene_colors

RESPONSES = {
    "/1/system": {"menulanguage": "German", "name": "Philips TV", "country": "Germany",
                  "softwareversion": "Q5551-0.14.99.0", "model": "37PFL9606K/02"},
    "/1/sources": {"tv": {"name": "Watch TV"}, "hdmi3": {"name": "HDMI 3"},
                   "hdmiside": {"name": "HDMI Side"}, "aux9": {"name": "Aux 9"}},
    "/1/ambilight/topology": {"layers": 1, "left": 4, "top": 7, "right": 4, "bottom": 0},
}


class FakeJointSpace:
    def __init__(self):
        self.current = "hdmi3"
        self.ambilight_mode = "internal"
        self.posts = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = ({"id": fake.current} if self.path == "/1/sources/current"
                        else {"current": fake.ambilight_mode} if self.path == "/1/ambilight/mode"
                        else RESPONSES.get(self.path))
                if body is None:
                    self.send_error(404)
                    return
                self._reply(json.dumps(body).encode())

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.posts.append((self.path, data))
                if self.path == "/1/sources/current":
                    fake.current = data["id"]
                elif self.path == "/1/ambilight/mode":
                    fake.ambilight_mode = data["current"]
                self._reply(b"<html><head><title>Ok</title></head><body>Ok</body></html>")

            def _reply(self, raw):
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


async def wait_for(condition):
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("Bedingung nicht erreicht")


def test_tv_client_polls_and_controls():
    async def scenario():
        fake = FakeJointSpace()
        state = ReceiverState()
        changes = []
        tv = TvClient("127.0.0.1", state, lambda: changes.append(state.to_dict()["tv"]),
                      port=fake.port, poll_interval=0.1)
        await tv.start()
        try:
            await wait_for(lambda: state.tv.connected)
            assert state.tv.model == "37PFL9606K/02"
            assert state.tv.source == "hdmi3"
            assert [s["name"] for s in state.tv.sources] == ["Antenne", "HDMI 3", "HDMI seitlich", "Aux 9"]
            assert changes[-1]["connected"] is True

            tv.control("tv_source", "hdmiside")
            tv.control("tv_key", "ambilight")
            await wait_for(lambda: state.tv.source == "hdmiside")
            assert ("/1/input/key", {"key": "AmbilightOnOff"}) in fake.posts

            assert state.tv.ambilight_mode == "internal"
            # Ziehen im Farbfeld: viele Farben schnell hintereinander, die letzte zählt
            for color in ("#ff0000", "#00ff00", "#ff6e14"):
                tv.control("tv_ambilight", color)
            await wait_for(lambda: ("/1/ambilight/cached", {"r": 255, "g": 110, "b": 20}) in fake.posts)
            assert fake.ambilight_mode == "manual"
            assert state.tv.ambilight == "#ff6e14"
            tv.control("tv_ambilight", "scene:lava")
            await wait_for(lambda: "layer1" in fake.posts[-1][1])
            tv.control("tv_ambilight", "follow")
            await wait_for(lambda: fake.ambilight_mode == "internal")
            await asyncio.sleep(0.3)  # nächste Abfrage bestätigt
            assert (state.tv.ambilight_mode, state.tv.ambilight) == ("internal", None)

            with pytest.raises(CommandError):
                tv.control("tv_ambilight", "#xyz123")
            with pytest.raises(CommandError):
                tv.control("tv_ambilight", "scene:disco")
            with pytest.raises(CommandError):
                tv.control("tv_key", "selbstzerstörung")
            with pytest.raises(CommandError):
                tv.control("tv_source", "hdmi9")
        finally:
            await tv.stop()
            fake.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_tv_unreachable_is_reported_and_blocks_commands():
    async def scenario():
        fake = FakeJointSpace()
        state = ReceiverState()
        tv = TvClient("127.0.0.1", state, lambda: None, port=fake.port, poll_interval=0.1, timeout=0.5)
        await tv.start()
        try:
            await wait_for(lambda: state.tv.connected)
            fake.stop()  # Fernseher geht in Standby: Netzwerk aus
            await wait_for(lambda: not state.tv.connected)
            assert state.tv.source is None
            with pytest.raises(ConnectionError):
                tv.control("tv_key", "ok")
        finally:
            await tv.stop()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_state_without_tv_host():
    assert ReceiverState().to_dict()["tv"] is None


def test_scene_gradient_runs_from_top_to_bottom():
    leds = scene_colors("lava", {"left": 4, "top": 7, "right": 4, "bottom": 0})["layer1"]
    assert set(leds) == {"left", "top", "right"}
    top, bottom = {"r": 200, "g": 0, "b": 0}, {"r": 255, "g": 140, "b": 0}
    assert all(c == top for c in leds["top"].values())
    # Uhrzeigersinn ab unten links: links 0 = unten, rechts 0 = oben
    assert (leds["left"]["0"], leds["left"]["3"]) == (bottom, top)
    assert (leds["right"]["0"], leds["right"]["3"]) == (top, bottom)

"""Web-Server: WebSocket für Live-Status und Befehle, REST zum Testen, statisches Frontend."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .client import DenonClient
from .heos import HeosClient
from .protocol import CommandError
from .tv import TvClient

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

DENON_HOST = os.environ.get("DENON_HOST")
if not DENON_HOST:
    raise SystemExit("DENON_HOST ist nicht gesetzt (IP des Receivers, siehe .env.example)")
DENON_PORT = int(os.environ.get("DENON_PORT", "23"))
MAX_VOLUME = float(os.environ.get("MAX_VOLUME", "45"))
TV_HOST = os.environ.get("TV_HOST")  # optional: Philips-Fernseher (jointSPACE)
FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))

client = DenonClient(DENON_HOST, DENON_PORT, volume_limit=MAX_VOLUME)
COVER_LOOKUP = os.environ.get("COVER_LOOKUP", "on").lower() not in ("off", "0", "false", "no")
heos = HeosClient(DENON_HOST, client.state, client.notify, cover_lookup=COVER_LOOKUP)
tv = TvClient(TV_HOST, client.state, client.notify) if TV_HOST else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    await client.start()
    await heos.start()
    if tv:
        await tv.start()
    yield
    if tv:
        await tv.stop()
    await heos.stop()
    await client.stop()


app = FastAPI(title="Denon Web-UI", lifespan=lifespan)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Safari auf dem iPhone hält HTML/CSS/JS sonst nach einem Update weiter im Cache.
    ``no-cache`` heißt: vor Verwendung per ETag nachfragen – unverändert kommt nur ein 304."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class Command(BaseModel):
    cmd: str
    value: bool | int | float | str | None = None


def _execute(command: Command) -> None:
    if command.cmd in HeosClient.COMMANDS:
        heos.control(command.cmd, command.value)
    elif tv and command.cmd in TvClient.COMMANDS:
        tv.control(command.cmd, command.value)
    else:
        client.send(command.cmd, command.value)


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "receiver_connected": client.state.connected}


@app.get("/api/state")
def get_state() -> dict:
    return client.state.to_dict()


@app.post("/api/command")
def post_command(command: Command) -> dict:
    try:
        _execute(command)
    except CommandError as exc:
        raise HTTPException(400, str(exc)) from None
    except ConnectionError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"ok": True}


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    """Server → Browser: ``{"type": "state", "state": {...}}`` bei jeder Änderung.
    Browser → Server: ``{"cmd": "...", "value": ...}``; Fehler kommen als ``{"type": "error"}``.
    """
    await ws.accept()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=50)

    def on_change(state: dict) -> None:
        if queue.full():  # langsamer Browser: alten Stand verwerfen, nur der neueste zählt
            queue.get_nowait()
        queue.put_nowait(state)

    unsubscribe = client.subscribe(on_change)

    async def push_states() -> None:
        await ws.send_json({"type": "state", "state": client.state.to_dict()})
        while True:
            await ws.send_json({"type": "state", "state": await queue.get()})

    pusher = asyncio.create_task(push_states())
    try:
        while True:
            data = await ws.receive_json()
            try:
                _execute(Command.model_validate(data))
            except (CommandError, ConnectionError, ValueError) as exc:
                await ws.send_json({"type": "error", "message": str(exc), "request": data})
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        pusher.cancel()


# Zuletzt einhängen, damit /api und /ws Vorrang haben.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

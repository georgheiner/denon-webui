"""Testet den Telnet-Client gegen einen nachgebauten Receiver auf localhost."""

import asyncio

from app.client import DenonClient


class FakeReceiver:
    """Beantwortet Abfragen wie der DRA-800H und protokolliert empfangene Befehle."""

    def __init__(self):
        self.received: list[str] = []
        self.connections = 0
        self.volume = "10"
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.drop_all()
        self.server.close()
        await self.server.wait_closed()

    def drop_all(self):
        for w in self._writers:
            w.close()
        self._writers.clear()

    async def _handle(self, reader, writer):
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                cmd = (await reader.readuntil(b"\r")).decode().strip()
                self.received.append(cmd)
                for line in self._answer(cmd):
                    writer.write((line + "\r").encode())
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    def _answer(self, cmd):
        if cmd == "PW?":
            return ["PWSTANDBY"]
        if cmd == "MV?":
            return [f"MV{self.volume}", "MVMAX 60"]
        if cmd.startswith("MV") and cmd[2:].isdigit():
            self.volume = cmd[2:]
            return [f"MV{self.volume}", "MVMAX 60"]
        if cmd == "PWON":
            return ["ZMON", "PWON"]
        if cmd == "SI?":
            return ["SINET"]
        if cmd == "SSFUN ?":
            return ["SSFUNGAME Game", "SSFUN END"]
        if cmd == "SSSOD ?":
            return ["SSSODGAME USE", "SSSODNET USE", "SSSOD END"]
        return []


async def wait_for(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("Bedingung nicht erfüllt")
        await asyncio.sleep(0.02)


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 15))


def test_initial_status_and_commands():
    async def scenario():
        fake = FakeReceiver()
        port = await fake.start()
        client = DenonClient("127.0.0.1", port, command_interval=0.01)
        updates = []
        client.subscribe(updates.append)
        await client.start()
        try:
            await wait_for(lambda: client.state.power is False and client.state.inputs_in_use)
            assert client.state.connected
            assert client.state.volume == 10.0
            assert [i["name"] for i in client.state.inputs] == ["Game", "HEOS Music"]

            client.send("power", True)
            await wait_for(lambda: client.state.power is True)
            assert updates[-1]["power"] is True
        finally:
            await client.stop()
            await fake.stop()

    run(scenario())


def test_volume_commands_coalesce():
    async def scenario():
        fake = FakeReceiver()
        port = await fake.start()
        client = DenonClient("127.0.0.1", port, command_interval=0.05)
        await client.start()
        try:
            await wait_for(lambda: client.state.volume == 10.0)
            fake.received.clear()
            for v in range(20, 41):  # wie ein schnell gezogener Slider
                client.send("volume", v)
            await wait_for(lambda: client.state.volume == 40.0)
            assert [c for c in fake.received if c.startswith("MV")] == ["MV40"]
        finally:
            await client.stop()
            await fake.stop()

    run(scenario())


def test_reconnects_after_drop():
    async def scenario():
        fake = FakeReceiver()
        port = await fake.start()
        client = DenonClient("127.0.0.1", port, command_interval=0.01, max_backoff=0.2)
        connected_flags = []
        client.subscribe(lambda s: connected_flags.append(s["connected"]))
        await client.start()
        try:
            await wait_for(lambda: client.state.connected)
            fake.drop_all()
            await wait_for(lambda: False in connected_flags)
            await wait_for(lambda: fake.connections == 2 and client.state.connected, timeout=5)
        finally:
            await client.stop()
            await fake.stop()

    run(scenario())


def test_backoff_grows_when_receiver_closes_at_once():
    # Hängender DRA-800H: nimmt die Verbindung an und schließt sie sofort, ohne zu antworten
    async def scenario():
        connections = 0

        async def slam(reader, writer):
            nonlocal connections
            connections += 1
            writer.close()

        server = await asyncio.start_server(slam, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = DenonClient("127.0.0.1", port, min_backoff=0.05, max_backoff=5)
        await client.start()
        try:
            await asyncio.sleep(1.2)
            # feste 0,05 s wären ~20 Versuche; wachsend 0,1 + 0,2 + 0,4 + 0,8 → höchstens 5
            assert 2 <= connections <= 5
        finally:
            await client.stop()
            server.close()
            await server.wait_closed()

    run(scenario())


def test_send_while_disconnected_raises():
    client = DenonClient("127.0.0.1", 9)
    try:
        client.send("power", True)
    except ConnectionError:
        pass
    else:
        raise AssertionError("ConnectionError erwartet")

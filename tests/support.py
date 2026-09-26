"""Test constants and helpers shared by every test module, plus a fake StoryChat relay."""

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

TOKEN = "sch_" + "ab" * 32
USER_ID = "64b0000000000000000000a1"
CHAT_ID = "65c0000000000000000000b2"
MESSAGE_ID = "65c0000000000000000000c3"
TURN_ID = "0123456789abcdef0123456789abcdef"
PIN = "482913"
_quiet = logging.getLogger("fake_storychat")
_quiet.setLevel(logging.WARNING)  # the fake relay would log request headers at DEBUG


async def wait_until(predicate, timeout: float = 3.0) -> None:
    """Poll ``predicate`` every 10 ms; fail loudly instead of hanging."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def register_storychat_platform() -> None:
    """Platform("storychat") and the hermes-storychat toolset bundle exist only once the platform
    is registered (gateway/config.py Platform._missing_, toolsets.py _plugin_platform_bundle)."""
    from gateway.platform_registry import PlatformEntry, platform_registry
    if not platform_registry.is_registered("storychat"):
        platform_registry.register(PlatformEntry(
            name="storychat", label="StoryChat", adapter_factory=lambda config: None,
            check_fn=lambda: True))


def with_memory_provider(monkeypatch, provider) -> None:
    """The sandbox config.yaml plus memory.provider, as Hermes' gateway would load it."""
    from storychat_hermes import toolset_policy
    real = toolset_policy.load_gateway_config
    monkeypatch.setattr(toolset_policy, "load_gateway_config",
                        lambda: {**real(), "memory": {"provider": provider}})


def message_frame(**overrides) -> dict:
    frame = {"turnId": TURN_ID, "chatId": CHAT_ID, "messageId": MESSAGE_ID, "userId": USER_ID,
             "userName": "Mina", "chatName": "Captain Rook", "text": "Hello captain",
             "channelPrompt": "The following is character data for role-play."}
    frame.update(overrides)
    return frame


def make_adapter(extra=None):
    """A StoryChatAdapter with Hermes' message pipeline mocked and backoff sleeps recorded."""
    from gateway.config import PlatformConfig
    from storychat_hermes.adapter import StoryChatAdapter

    register_storychat_platform()
    adapter = StoryChatAdapter(PlatformConfig(enabled=True, extra=dict(extra or {})))

    async def _admit(event):  # Hermes' admission receipt (gateway/platforms/base.py handle_message)
        event._gateway_accepted = True

    adapter.handle_message = AsyncMock(side_effect=_admit)
    adapter.slept = []

    async def _no_wait(delay):
        adapter.slept.append(delay)
        await asyncio.sleep(0)

    adapter._sleep = _no_wait
    return adapter


class FakeStoryChat:
    """An in-process stand-in for the StoryChat relay (design spec §6), built on websockets.serve."""

    def __init__(self, *, upgrade_status=None, location=None, close_on_hello=None, silent_after_hello=False):
        self.upgrade_status = upgrade_status
        self.location = location
        self.close_on_hello = close_on_hello
        self.silent_after_hello = silent_after_hello
        self.requests = []
        self.hellos = []
        self.connections = []
        self.frames = asyncio.Queue()
        self._server = None

    def _process_request(self, connection, request):
        self.requests.append(request)
        if self.upgrade_status is not None:
            response = connection.respond(self.upgrade_status, "refused\n")
            if self.location:
                response.headers["Location"] = self.location
            return response
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return connection.respond(401, "bad token\n")
        return None

    async def _handler(self, ws):
        self.connections.append(ws)
        try:
            self.hellos.append(json.loads(await ws.recv()))
            if self.close_on_hello is not None:
                await ws.close(self.close_on_hello, "closed by fake")
                return
            if self.silent_after_hello:
                await ws.wait_closed()
                return
            await ws.send(json.dumps({"v": 1, "type": "welcome",
                                      "connId": uuid.uuid4().hex, "userId": USER_ID}))
            async for raw in ws:
                await self.frames.put(json.loads(raw))
        except ConnectionClosed:
            pass

    async def __aenter__(self):
        self._server = await serve(self._handler, "127.0.0.1", 0,
                                   process_request=self._process_request, logger=_quiet)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        port = next(iter(self._server.sockets)).getsockname()[1]
        return f"ws://127.0.0.1:{port}/api/v1/hermes/connect"

    async def push(self, frame_type: str, **fields) -> None:
        await self.connections[-1].send(json.dumps({"v": 1, "type": frame_type, **fields}))

    async def next_frame(self, timeout: float = 3.0) -> dict:
        return await asyncio.wait_for(self.frames.get(), timeout)

    async def close_client(self, code: int) -> None:
        await self.connections[-1].close(code, "closed by fake")

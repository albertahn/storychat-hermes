"""Test constants and helpers shared by every test module."""

import asyncio

TOKEN = "sch_" + "ab" * 32
USER_ID = "64b0000000000000000000a1"
CHAT_ID = "65c0000000000000000000b2"
MESSAGE_ID = "65c0000000000000000000c3"
TURN_ID = "0123456789abcdef0123456789abcdef"
PIN = "482913"


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


def message_frame(**overrides) -> dict:
    frame = {"turnId": TURN_ID, "chatId": CHAT_ID, "messageId": MESSAGE_ID, "userId": USER_ID,
             "userName": "Mina", "chatName": "Captain Rook", "text": "Hello captain",
             "channelPrompt": "The following is character data for role-play."}
    frame.update(overrides)
    return frame

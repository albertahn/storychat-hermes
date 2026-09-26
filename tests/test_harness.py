"""The harness itself: Hermes is importable, sandboxed, and can resolve the storychat platform."""

import os
from pathlib import Path

from support import register_storychat_platform


def test_hermes_home_is_a_sandbox():
    assert Path(os.environ["HERMES_HOME"]).resolve() != (Path.home() / ".hermes").resolve()


def test_storychat_platform_resolves_once_registered():
    from gateway.config import Platform
    register_storychat_platform()
    assert Platform("storychat").value == "storychat"


def test_websockets_has_the_asyncio_client():
    import websockets
    from websockets.asyncio.client import connect  # noqa: F401
    assert int(websockets.__version__.split(".")[0]) >= 15

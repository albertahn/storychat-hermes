"""Sandbox every Hermes path a test could touch and expose the plugin as ``storychat_hermes``."""

import os
import sys
import tempfile
import types
from pathlib import Path

# gateway.run reads HERMES_HOME once at import: point it at an empty sandbox before Hermes loads.
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="storychat-hermes-home-")
os.environ["HERMES_GATEWAY_LOCK_DIR"] = tempfile.mkdtemp(prefix="storychat-hermes-locks-")

# Spec §9.2: without this, Hermes' tool-progress / interim-assistant lines carry no _interim_send
# marker for storychat, so they'd go out as kind "reply" and the backend would save them as the
# character's message. Every test's sandbox starts with the spec-recommended opt-out already set,
# same as a real operator following §9.2 would configure.
(Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text(
    "display:\n"
    "  platforms:\n"
    "    storychat:\n"
    "      tool_progress: off\n"
    "      long_running_notifications: off\n"
    "      interim_assistant_messages: false\n")

import pytest  # noqa: E402

from support import TOKEN, USER_ID  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Hermes imports the repo root as a package (hermes_cli/plugins_loader.py). Tests reach its modules as
# storychat_hermes.<module> without running __init__.py; test_register.py loads __init__.py itself.
_package = types.ModuleType("storychat_hermes")
_package.__path__ = [str(ROOT)]
sys.modules["storychat_hermes"] = _package

_STORYCHAT_ENV = ("STORYCHAT_HERMES_TOKEN", "STORYCHAT_ALLOWED_USERS", "STORYCHAT_APPROVAL_PIN",
                  "STORYCHAT_TOOLSETS", "STORYCHAT_URL", "STORYCHAT_DEV_INSECURE_LOCALHOST",
                  "STORYCHAT_ALLOW_MEMORY_PROVIDER")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in _STORYCHAT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    from storychat_hermes import adapter
    monkeypatch.setattr(adapter, "_opt_in_warned", False)
    monkeypatch.setattr(adapter, "_memory_provider_warned", False)
    from storychat_hermes import approvals
    monkeypatch.setattr(approvals, "PIN_GUARD", approvals.PinGuard())


@pytest.fixture
def storychat_env(monkeypatch):
    """A valid local-dev configuration; tests add STORYCHAT_URL from their fake relay."""
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_ALLOWED_USERS", USER_ID)
    monkeypatch.setenv("STORYCHAT_DEV_INSECURE_LOCALHOST", "1")

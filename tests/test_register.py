import importlib.util
import sys
from pathlib import Path

import yaml

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry
from storychat_hermes import adapter as adapter_mod
from support import TOKEN

ROOT = Path(__file__).resolve().parents[1]


class RecordingContext:
    def __init__(self):
        self.calls = []

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)


def test_register_declares_the_storychat_platform():
    ctx = RecordingContext()
    adapter_mod.register(ctx)
    (kwargs,) = ctx.calls
    assert kwargs["name"] == "storychat"
    assert kwargs["label"] == "StoryChat"
    assert kwargs["adapter_factory"] is adapter_mod.StoryChatAdapter
    assert kwargs["required_env"] == ["STORYCHAT_HERMES_TOKEN"]
    assert kwargs["allowed_users_env"] == "STORYCHAT_ALLOWED_USERS"
    assert "allow_all_env" not in kwargs
    PlatformEntry(**kwargs)  # every keyword is one Hermes' PlatformEntry still accepts


def test_is_connected_gates_on_the_token(monkeypatch):
    assert adapter_mod.is_connected(PlatformConfig(enabled=True)) is False
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", "   ")
    assert adapter_mod.is_connected(PlatformConfig(enabled=True)) is False
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    assert adapter_mod.is_connected(PlatformConfig(enabled=True)) is True
    assert adapter_mod.check_requirements() is True


def test_manifest_is_a_platform_plugin_requiring_the_token():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert (manifest["name"], manifest["kind"], manifest["version"]) == (
        "storychat-hermes", "platform", adapter_mod.PLUGIN_VERSION)
    assert [e["name"] for e in manifest["requires_env"]] == ["STORYCHAT_HERMES_TOKEN"]


def test_hermes_can_load_the_directory_plugin():
    # Same mechanics as hermes_cli/plugins_loader.py _load_directory_module.
    spec = importlib.util.spec_from_file_location(
        "storychat_hermes_probe", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert callable(module.register)
    finally:
        for name in [n for n in sys.modules if n.startswith("storychat_hermes_probe")]:
            del sys.modules[name]

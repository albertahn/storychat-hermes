import pytest

from storychat_hermes import settings
from support import PIN, TOKEN, USER_ID


def test_defaults_to_prod_relay_and_chat_only(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    cfg = settings.load_settings()
    assert cfg.url == "wss://prod-rail.storychat.app/api/v1/hermes/connect"
    assert cfg.token == TOKEN
    assert cfg.approval_pin == ""
    assert cfg.toolsets_raw == ""
    assert cfg.is_local_dev is False


@pytest.mark.parametrize("token", ["", "sch_short", "abc_" + "ab" * 32, "sch_" + "AB" * 32])
def test_rejects_missing_or_malformed_token(monkeypatch, token):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", token)
    with pytest.raises(settings.SettingsError, match="storychat.app/chat/hermes"):
        settings.load_settings()


def test_ws_is_refused_without_the_dev_flag(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_URL", "ws://localhost:8080/api/v1/hermes/connect")
    with pytest.raises(settings.SettingsError, match="STORYCHAT_DEV_INSECURE_LOCALHOST"):
        settings.load_settings()


def test_ws_localhost_is_allowed_with_the_dev_flag(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_URL", "ws://localhost:8080/api/v1/hermes/connect")
    monkeypatch.setenv("STORYCHAT_DEV_INSECURE_LOCALHOST", "1")
    assert settings.load_settings().is_local_dev is True


def test_dev_flag_never_allows_ws_to_a_remote_host(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_URL", "ws://example.com/api/v1/hermes/connect")
    monkeypatch.setenv("STORYCHAT_DEV_INSECURE_LOCALHOST", "1")
    with pytest.raises(settings.SettingsError):
        settings.load_settings()


def test_pin_must_be_six_or_more_digits(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_APPROVAL_PIN", "1234")
    assert settings.load_settings().approval_pin == ""
    monkeypatch.setenv("STORYCHAT_APPROVAL_PIN", PIN)
    assert settings.load_settings().approval_pin == PIN


def test_a_comment_that_dotenv_read_as_the_value_counts_as_empty(monkeypatch):
    # python-dotenv (Hermes' .env parser) reads `STORYCHAT_TOOLSETS=    # note` as "# note".
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_TOOLSETS",
                       '# empty = chat only (default). e.g. "web,file,terminal" to opt in.')
    monkeypatch.setenv("STORYCHAT_APPROVAL_PIN",
                       "# fill in yourself, 6+ digits; needed to approve from StoryChat")
    cfg = settings.load_settings()
    assert (cfg.toolsets_raw, cfg.approval_pin) == ("", "")


def test_allowed_users_are_split_on_commas_and_trimmed(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_ALLOWED_USERS", f" {USER_ID} ,, 65C0000000000000000000B2 ")
    assert settings.load_settings().allowed_users == (USER_ID, "65C0000000000000000000B2")


def test_a_printed_settings_object_never_shows_the_token_or_the_pin(monkeypatch):
    monkeypatch.setenv("STORYCHAT_HERMES_TOKEN", TOKEN)
    monkeypatch.setenv("STORYCHAT_APPROVAL_PIN", PIN)
    text = repr(settings.load_settings())
    assert TOKEN not in text
    assert PIN not in text

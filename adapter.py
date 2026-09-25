"""StoryChat platform adapter for Hermes Agent (design spec §6 wire protocol, §9 plugin).

The adapter dials OUT to the StoryChat relay over one websocket, turns ``message`` frames into
Hermes ``MessageEvent``s and streams Hermes' sends/edits back as ``send``/``edit`` frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import lifecycle, protocol, toolset_policy
from . import settings as settings_mod

logger = logging.getLogger(__name__)
# websockets logs every header and frame at DEBUG (the Bearer token, chat text, the PIN). Its logger
# is capped at INFO so a gateway running with debug logging still never prints them (spec §12).
_ws_logger = logging.getLogger("storychat_hermes.websocket")
_ws_logger.setLevel(logging.INFO)

PLUGIN_VERSION = "0.1.0"
# 16 × 16384 chars = the relay's 256 KiB reply cap, so the 20-segment cap is never hit first,
# and 16384 chars × 4 UTF-8 bytes stays far below the 1 MiB frame limit.
MAX_MESSAGE_LENGTH = 16384
OPEN_TIMEOUT_S = 10.0
WELCOME_TIMEOUT_S = 10.0
LOCK_SCOPE = "storychat-hermes-token"
# Spec §9.3: logged exactly, with the real id, when STORYCHAT_ALLOWED_USERS does not name the owner.
NOT_ALLOWED_MSG = "STORYCHAT_ALLOWED_USERS must contain your StoryChat userId {user_id} — copy it from storychat.app/chat/hermes"
_opt_in_warned = False


@dataclass(frozen=True)
class _Turn:
    turn_id: str
    chat_id: str
    message_id: str
    user_name: str
    chat_name: str


class _ConnectFailed(Exception):
    def __init__(self, verdict: lifecycle.Verdict) -> None:
        super().__init__(verdict.message)
        self.verdict = verdict


def _close_code(exc: ConnectionClosed) -> Optional[int]:
    if exc.rcvd is not None:
        return exc.rcvd.code
    return exc.sent.code if exc.sent is not None else None


class StoryChatAdapter(BasePlatformAdapter):
    """One outbound websocket to StoryChat; one Hermes DM session per storychat."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    REQUIRES_EDIT_FINALIZE = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("storychat"))
        self._settings: Optional[settings_mod.StoryChatSettings] = None
        self._ws: Any = None
        self._run_task: Optional[asyncio.Task] = None
        self._closing = False
        self._user_id = ""
        self._toolset_override: List[str] = list(toolset_policy.CHAT_ONLY_SENTINEL)
        self._effective_toolsets: List[str] = []
        self._turns: Dict[str, _Turn] = {}  # chat_id -> running turn
        self._sleep = asyncio.sleep  # tests replace these two
        self._rng = random.Random()

    # ── connection lifecycle (spec §9.4) ────────────────────────────────────

    def _fail(self, code: str, message: str, *, retryable: bool) -> bool:
        logger.error("[%s] %s", self.name, message)
        self._set_fatal_error(code, message, retryable=retryable)
        return False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._closing = False
        try:
            cfg = settings_mod.load_settings()
        except settings_mod.SettingsError as exc:
            return self._fail("storychat_config_invalid", str(exc), retryable=False)
        if not self._check_toolsets(cfg):
            return False
        if not self._acquire_platform_lock(LOCK_SCOPE, cfg.token, "StoryChat pairing token"):
            return False
        self._settings = cfg
        try:
            await self._open_session()
        except _ConnectFailed as exc:
            self._release_platform_lock()
            return self._fail(exc.verdict.code, exc.verdict.message, retryable=exc.verdict.retry)
        self._run_task = asyncio.create_task(self._run())
        self._wire_plugin_handlers(None)
        return True

    def _check_toolsets(self, cfg: settings_mod.StoryChatSettings) -> bool:
        """Startup self-check (spec §9.3): refuse to connect when chat-only mode would leak tools."""
        global _opt_in_warned
        try:
            override = toolset_policy.toolset_override(cfg.toolsets_raw)
            effective = toolset_policy.effective_toolsets(override)
        except toolset_policy.ToolsetPolicyError as exc:
            return self._fail("storychat_toolsets_invalid", str(exc), retryable=False)
        except Exception as exc:
            return self._fail("storychat_toolsets_unverified",
                              f"could not verify the StoryChat toolsets ({type(exc).__name__}); "
                              "refusing to connect", retryable=False)
        chat_only = toolset_policy.is_chat_only(override)
        leaked = toolset_policy.leaked_toolsets(effective) if chat_only else []
        if leaked:
            return self._fail(
                "storychat_toolsets_leaked",
                "chat-only mode, but these toolsets would still reach StoryChat: "
                f"{', '.join(leaked)}. Turn them off for the storychat platform (list plugin "
                "toolsets under known_plugin_toolsets.storychat in config.yaml, or add them to "
                "agent.disabled_toolsets), then restart the gateway.", retryable=False)
        if not chat_only and "clarify" in effective:
            return self._fail(
                "storychat_toolsets_clarify",
                "STORYCHAT_TOOLSETS brings in the clarify toolset (through a bundle such as "
                "hermes-cli, coding or all), and StoryChat cannot answer clarify questions, so "
                "turns would hang. Name toolsets one by one (for example web,file,terminal) or "
                "add clarify to agent.disabled_toolsets in config.yaml, then restart the gateway.",
                retryable=False)
        if not chat_only and not _opt_in_warned:
            _opt_in_warned = True
            logger.warning("[%s] STORYCHAT_TOOLSETS enables %s. Imported or cloned character "
                           "cards can carry instructions; every dangerous command still needs "
                           "your approval PIN.", self.name, ", ".join(effective) or "no toolsets")
        self._toolset_override, self._effective_toolsets = override, effective
        return True

    async def _open_session(self) -> None:
        """Open the socket, send hello, wait for welcome; raise _ConnectFailed with a verdict."""
        cfg = self._settings
        try:
            ws = await lifecycle.NoRedirectConnect(
                cfg.url, additional_headers={"Authorization": f"Bearer {cfg.token}"},
                user_agent_header=f"storychat-hermes/{PLUGIN_VERSION}",
                max_size=protocol.MAX_FRAME_BYTES, open_timeout=OPEN_TIMEOUT_S,
                proxy=None if cfg.is_local_dev else True, logger=_ws_logger)
        except InvalidStatus as exc:
            raise _ConnectFailed(lifecycle.classify_upgrade_status(exc.response.status_code)) from None
        except (OSError, TimeoutError, InvalidHandshake) as exc:
            raise _ConnectFailed(lifecycle.Verdict(
                True, lifecycle.BACKOFF_START_S, "storychat_unreachable",
                f"could not reach StoryChat ({type(exc).__name__}); retrying")) from None
        try:
            try:
                await ws.send(protocol.hello(PLUGIN_VERSION, self._effective_toolsets))
                frame = protocol.parse_server_frame(
                    await asyncio.wait_for(ws.recv(), timeout=WELCOME_TIMEOUT_S))
                if frame["type"] != "welcome":
                    raise protocol.ProtocolError(f"expected welcome, got {frame['type']}")
            except ConnectionClosed as exc:
                raise _ConnectFailed(lifecycle.classify_close(_close_code(exc))) from None
            except protocol.ProtocolError:
                await ws.close()
                raise _ConnectFailed(lifecycle.classify_close(4400)) from None
            except TimeoutError:
                await ws.close()
                raise _ConnectFailed(lifecycle.Verdict(
                    True, lifecycle.BACKOFF_START_S, "storychat_no_welcome",
                    "StoryChat did not answer hello within 10s; retrying")) from None
            # Spec §9.3: otherwise Hermes would reject the owner, or DM a pairing code that the relay would
            # save as the character's reply. Hermes matches its allowlist exactly, so keep the listed spelling.
            user_id = next((u for u in cfg.allowed_users if u.lower() == frame["userId"].lower()), None)
            if user_id is None:
                await ws.close()
                raise _ConnectFailed(lifecycle.Verdict(
                    False, 0.0, "storychat_user_not_allowed", NOT_ALLOWED_MSG.format(user_id=frame["userId"])))
            self._ws, self._user_id = ws, user_id
            logger.info("[%s] connected (connId=%s userId=%s)", self.name, frame["connId"], frame["userId"])
            self._mark_connected()
        except BaseException:
            try:
                await ws.close()
            except Exception:
                logger.debug("[%s] closing an unfinished StoryChat session failed", self.name, exc_info=True)
            raise

    async def _run(self) -> None:
        """Read frames until the socket closes, then reconnect on this same instance."""
        while not self._closing:
            verdict = await self._pump()
            self._ws = None
            if self._closing:
                return
            self._mark_disconnected()
            if not verdict.retry:
                await self._go_fatal(verdict, retryable=False)
                return
            logger.warning("[%s] %s", self.name, verdict.message)
            if not await self._reconnect(verdict.backoff_start):
                return

    async def _pump(self) -> lifecycle.Verdict:
        ws = self._ws
        try:
            async for raw in ws:
                try:
                    await self._handle_frame(raw)
                except Exception as exc:
                    logger.error("[%s] frame handler failed: %s", self.name, type(exc).__name__)
        except ConnectionClosed as exc:
            return lifecycle.classify_close(_close_code(exc))
        return lifecycle.classify_close(ws.close_code)

    async def _reconnect(self, start: float) -> bool:
        attempt = 0
        while not self._closing:
            await self._sleep(lifecycle.backoff_delay(attempt, start, self._rng))
            attempt += 1
            try:
                await self._open_session()
                return True
            except _ConnectFailed as exc:
                verdict = exc.verdict
            if not verdict.retry:
                await self._go_fatal(verdict, retryable=False)
                return False
            if attempt >= lifecycle.MAX_RECONNECT_ATTEMPTS:
                await self._go_fatal(verdict, retryable=True)
                return False
            if verdict.backoff_start > start:
                start, attempt = verdict.backoff_start, 0
            logger.warning("[%s] %s", self.name, verdict.message)
        return False

    async def _go_fatal(self, verdict: lifecycle.Verdict, *, retryable: bool) -> None:
        logger.error("[%s] %s", self.name, verdict.message)
        self._set_fatal_error(verdict.code, verdict.message, retryable=retryable)
        await self._notify_fatal_error()

    async def disconnect(self) -> None:
        self._closing = True
        try:
            self._release_platform_lock()
        except Exception:
            logger.debug("[%s] releasing the platform lock failed during disconnect", self.name,
                        exc_info=True)
        self._mark_disconnected()
        task, self._run_task = self._run_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("[%s] the run task raised during disconnect", self.name, exc_info=True)
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("[%s] closing the websocket failed during disconnect", self.name,
                            exc_info=True)
        self._turns = {}

    async def _send_frame(self, frame: str) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(frame)
        except ConnectionClosed:
            return False
        return True

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    def toolsets_for_source(self, source: Any) -> List[str]:
        """Never ``[]``/``None``: Hermes reads both as "no override" = every tool (spec §9.3)."""
        return list(self._toolset_override)

    # ── inbound frames ──────────────────────────────────────────────────────

    async def _handle_frame(self, raw: Any) -> None:
        try:
            frame = protocol.parse_server_frame(raw)
        except protocol.ProtocolError as exc:
            logger.warning("[%s] dropped invalid frame: %s", self.name, exc)
            return
        if frame["type"] == "message":
            await self._on_message(frame)

    async def _on_message(self, frame: Dict[str, Any]) -> None:
        turn_id, chat_id = frame["turnId"], frame["chatId"]
        running = self._turns.get(chat_id)
        if frame["userId"].lower() != self._user_id.lower() or running is not None:
            logger.warning("[%s] refused turn %s (wrong user or a turn is already running)",
                           self.name, turn_id)
            await self._send_frame(protocol.turn_end(turn_id, chat_id, "error"))
            return
        # R-T12: config.yaml is re-read every turn (gateway/run_turn.py _load_gateway_config), so a
        # config edit after startup could otherwise bypass the connect-time chat-only self-check.
        try:
            effective = toolset_policy.effective_toolsets(self._toolset_override)
        except Exception:
            logger.debug("[%s] toolset recheck failed", self.name, exc_info=True)
            effective = None
        chat_only = toolset_policy.is_chat_only(self._toolset_override)
        if (effective is None or (chat_only and toolset_policy.leaked_toolsets(effective))
                or (not chat_only and "clarify" in effective)):
            logger.error("[%s] refused turn %s: the StoryChat toolsets changed since startup; "
                         "fix config.yaml and restart the gateway", self.name, turn_id)
            await self._send_frame(protocol.turn_end(turn_id, chat_id, "error"))
            return
        source = self.build_source(chat_id=chat_id, chat_name=frame["chatName"], chat_type="dm",
                                   user_id=self._user_id, user_name=frame["userName"])
        event = MessageEvent(text=frame["text"], message_type=MessageType.TEXT, source=source,
                             message_id=frame["messageId"],
                             channel_prompt=frame["channelPrompt"] or None,
                             allow_gateway_control=False)
        self._turns[chat_id] = _Turn(turn_id, chat_id, frame["messageId"], frame["userName"],
                                     frame["chatName"])
        logger.info("[%s] turn %s started", self.name, turn_id)
        await self.handle_message(event)

    # ── outbound: send / edit / turn_end ────────────────────────────────────

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Text can only go to a running StoryChat turn."""
        return SendResult(success=False, error="no active StoryChat turn for this chat")


# ── plugin registration (spec §9.1) ─────────────────────────────────────────


def check_requirements() -> bool:
    """Passive probe: the websockets asyncio client ships with Hermes."""
    try:
        import websockets.asyncio.client  # noqa: F401
    except ImportError:
        return False
    return True


def is_connected(config: Any) -> bool:
    """Gates enablement: without a token the platform would retry-connect forever."""
    return bool(str(_get_scoped_secret("STORYCHAT_HERMES_TOKEN", "") or "").strip())


def register(ctx: Any) -> None:
    ctx.register_platform(
        name="storychat", label="StoryChat", adapter_factory=StoryChatAdapter,
        check_fn=check_requirements, is_connected=is_connected,
        required_env=["STORYCHAT_HERMES_TOKEN"], allowed_users_env="STORYCHAT_ALLOWED_USERS",
        install_hint="websockets ships with Hermes Agent; reinstall Hermes if it is missing",
        max_message_length=MAX_MESSAGE_LENGTH, emoji="📖")

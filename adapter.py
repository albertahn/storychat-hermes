"""StoryChat platform adapter for Hermes Agent (design spec §6 wire protocol, §9 plugin).

The adapter dials OUT to the StoryChat relay over one websocket, turns ``message`` frames into
Hermes ``MessageEvent``s and streams Hermes' sends/edits back as ``send``/``edit`` frames.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidProxy, InvalidStatus

from gateway.config import DEFAULT_STREAMING_CURSOR, Platform, PlatformConfig
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome

from . import approvals as approvals_mod
from . import lifecycle, notices, protocol, toolset_policy
from . import settings as settings_mod
from .approvals import ApprovalBook, PendingApproval, correlate_request_id

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
# _stop_ids is otherwise never pruned; capped so a late "Stopped" reply stays recognisable
# for the newest MAX_STOP_IDS stops instead of growing forever on a long-running gateway.
MAX_STOP_IDS = 256
_OUTCOME_REASONS = {ProcessingOutcome.SUCCESS: "done", ProcessingOutcome.CANCELLED: "interrupted",
                    ProcessingOutcome.FAILURE: "error"}
# Spec §9.3: logged exactly, with the real id, when STORYCHAT_ALLOWED_USERS does not name the owner.
NOT_ALLOWED_MSG = "STORYCHAT_ALLOWED_USERS must contain your StoryChat userId {user_id} — copy it from storychat.app/chat/hermes"
PROXY_MSG = "proxy settings are invalid or need python-socks — check HTTPS_PROXY/WSS_PROXY/SOCKS_PROXY"
# Why the per-turn recheck refused a turn; the README troubleshooting table quotes these.
TOOLSETS_CHANGED = "the StoryChat toolsets changed since startup"
DISPLAY_CHANGED = "the StoryChat display settings changed since startup"
RECHECK_UNVERIFIED = "could not verify the StoryChat toolsets/display settings"
APPROVALS_OFF = "Hermes approvals are off (approvals.mode: off, --yolo or /yolo)"
MEMORY_PROVIDER_ON = ("an external memory provider is on (memory.provider) without "
                      "STORYCHAT_ALLOW_MEMORY_PROVIDER=1")
MEMORY_PROVIDER_MSG = (
    "memory.provider is {provider}: Hermes sends every StoryChat turn, including text a character "
    "card can steer, to that memory provider, and your other Hermes sessions can recall it. Set "
    "memory.provider back to the built-in store in config.yaml, or set "
    "STORYCHAT_ALLOW_MEMORY_PROVIDER=1 in .env to accept this, then restart the gateway.")
_opt_in_warned = False
_memory_provider_warned = False


@dataclass(frozen=True)
class _Turn:
    turn_id: str
    chat_id: str
    message_id: str
    user_name: str
    chat_name: str
    stop_requested: bool = False
    failed: bool = False  # Hermes sent its failed-turn copy (notices.FAILURE) for this turn


def _approvals_bypassed(session_key: str) -> bool:
    """Whether Hermes runs dangerous commands without asking anyone: process --yolo
    (HERMES_YOLO_MODE), /yolo for this session, or approvals.mode: off. tools/approval.py
    check_all_command_guards approves on exactly these before any approval card is shown."""
    from tools.approval import is_approval_bypass_active_for_session
    return is_approval_bypass_active_for_session(session_key)


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
        self._msg_kinds: Dict[str, Tuple[str, str]] = {}  # msgId -> (turnId, kind)
        self._stop_ids: Dict[str, str] = {}  # synthetic /stop message_id -> turnId
        self._aux_tasks: set = set()
        self._approvals = ApprovalBook()
        self._pin_guard = approvals_mod.PIN_GUARD
        self._sleep = asyncio.sleep  # tests replace these two
        self._rng = random.Random()
        # Hermes delivers operational notices ("No home channel is set", subagent failures) with a
        # plain send() unless notice_delivery is "private" (gateway/run_notifications.py
        # _deliver_platform_notice); a plain send would be saved as the character's reply. "private"
        # routes them to send_private_notice, which sends them as status. StoryChat has no
        # public/private audience, so "public" (explicit, or a typo Hermes normalises to public)
        # would only save notices as the reply: always override it.
        self.config.extra["notice_delivery"] = "private"

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
        self._run_task.add_done_callback(self._on_run_task_done)
        self._wire_plugin_handlers(None)
        return True

    def _on_run_task_done(self, task: "asyncio.Task") -> None:
        """A connection task that dies with an exception would otherwise leave the platform
        silently dead: hand it to Hermes' reconnect watcher like any other retryable failure."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None or self._closing:
            return
        logger.error("[%s] the StoryChat connection task failed: %s", self.name, type(exc).__name__)
        logger.debug("[%s] connection task failure", self.name, exc_info=exc)
        self._set_fatal_error("storychat_connection_failed",
                              f"the StoryChat connection failed ({type(exc).__name__}); retrying",
                              retryable=True)
        self._spawn(self._notify_fatal_error())

    def _check_toolsets(self, cfg: settings_mod.StoryChatSettings) -> bool:
        """Startup self-check (spec §9.3): refuse to connect when StoryChat turns would get tools
        they must not, run commands with approvals off, stream progress as the reply, or feed an
        external memory provider."""
        global _opt_in_warned
        try:
            override = toolset_policy.toolset_override(cfg.toolsets_raw)
            effective = toolset_policy.effective_toolsets(override)
        except toolset_policy.ToolsetPolicyError as exc:
            return self._fail("storychat_toolsets_invalid", str(exc), retryable=False)
        except Exception as exc:
            logger.debug("[%s] toolset self-check failure", self.name, exc_info=True)
            return self._fail("storychat_toolsets_unverified",
                              f"could not verify the StoryChat toolsets ({type(exc).__name__}); "
                              "refusing to connect", retryable=False)
        too_long = sum(len(name) > protocol.MAX_TOOLSET_NAME_CHARS for name in effective)
        if len(effective) > protocol.MAX_HELLO_TOOLSETS or too_long:
            return self._fail(
                "storychat_toolsets_invalid",
                f"StoryChat accepts at most {protocol.MAX_HELLO_TOOLSETS} toolsets with names of up "
                f"to {protocol.MAX_TOOLSET_NAME_CHARS} characters, but STORYCHAT_TOOLSETS resolves to "
                f"{len(effective)} toolsets, {too_long} of them with a longer name. Name fewer "
                "toolsets or MCP servers (or give a long-named MCP server a shorter name), then "
                "restart the gateway.", retryable=False)
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
        if not chat_only and _approvals_bypassed(""):
            return self._fail(
                "storychat_approvals_off",
                "STORYCHAT_TOOLSETS gives StoryChat tools, but Hermes approvals are off "
                "(approvals.mode: off in config.yaml, or the gateway runs with --yolo / "
                "HERMES_YOLO_MODE), so dangerous commands would run without your PIN. Turn "
                "approvals on (approvals.mode: manual or smart) or leave STORYCHAT_TOOLSETS empty "
                "for chat only, then restart the gateway.", retryable=False)
        if not chat_only and not _opt_in_warned:
            _opt_in_warned = True
            logger.warning("[%s] STORYCHAT_TOOLSETS enables %s. Imported or cloned character "
                           "cards can carry instructions; dangerous commands need approval from "
                           "StoryChat with your PIN, unless they match your command_allowlist or "
                           "smart approvals pass them.", self.name,
                           ", ".join(effective) or "no toolsets")
        try:
            unsafe = toolset_policy.unsafe_display_settings()
        except Exception as exc:
            logger.debug("[%s] display self-check failure", self.name, exc_info=True)
            return self._fail("storychat_display_unverified",
                              f"could not verify the StoryChat display settings ({type(exc).__name__}); "
                              "refusing to connect", retryable=False)
        if unsafe:
            return self._fail(
                "storychat_display_unsafe",
                "these StoryChat display settings are still on: "
                f"{', '.join(unsafe)}. Hermes would send them to StoryChat as the character's "
                "reply. Set them off under display.platforms.storychat in config.yaml "
                "(tool_progress: off, interim_assistant_messages: false, thinking_progress: "
                "false), then restart the gateway.", retryable=False)
        provider = toolset_policy.external_memory_provider()
        if self._memory_provider_blocked(provider, cfg.allow_memory_provider):
            return self._fail("storychat_memory_provider",
                              MEMORY_PROVIDER_MSG.format(provider=provider), retryable=False)
        self._toolset_override, self._effective_toolsets = override, effective
        return True

    def _memory_provider_blocked(self, provider: Optional[str], allowed: bool) -> bool:
        """Chat-only mode cannot keep StoryChat text out of Hermes' external memory provider, so
        it is refused unless STORYCHAT_ALLOW_MEMORY_PROVIDER=1; accepted, it is logged once."""
        global _memory_provider_warned
        if provider is None:
            return False
        if not allowed:
            return True
        if not _memory_provider_warned:
            _memory_provider_warned = True
            logger.warning("[%s] STORYCHAT_ALLOW_MEMORY_PROVIDER=1: StoryChat turns, including text "
                           "a character card can steer, are written to the %s memory provider.",
                           self.name, provider)
        return False

    async def _open_session(self) -> None:
        """Open the socket, send hello, wait for welcome; raise _ConnectFailed with a verdict for
        every failure, so neither connect() nor _reconnect() ever sees a raw exception."""
        try:
            await self._handshake()
        except _ConnectFailed:
            raise
        except (InvalidProxy, ImportError):
            # A bad HTTPS_PROXY/WSS_PROXY/SOCKS_PROXY URL, or a SOCKS proxy without python-socks: retrying
            # cannot help. The exception text can carry proxy credentials, so it is not logged.
            raise _ConnectFailed(lifecycle.Verdict(False, 0.0, "storychat_proxy_invalid",
                                                   PROXY_MSG)) from None
        except Exception as exc:
            logger.debug("[%s] unexpected StoryChat connect failure", self.name, exc_info=True)
            raise _ConnectFailed(lifecycle.Verdict(
                True, lifecycle.BACKOFF_START_S, "storychat_unreachable",
                f"could not reach StoryChat ({type(exc).__name__}); retrying")) from None

    async def _handshake(self) -> None:
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
                logger.debug("[%s] StoryChat's welcome did not match protocol v1", self.name,
                             exc_info=True)
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
            self._approvals.clear()  # the relay dropped every pending approval with the socket
            self._mark_disconnected()
            if not verdict.retry:
                await self._go_fatal(verdict, retryable=False)
                return
            logger.warning("[%s] %s", self.name, verdict.message)
            if not await self._reconnect(verdict.backoff_start):
                return
            self._stop_tracked_turns()

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
        for aux in list(self._aux_tasks):
            aux.cancel()
        self._approvals.clear()
        self._turns = {}
        self._msg_kinds = {}

    async def _send_frame(self, frame: str) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(frame)
        except ConnectionClosed:
            return False
        return True

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._aux_tasks.add(task)
        task.add_done_callback(self._aux_tasks.discard)
        task.add_done_callback(self._log_aux_task_failure)

    def _log_aux_task_failure(self, task: "asyncio.Task") -> None:
        """Retrieving the exception here also stops asyncio warning it was never retrieved."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        logger.error("[%s] background task failed: %s", self.name, type(exc).__name__)
        logger.debug("[%s] background task failure", self.name, exc_info=exc)

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
        elif frame["type"] == "stop":
            await self._on_stop(frame)
        elif frame["type"] == "approval_decision":
            await self._on_approval_decision(frame)

    async def _on_message(self, frame: Dict[str, Any]) -> None:
        turn_id, chat_id = frame["turnId"], frame["chatId"]
        running = self._turns.get(chat_id)
        if (frame["userId"].lower() != self._user_id.lower()
                or (running is not None and not running.stop_requested)):
            logger.warning("[%s] refused turn %s (wrong user or a turn is already running)",
                           self.name, turn_id)
            await self._send_frame(protocol.turn_end(turn_id, chat_id, "error"))
            return
        source = self.build_source(chat_id=chat_id, chat_name=frame["chatName"], chat_type="dm",
                                   user_id=self._user_id, user_name=frame["userName"])
        event = MessageEvent(text=frame["text"], message_type=MessageType.TEXT, source=source,
                             message_id=frame["messageId"],
                             channel_prompt=frame["channelPrompt"] or None,
                             allow_gateway_control=False)
        problem = self._recheck_toolsets(turn_id, self._event_session_key(event))
        if problem is not None:
            logger.error("[%s] refused turn %s: %s; fix config.yaml and restart the gateway",
                         self.name, turn_id, problem)
            await self._send_frame(protocol.turn_end(turn_id, chat_id, "error"))
            return
        if self._hermes_session_busy(event):
            # A stopped turn still unwinding, or the gap between on_processing_complete and Hermes
            # releasing the session: Hermes would treat this message as a busy follow-up (busy ack
            # sent as this turn's reply, or folded into the old turn with no turn_end of its own).
            logger.warning("[%s] refused turn %s: Hermes is still finishing the previous turn",
                           self.name, turn_id)
            await self._send_frame(protocol.turn_end(turn_id, chat_id, "error"))
            return
        stored = _Turn(turn_id, chat_id, frame["messageId"], frame["userName"], frame["chatName"])
        self._turns[chat_id] = stored
        if running is not None:
            # Only reachable here when running.stop_requested (the guard above already refused any
            # other case) and Hermes already released its session: no Hermes run will ever end the
            # old turn now, so its approvals would stay answerable forever and the relay would never
            # get its turn_end. The new turn is stored first so a pending _send_stop for the old
            # one finds it replaced and cannot send a second turn_end.
            self._approvals.drop_chat(chat_id)
            self._msg_kinds = {k: v for k, v in self._msg_kinds.items() if v[0] != running.turn_id}
            logger.info("[%s] turn %s ended: %s", self.name, running.turn_id, "interrupted")
            await self._send_frame(protocol.turn_end(running.turn_id, chat_id, "interrupted"))
        logger.info("[%s] turn %s started", self.name, turn_id)
        try:
            await self.handle_message(event)
        finally:
            if event._gateway_accepted is not True:
                await self._end_unadmitted_turn(stored)

    async def _end_unadmitted_turn(self, turn: _Turn) -> None:
        """Hermes did not take the event (base.py handle_message sets no admission receipt when it
        drops it, e.g. _drop_unresolved), so no on_processing_complete will ever end this turn and
        the relay would keep the chat busy until MAX_TURN_MS."""
        current = self._turns.get(turn.chat_id)
        if current is None or current.turn_id != turn.turn_id:
            return
        self._turns.pop(turn.chat_id)
        self._approvals.drop_chat(turn.chat_id)
        self._msg_kinds = {k: v for k, v in self._msg_kinds.items() if v[0] != turn.turn_id}
        logger.warning("[%s] turn %s ended: error (Hermes did not accept it)", self.name, turn.turn_id)
        await self._send_frame(protocol.turn_end(turn.turn_id, turn.chat_id, "error"))

    def _recheck_toolsets(self, turn_id: str, session_key: str) -> Optional[str]:
        """Hermes re-reads config.yaml every turn (gateway/run_turn.py _load_gateway_config), so a
        config edit after startup could otherwise bypass the connect-time self-checks (spec §9.3).
        Loaded once so every check sees the same snapshot. Returns why the turn must be refused, or
        None."""
        chat_only = toolset_policy.is_chat_only(self._toolset_override)
        try:
            cfg = toolset_policy.load_gateway_config()
            effective = toolset_policy.effective_toolsets(self._toolset_override, cfg)
            unsafe = toolset_policy.unsafe_display_settings(cfg)
            approvals_off = not chat_only and _approvals_bypassed(session_key)
            memory_provider = toolset_policy.external_memory_provider(cfg)
        except Exception as exc:
            logger.error("[%s] toolset recheck for turn %s failed: %s", self.name, turn_id,
                         type(exc).__name__)
            logger.debug("[%s] toolset recheck failure", self.name, exc_info=True)
            return RECHECK_UNVERIFIED
        if ((chat_only and toolset_policy.leaked_toolsets(effective))
                or (not chat_only and "clarify" in effective)):
            return TOOLSETS_CHANGED
        if approvals_off:
            return APPROVALS_OFF
        if self._memory_provider_blocked(memory_provider, self._settings.allow_memory_provider):
            return MEMORY_PROVIDER_ON
        return DISPLAY_CHANGED if unsafe else None

    def _hermes_session_busy(self, event: MessageEvent) -> bool:
        """Whether Hermes still holds this chat's session. Heals a stale guard first, exactly as
        Hermes' handle_message does on entry (base.py), so refusing here never traps the chat."""
        session_key = self._event_session_key(event)
        self._heal_stale_session_lock(session_key)
        return session_key in self._active_sessions

    async def _on_stop(self, frame: Dict[str, Any]) -> None:
        turn = self._turns.get(frame["chatId"])
        if turn is None or turn.turn_id != frame["turnId"] or turn.stop_requested:
            return
        await self._dispatch_stop(turn)

    async def _dispatch_stop(self, turn: _Turn) -> None:
        """Hermes has no cancel API: send it the /stop command it answers while a turn runs."""
        if self._turns.get(turn.chat_id) is not turn:
            # Scheduled from a stale snapshot (_stop_tracked_turns spawns this a tick before it
            # runs): the turn already ended in between, so there is nothing left to stop.
            return
        stop_id = uuid.uuid4().hex
        stopping = replace(turn, stop_requested=True)
        self._turns[turn.chat_id] = stopping
        self._stop_ids[stop_id] = turn.turn_id
        while len(self._stop_ids) > MAX_STOP_IDS:
            del self._stop_ids[next(iter(self._stop_ids))]
        source = self.build_source(chat_id=turn.chat_id, chat_name=turn.chat_name, chat_type="dm",
                                   user_id=self._user_id, user_name=turn.user_name)
        event = MessageEvent(text="/stop", message_type=MessageType.TEXT, source=source,
                             message_id=stop_id, allow_gateway_control=True)
        self._spawn(self._send_stop(stopping, event))

    async def _send_stop(self, turn: _Turn, event: MessageEvent) -> None:
        if self._turns.get(turn.chat_id) is not turn:
            return  # the turn ended, or was replaced, before this task ran
        # Checked here, with no await before handle_message: Hermes may release the session between
        # _dispatch_stop and this task, and a /stop must never reach an idle session.
        if not self._hermes_session_busy(event):
            self._turns.pop(turn.chat_id, None)
            self._approvals.drop_chat(turn.chat_id)
            self._msg_kinds = {k: v for k, v in self._msg_kinds.items() if v[0] != turn.turn_id}
            logger.info("[%s] turn %s ended: %s", self.name, turn.turn_id, "interrupted")
            await self._send_frame(protocol.turn_end(turn.turn_id, turn.chat_id, "interrupted"))
            return
        logger.info("[%s] stopping turn %s", self.name, turn.turn_id)
        await self.handle_message(event)

    def _stop_tracked_turns(self) -> None:
        for turn in list(self._turns.values()):
            if not turn.stop_requested:
                self._spawn(self._dispatch_stop(turn))

    # ── outbound: send / edit / turn_end ────────────────────────────────────

    @staticmethod
    def truncate_message(content: str, max_length: int = 4096,
                         len_fn: Optional[Callable[[str], int]] = None) -> List[str]:
        """Hermes' own split without its " (i/n)" markers. The stream consumer splits a reply that
        overflows MAX_MESSAGE_LENGTH with this (gateway/stream_consumer.py _split_first_send), and
        the relay joins the segments into one saved reply, where a marker would stay mid-text."""
        chunks = BasePlatformAdapter.truncate_message(content, max_length, len_fn=len_fn)
        if len(chunks) < 2:
            return chunks
        return [chunk.removesuffix(f" ({i}/{len(chunks)})") for i, chunk in enumerate(chunks, 1)]

    def _strip_cursor(self, content: str) -> str:
        streaming = getattr(getattr(self.gateway_runner, "config", None), "streaming", None)
        cursor = getattr(streaming, "cursor", DEFAULT_STREAMING_CURSOR)
        return content[: -len(cursor)] if cursor and content.endswith(cursor) else content

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._send_text(chat_id, content, reply_to, metadata, force_status=False)

    async def send_or_update_status(self, chat_id: str, status_key: str, content: str, *,
                                    metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Hermes status lines (memory recall, compression, context warnings; gateway/run.py
        _send_or_update_status_coro) are never the character's reply. Each one is a new status
        message; status_key (Hermes' dedupe key) is not needed for that."""
        return await self._send_text(chat_id, content, None, metadata, force_status=True)

    async def send_private_notice(self, chat_id: str, user_id: Optional[str], content: str,
                                  reply_to: Optional[str] = None,
                                  metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Operational notices (see notice_delivery in __init__) go out as status."""
        return await self._send_text(chat_id, content, reply_to, metadata, force_status=True)

    async def emit_warning(self, chat_id: str, content: str, *, reply_to=None, metadata=None,
                           logical_platform=None) -> Optional[SendResult]:
        """Gateway diagnostics (context-hygiene failures, media fallbacks: base.py emit_warning) are
        never the character's reply. None means suppressed by the display settings, as in Hermes."""
        if not self.warning_notifications_enabled(logical_platform, chat_id=chat_id, metadata=metadata):
            return None
        return await self._send_text(chat_id, content, reply_to, metadata, force_status=True)

    async def _send_text(self, chat_id: str, content: str, reply_to: Optional[str],
                         metadata: Optional[Dict[str, Any]], *, force_status: bool) -> SendResult:
        turn = self._turns.get(chat_id)
        if turn is None:
            return SendResult(success=False, error="no active StoryChat turn for this chat")
        stop_turn_id = self._stop_ids.get(reply_to) if reply_to else None
        known_turn_id = self._msg_kinds.get(reply_to, (None, None))[0] if reply_to else None
        # A late send from a turn that already ended (its stop reply, its own reply_to, or a
        # stream-split chunk) must never attach to a DIFFERENT, newer turn of the same chat.
        if (reply_to is not None and reply_to != turn.message_id
                and stop_turn_id != turn.turn_id and known_turn_id != turn.turn_id):
            return SendResult(success=False, error="no active StoryChat turn for this chat")
        is_status = (force_status or stop_turn_id is not None
                     or (metadata or {}).get("_interim_send") is True)
        hermes_text = None if is_status else notices.classify(content)
        if hermes_text == notices.FAILURE:
            turn = replace(turn, failed=True)
            self._turns[chat_id] = turn
        kind = "status" if is_status or hermes_text else "reply"
        msg_id = uuid.uuid4().hex
        try:
            frame = protocol.send(turn.turn_id, chat_id, msg_id, self._strip_cursor(content),
                                  reply_to, kind)
        except protocol.FrameTooLarge as exc:
            return SendResult(success=False, error=str(exc))
        if not await self._send_frame(frame):
            return SendResult(success=False, error="not connected to StoryChat")
        self._msg_kinds[msg_id] = (turn.turn_id, kind)
        return SendResult(success=True, message_id=msg_id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        if self._approvals.remove(message_id) is not None:
            # Hermes timed the approval out and edits its card: tell StoryChat instead (spec §6).
            ok = await self._send_frame(protocol.approval_expired(message_id))
            return SendResult(success=ok, message_id=message_id,
                              error=None if ok else "not connected to StoryChat")
        known = self._msg_kinds.get(message_id)
        turn = self._turns.get(chat_id)
        if known is None or turn is None or turn.turn_id != known[0]:
            return SendResult(success=False, error="no active StoryChat turn for this message")
        try:
            frame = protocol.edit(turn.turn_id, chat_id, message_id, self._strip_cursor(content),
                                  finalize, known[1])
        except protocol.FrameTooLarge as exc:
            return SendResult(success=False, error=str(exc))
        if not await self._send_frame(frame):
            return SendResult(success=False, error="not connected to StoryChat")
        return SendResult(success=True, message_id=message_id)

    async def _record_delivery_obligation(self, event: MessageEvent, session_key: str,
                                          text_content: str, delivery_adapter: Any,
                                          is_ephemeral_response: bool) -> Optional[str]:
        """Opt out of Hermes' delivery ledger (base.py send_final_ledgered): it redelivers a failed
        final 30 s and 120 s later with no reply_to, when a newer turn of this chat may be running,
        and the relay would save it as that turn's reply. The relay already ended the old turn."""
        return None

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        turn = self._turns.get(event.source.chat_id)
        if turn is None or turn.message_id != event.message_id:
            return
        self._turns.pop(turn.chat_id, None)
        self._approvals.drop_chat(turn.chat_id)
        self._msg_kinds = {k: v for k, v in self._msg_kinds.items() if v[0] != turn.turn_id}
        if turn.stop_requested:
            reason = "interrupted"
        else:
            reason = "error" if turn.failed else _OUTCOME_REASONS.get(outcome, "error")
        logger.info("[%s] turn %s ended: %s", self.name, turn.turn_id, reason)
        if not await self._send_frame(protocol.turn_end(turn.turn_id, turn.chat_id, reason)):
            logger.warning("[%s] could not send turn_end for turn %s (not connected)",
                           self.name, turn.turn_id)

    # ── approvals (spec §6, §8, §9.3) ───────────────────────────────────────

    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        from gateway.platforms.base_exec_approval import approval_timeout_seconds
        from tools import approval as hermes_approval
        turn = self._turns.get(prompt.chat_id)
        if turn is None or self._ws is None:
            return SendResult(success=False, error="no active StoryChat turn for this chat")
        if turn.stop_requested:
            # After Stop only turn_end counts (spec §8): no new card; Hermes' /stop ends the wait.
            return SendResult(success=False, error="the StoryChat turn is stopping")
        request_id = correlate_request_id(
            prompt.command, prompt.description,
            hermes_approval.list_gateway_approvals(prompt.session_key),
            self._approvals.tracked_request_ids(prompt.session_key))
        if request_id is None:
            # Hermes always assigns a request_id (approval_gateway_wait.py _ApprovalEntry.__init__
            # setdefault). Failing to correlate means the card's own entry already left Hermes'
            # queue, so never fall back to a fresh uuid + later FIFO resolve: that could approve or
            # deny whatever unrelated command happens to be queue[0] when the decision arrives.
            return SendResult(success=False, error="approval is no longer pending in Hermes")
        # The relay closes with 4400 on a schema miss (fatal here), so only send a card it accepts.
        choices = tuple(dict.fromkeys(c for c in prompt.choices if c in protocol.APPROVAL_CHOICES))
        if not choices or not protocol.is_opaque_id(request_id):
            return SendResult(success=False, error="approval does not fit the StoryChat protocol")
        approval_id = request_id
        expires_at = int(time.time() * 1000) + int(approval_timeout_seconds()) * 1000
        try:
            frame = protocol.approval_request(
                turn.turn_id, prompt.chat_id, approval_id, prompt.command, prompt.description,
                choices, prompt.smart_denied, expires_at)
        except protocol.FrameTooLarge as exc:
            return SendResult(success=False, error=str(exc))
        # Book it before sending so a fast decision can never find it missing.
        self._approvals.add(PendingApproval(approval_id, prompt.session_key, prompt.chat_id,
                                            turn.turn_id, request_id, choices))
        if not await self._send_frame(frame):
            self._approvals.remove(approval_id)
            return SendResult(success=False, error="not connected to StoryChat")
        return SendResult(success=True, message_id=approval_id)

    async def _on_approval_decision(self, frame: Dict[str, Any]) -> None:
        approval_id = frame["approvalId"]
        resolved, reason, attempts_left = self._decide(approval_id, frame["choice"], frame.get("pin"))
        # Spec §12: logs may carry userId/connId/turnId/frame type/size only — never the approvalId,
        # the command text, or the PIN.
        logger.info("[%s] approval decision resolved=%s reason=%s", self.name, resolved, reason)
        await self._send_frame(protocol.approval_ack(approval_id, resolved, reason, attempts_left))

    def _decide(self, approval_id: str, choice: str,
                pin: Optional[str]) -> Tuple[bool, Optional[str], Optional[int]]:
        from tools import approval as hermes_approval
        entry = self._approvals.find(approval_id)
        if entry is None and self._approvals.resolved_choice(approval_id) is not None:
            return self._repeated_decision(approval_id, choice, pin)
        if entry is None or not entry.request_id:
            # Without Hermes' request_id, resolve_gateway_approval would pick its OLDEST entry.
            return False, "expired", None
        live = hermes_approval.list_gateway_approvals(entry.session_key)
        self._approvals.reconcile(entry.session_key, {e["request_id"] for e in live if e.get("request_id")})
        if self._approvals.find(approval_id) is None:
            return False, "expired", None
        if self._approvals.head(entry.session_key).approval_id != approval_id:
            return False, "not_oldest", None
        if choice not in entry.choices:
            return False, None, None
        if choice != "deny":
            reason, attempts_left = self._pin_guard.check(self._settings.approval_pin, pin)
            if reason is not None:
                return False, reason, attempts_left
        # Always target this exact request_id: never FIFO-resolve, or a PIN-approved decision
        # could act on a different, unrelated command (spec §6/§8).
        count = hermes_approval.resolve_gateway_approval(
            entry.session_key, choice, request_id=entry.request_id)
        self._approvals.remove(approval_id)
        if count <= 0:
            return False, "expired", None
        self._approvals.mark_resolved(approval_id, choice)
        return True, None, None

    def _repeated_decision(self, approval_id: str, choice: str,
                           pin: Optional[str]) -> Tuple[bool, Optional[str], Optional[int]]:
        """The relay stops waiting for an ack after 10 s, so a slow ack can make the user repeat a
        decision Hermes already applied. Answer with what happened, not "expired" (shown as
        "command was not run"); a different choice can no longer be applied."""
        if self._approvals.resolved_choice(approval_id) != choice:
            return False, None, None
        if choice != "deny":
            reason, attempts_left = self._pin_guard.check(self._settings.approval_pin, pin)
            if reason is not None:
                return False, reason, attempts_left
        return True, None, None


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

"""Zoom Team Chat platform adapter for the Hermes gateway.

Architecture (every path below was validated against the real Zoom Marketplace
in the standalone ``zoom_bridge`` debug toolkit):

* Inbound — a self-hosted ``aiohttp`` webhook server receives Zoom chatbot
  events, verifies the HMAC signature (or legacy verification token), answers
  the ``endpoint.url_validation`` handshake, dedupes retries, normalizes the
  two event shapes (``bot_notification`` and ``team_chat.app_mention``), and
  forwards each as a ``MessageEvent`` via ``self.handle_message``.

* Outbound — a ``ZoomClient`` (``client_credentials`` chatbot token, scope
  ``imchat:bot``) sends / edits / deletes messages through Zoom's REST API.

Key Zoom-specific conclusions baked in (see the project's investigation notes):

  * Chat type is inferred from the ``to_jid`` *suffix*, not from any
    ``isChannel`` field (none exists):
      - ``@conference.xmpp.zoom.us``  → channel (replies broadcast to everyone)
      - ``@xmpp.zoom.us`` (plain)     → DM (one-to-one)
      - ``<userJid>/<channelJid>``    → group-chat slash command (directed)
  * ``user_jid`` is REQUIRED on sends for user-managed apps (Zoom returns
    code 7001 without it) and harmless on channel sends (a channel ``to_jid``
    broadcasts to all members regardless of ``user_jid``). We learn and cache
    it from inbound payloads (Teams ``_conv_refs`` pattern).
  * ``bot_notification`` uses camelCase top-level fields; ``app_mention``
    nests under ``payload.object`` with snake_case. Both are normalized.
  * ``app_mention``'s ``message`` includes an ``@BotName`` prefix that must be
    stripped; ``at_items[].end_position`` is unreliable, so we match against
    ``robot_name``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from ._state import load_account_id, save_account_id, state_file
from ._zoom_client import TokenManager, ZoomClient, markdown_body, text_body
from ._zoom_verify import compute_encrypted_token, verify_request

log = logging.getLogger("zoom_plugin.adapter")

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - aiohttp is a Hermes hard dependency
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

_DEFAULT_PORT = 8646
_MAX_BODY_BYTES = 4 * 1024 * 1024
_WEBHOOK_PATH = "/zoom/webhook"
# Conservative per-message cap (Zoom message cards accept more, but smart
# chunking at ~4k keeps agent replies readable and matches sibling adapters).
MAX_MESSAGE_LENGTH = 4000
# event_ts dedup window — Zoom retries on a slow/missing ACK within a few
# minutes; 10 minutes is comfortably wide.
_DEDUP_TTL_SECONDS = 600


class ZoomAdapter(BasePlatformAdapter):
    """Zoom Team Chat gateway adapter (Chatbot API)."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    splits_long_messages = True  # send() chunks via truncate_message()

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("zoom"))
        extra = config.extra or {}

        self._client_id = extra.get("client_id") or os.getenv("ZOOM_CLIENT_ID", "")
        self._client_secret = (
            extra.get("client_secret") or os.getenv("ZOOM_CLIENT_SECRET", "")
        )
        self._bot_jid = extra.get("bot_jid") or os.getenv("ZOOM_BOT_JID", "")
        # Secret token (HMAC) from Features → Access.
        self._secret_token = (
            extra.get("secret_token") or os.getenv("ZOOM_SECRET_TOKEN", "")
        )
        self._port = _coerce_port(
            extra.get("port") or os.getenv("ZOOM_WEBHOOK_PORT", str(_DEFAULT_PORT))
        )

        self._token_manager: Optional[TokenManager] = None
        self._client: Optional[ZoomClient] = None
        self._runner: Optional["web.AppRunner"] = None

        # Per-chat delivery metadata learned from inbound payloads:
        #   chat_id -> {account_id, user_jid, chat_name, chat_type, ts}
        # Mirrors Teams' ``_conv_refs`` cache. ``user_jid`` is required on
        # sends for user-managed apps; ``account_id`` is required on every send.
        self._chat_meta: Dict[str, dict] = {}

        # account_id persisted across restarts (see _state.py). The chatbot
        # send/edit/delete APIs require an account_id that the
        # client_credentials chatbot token does NOT carry — the ONLY source
        # is the inbound webhook payload. We capture the first value we see
        # and cache it to $HERMES_HOME so cron pushes (no inbound context)
        # keep working after a restart. There is no ZOOM_ACCOUNT_ID env var;
        # account_id is never configured by the user.
        self._persisted_account_id: Optional[str] = load_account_id()
        if self._persisted_account_id:
            log.info("[zoom] loaded persisted account_id from %s", state_file())

        # In-memory event dedup keyed by (event, timestamp).
        self._seen: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error(
                "MISSING_SDK",
                "aiohttp not installed. Run: pip install aiohttp",
                retryable=False,
            )
            return False
        if not (
            self._client_id
            and self._client_secret
            and self._bot_jid
            and self._secret_token
        ):
            self._set_fatal_error(
                "MISSING_CREDENTIALS",
                "ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_BOT_JID, and "
                "ZOOM_SECRET_TOKEN are all required",
                retryable=False,
            )
            return False

        try:
            self._token_manager = TokenManager(
                self._client_id, self._client_secret
            )
            self._client = ZoomClient(self._token_manager, self._bot_jid)

            app = web.Application(client_max_size=_MAX_BODY_BYTES)
            app.router.add_get("/", _landing)
            app.router.add_get("/health", _health)
            app.router.add_post(_WEBHOOK_PATH, self._handle_webhook)
            app.on_cleanup.append(self._on_app_cleanup)

            self._runner = web.AppRunner(app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "0.0.0.0", self._port)
            await site.start()

            self._running = True
            self._mark_connected()
            log.info(
                "[zoom] Webhook server listening on 0.0.0.0:%d%s",
                self._port,
                _WEBHOOK_PATH,
            )
            return True
        except Exception as e:  # noqa: BLE001
            self._set_fatal_error(
                "CONNECT_FAILED", f"Zoom connection failed: {e}", retryable=True
            )
            log.error("[zoom] Failed to connect: %s", e)
            return False

    async def _on_app_cleanup(self, _: Any) -> None:
        await self._close_client()

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._token_manager is not None:
            await self._token_manager.close()
            self._token_manager = None

    async def disconnect(self) -> None:
        self._running = False
        if self._runner is not None:
            await self._runner.cleanup()  # triggers _on_app_cleanup → _close_client
            self._runner = None
        else:
            await self._close_client()
        self._mark_disconnected()
        log.info("[zoom] Disconnected")

    # ------------------------------------------------------------------ #
    # Inbound: webhook server
    # ------------------------------------------------------------------ #
    async def _handle_webhook(self, request: "web.Request") -> "web.StreamResponse":
        raw = await request.read()
        headers = {k.lower(): v for k, v in request.headers.items()}

        ok, method = verify_request(headers, raw, self._secret_token)
        if not ok:
            log.warning("[zoom] webhook REJECTED (%s)", method)
            return web.Response(status=401, text="unauthorized")

        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"_raw": raw.decode("utf-8", "replace")}

        event = body.get("event", "(no-event)")
        payload = body.get("payload", {}) or {}

        # endpoint.url_validation handshake (fires once when saving the URL).
        # ZOOM_SECRET_TOKEN is mandatory (connect() fatals without it), so the
        # encrypted token is always computed.
        if event == "endpoint.url_validation":
            plain = payload.get("plainToken", "")
            if plain:
                enc = compute_encrypted_token(self._secret_token, plain)
                return web.json_response(
                    {"plainToken": plain, "encryptedToken": enc}
                )
            return web.json_response({"plainToken": plain})

        # ACK fast (Zoom retries on a slow response), then process in the
        # background. handle_message() itself returns quickly by spawning its
        # own background tasks, but detaching here keeps the ACK unconditional.
        asyncio.create_task(self._process_inbound(event, payload))
        return web.Response(status=200, text="OK")

    async def _process_inbound(self, event: str, payload: dict) -> None:
        try:
            if event not in ("bot_notification", "team_chat.app_mention"):
                log.debug("[zoom] ignoring event %s", event)
                return

            fields = self._extract_msg_fields(payload, event)
            dedup_key = self._dedup_key(event, payload)
            if dedup_key and self._is_dup(dedup_key):
                log.info("[zoom] dup event %s — skipping", dedup_key)
                return

            to_jid = fields["to_jid"]
            if not to_jid:
                log.warning("[zoom] inbound %s has no to_jid — dropping", event)
                return

            chat_type = _classify_chat_type(to_jid)
            user_jid = fields["user_jid"]
            user_name = fields["user_name"]
            text = fields["text"]

            # Self-bot guard: Zoom doesn't echo the bot's own messages, but a
            # misconfigured subscription could. Never reply to ourself.
            if user_jid and user_jid == self._bot_jid:
                log.debug("[zoom] ignoring self message")
                return

            # Cache delivery metadata so send() can route back to this chat.
            # account_id comes ONLY from the inbound webhook payload (no env
            # var). Persist the first value we ever obtain so later cron
            # pushes — which run out-of-process with no inbound context — can
            # reload it from state.json.
            inbound_account_id = fields["account_id"]
            account_id = inbound_account_id or self._persisted_account_id
            if inbound_account_id and self._persisted_account_id is None:
                if save_account_id(inbound_account_id):
                    self._persisted_account_id = inbound_account_id
            self._chat_meta[to_jid] = {
                "account_id": account_id,
                "user_jid": user_jid,
                "chat_name": fields["chat_name"],
                "chat_type": chat_type,
                "ts": time.time(),
            }

            source = self.build_source(
                chat_id=to_jid,
                chat_name=fields["chat_name"] or None,
                chat_type=chat_type,
                user_id=user_jid or None,
                user_name=user_name or None,
            )

            message_id = fields.get("message_id")
            reply_to_main = fields.get("reply_main_message_id")
            event_obj = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=message_id,
                reply_to_message_id=reply_to_main,
            )
            await self.handle_message(event_obj)
        except Exception:
            log.exception("[zoom] inbound processing failed")

    # ------------------------------------------------------------------ #
    # Payload normalization (validated against real captures)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_msg_fields(payload: dict, event: str) -> dict:
        """Normalize the two event shapes into one dict.

        ``bot_notification`` uses camelCase top-level fields; ``app_mention``
        nests under ``payload.object`` with snake_case. Both carry the same
        logical info. ``app_mention``'s ``message`` also includes an
        ``@BotName`` prefix that must be stripped.
        """
        if event == "team_chat.app_mention":
            obj = payload.get("object", {}) or {}
            msg = obj.get("message", "")
            # Strip the "@BotName" prefix by matching robot_name. at_items'
            # end_position is unreliable (off-by-one vs the real token length).
            robot_name = obj.get("robot_name", "")
            if robot_name and msg.lower().startswith("@" + robot_name.lower()):
                msg = msg[len(robot_name) + 1 :].lstrip()
            elif msg.startswith("@"):
                parts = msg.split(None, 1)
                if len(parts) == 2:
                    msg = parts[1]
            return {
                "to_jid": obj.get("to_jid", ""),
                "account_id": payload.get("account_id", ""),
                "user_jid": payload.get("operator_jid", ""),
                "user_name": payload.get("operator", ""),
                "text": msg,
                "chat_name": obj.get("channel_name", ""),
                "message_id": obj.get("message_id"),
                "reply_main_message_id": None,
            }
        # bot_notification (DM or channel slash command)
        return {
            "to_jid": payload.get("toJid") or payload.get("to_jid", ""),
            "account_id": payload.get("accountId") or payload.get("account_id", ""),
            "user_jid": payload.get("userJid") or payload.get("user_jid", ""),
            "user_name": payload.get("userName", ""),
            "text": payload.get("cmd", ""),
            "chat_name": payload.get("channelName", ""),
            "message_id": None,
            "reply_main_message_id": payload.get("replyMainMessageId"),
        }

    @staticmethod
    def _dedup_key(event: str, payload: dict) -> str:
        if event == "team_chat.app_mention":
            obj = payload.get("object", {}) or {}
            mid = obj.get("message_id") or ""
            if mid:
                return f"app_mention:{mid}"
            return f"app_mention:{obj.get('timestamp')}"
        # bot_notification
        return f"bot_notification:{payload.get('timestamp')}:{payload.get('triggerId')}"

    def _is_dup(self, key: str) -> bool:
        now = time.time()
        expired = [k for k, v in self._seen.items() if now - v > _DEDUP_TTL_SECONDS]
        for k in expired:
            self._seen.pop(k, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False

    # ------------------------------------------------------------------ #
    # Outbound
    # ------------------------------------------------------------------ #
    def _delivery_ctx(self, chat_id: str) -> dict:
        meta = self._chat_meta.get(chat_id, {})
        account_id = meta.get("account_id") or self._persisted_account_id
        user_jid = meta.get("user_jid", "")
        chat_type = meta.get("chat_type") or _classify_chat_type(chat_id)
        return {"account_id": account_id, "user_jid": user_jid, "chat_type": chat_type}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="Zoom client not initialized")
        ctx = self._delivery_ctx(chat_id)
        account_id = ctx["account_id"]
        if not account_id:
            return SendResult(success=False, error="no account_id for chat")

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, max_length=self.MAX_MESSAGE_LENGTH)
        last_message_id: Optional[str] = None

        for idx, chunk in enumerate(chunks):
            try:
                resp = await self._client.send(
                    chat_id,
                    account_id,
                    markdown_body(chunk),
                    user_jid=ctx["user_jid"],
                    reply_to=reply_to if idx == 0 else "",
                    is_markdown=True,
                )
            except Exception as e:  # noqa: BLE001
                return SendResult(success=False, error=str(e), retryable=True)

            if resp.get("_status", 200) >= 400:
                return SendResult(
                    success=False,
                    error=resp.get("message") or f"Zoom HTTP {resp.get('_status')}",
                    raw_response=resp,
                    retryable=False,
                )
            last_message_id = resp.get("message_id")
        return SendResult(success=True, message_id=last_message_id, raw_response=resp)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        meta = self._chat_meta.get(chat_id, {})
        return {
            "name": meta.get("chat_name") or chat_id,
            "type": meta.get("chat_type") or _classify_chat_type(chat_id),
            "chat_id": chat_id,
        }


# ── helpers ──────────────────────────────────────────────────────────────────
def _classify_chat_type(to_jid: str) -> str:
    """Infer chat type from the ``to_jid`` suffix.

    Zoom has no ``isChannel`` field; the JID suffix is the only stable signal:
      - ``@conference.xmpp.zoom.us`` → channel
      - ``<userJid>/<channelJid>``    → group-chat slash command (directed)
      - ``@xmpp.zoom.us`` (plain)     → DM
    """
    if "@conference.xmpp" in to_jid:
        return "channel"
    if "/" in to_jid and "@" in to_jid:
        return "group"
    return "dm"


def _coerce_port(value: Any, *, default: int = _DEFAULT_PORT) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


async def _health(_: "web.Request") -> "web.Response":
    return web.Response(text="ok")


async def _landing(_: "web.Request") -> "web.Response":
    """Human-readable index at ``/``.

    Replaces aiohttp's default 404 so that poking the root URL — e.g. after
    standing up a Cloudflare tunnel, or pointing a browser/redirect at the
    service host — shows what's actually running here instead of a bare
    "Not Found". Zoom posts to ``/zoom/webhook``; nothing meaningful happens
    at ``/`` itself.
    """
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Zoom Team Chat gateway — Hermes Agent</title>
  <style>
    body { font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
           max-width: 38rem; margin: 3rem auto; padding: 0 1rem; color: #1f2933; }
    code { background: #f1f3f5; padding: .1em .35em; border-radius: 3px; font-size: .92em; }
    h1 { font-size: 1.4rem; margin-bottom: .25rem; }
    p { margin: .6rem 0; }
    .muted { color: #627d98; }
  </style>
</head>
<body>
  <h1>🎥 Zoom Team Chat gateway</h1>
  <p class="muted">Hermes Agent platform plugin · inbound webhook server</p>
  <p>This service receives Zoom Team Chat chatbot events at
     <code>POST /zoom/webhook</code> and replies through Zoom's chatbot
     message API. There's nothing to see here directly.</p>
  <p>Service status: <code>GET /health</code></p>
</body>
</html>
"""
    return web.Response(text=html, content_type="text/html")


# --------------------------------------------------------------------------- #
# Plugin entry point + config hooks
# --------------------------------------------------------------------------- #
def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (os.getenv("ZOOM_CLIENT_ID") or extra.get("client_id"))
        and (os.getenv("ZOOM_CLIENT_SECRET") or extra.get("client_secret"))
        and (os.getenv("ZOOM_BOT_JID") or extra.get("bot_jid"))
        and (os.getenv("ZOOM_SECRET_TOKEN") or extra.get("secret_token"))
    )


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars before adapter construction.

    Lets ``hermes gateway status`` / ``get_connected_platforms()`` reflect an
    env-only setup without instantiating the adapter.
    """
    client_id = os.getenv("ZOOM_CLIENT_ID", "").strip()
    client_secret = os.getenv("ZOOM_CLIENT_SECRET", "").strip()
    bot_jid = os.getenv("ZOOM_BOT_JID", "").strip()
    secret_token = os.getenv("ZOOM_SECRET_TOKEN", "").strip()
    if not (client_id and client_secret and bot_jid and secret_token):
        return None
    seed: dict = {
        "client_id": client_id,
        "client_secret": client_secret,
        "bot_jid": bot_jid,
        "secret_token": secret_token,
    }
    port = os.getenv("ZOOM_WEBHOOK_PORT", "").strip()
    if port:
        try:
            seed["port"] = int(port)
        except ValueError:
            pass
    home = os.getenv("ZOOM_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("ZOOM_HOME_CHANNEL_NAME", "Home"),
        }
    return seed


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process delivery for cron jobs that run separately from the gateway.

    Opens an ephemeral token + client, sends one message, and closes. ``user_jid``
    is unknown here (no inbound context), so we omit it — which works for channel
    targets (a channel ``to_jid`` broadcasts to everyone) but returns code 7001
    for user-managed DM targets. Point ``ZOOM_HOME_CHANNEL`` at a channel.
    """
    if not AIOHTTP_AVAILABLE:
        return {"error": "aiohttp not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    client_id = extra.get("client_id") or os.getenv("ZOOM_CLIENT_ID", "")
    client_secret = extra.get("client_secret") or os.getenv("ZOOM_CLIENT_SECRET", "")
    # account_id is never configured by the user — it's learned from the
    # inbound webhook and persisted to state.json (see _state.py). This cron
    # path runs out-of-process and can't see the adapter's in-memory cache,
    # so re-read the file. If it's empty, the bot hasn't received any message
    # yet — send it one DM to capture the account_id.
    account_id = load_account_id()
    bot_jid = extra.get("bot_jid") or os.getenv("ZOOM_BOT_JID", "")
    if not (client_id and client_secret and account_id and bot_jid):
        return {"error": "account_id not yet captured — send the bot one message so it can learn it, then retry"}

    tm = TokenManager(client_id, client_secret)
    client = ZoomClient(tm, bot_jid)
    try:
        resp = await client.send_text(chat_id, account_id, message)
        if resp.get("_status", 200) >= 400:
            return {"error": resp.get("message") or f"Zoom HTTP {resp.get('_status')}"}
        return {"success": True, "message_id": resp.get("message_id")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    finally:
        await client.close()


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="zoom",
        label="Zoom Team Chat",
        adapter_factory=lambda cfg: ZoomAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[
            "ZOOM_CLIENT_ID",
            "ZOOM_CLIENT_SECRET",
            "ZOOM_BOT_JID",
            "ZOOM_SECRET_TOKEN",
        ],
        install_hint="pip install aiohttp",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ZOOM_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ZOOM_ALLOWED_USERS",
        allow_all_env="ZOOM_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🎥",
        platform_hint=(
            "You are chatting via Zoom Team Chat. It renders a subset of "
            "markdown — bold (**text**), italic (*text*), inline code "
            "(`code`), and links work. Keep responses clear and concise. "
            "The bot responds to DMs, channel slash commands, and @mentions."
        ),
    )

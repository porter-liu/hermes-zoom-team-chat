"""Zoom chatbot client — token management + send / edit / delete messages.

Reused verbatim from the standalone ``zoom_bridge`` debug toolkit, where every
code path here was validated against the real Zoom Marketplace.

Token flow (``client_credentials`` grant):
  - grant_type = client_credentials  (no refresh token, no account_id in the request)
  - scope      = imchat:bot
  - expires    = 1 hour, refreshed by simply requesting a new one

Message endpoints:
  POST   /v2/im/chat/messages              → sendChatbotMessage
  PUT    /v2/im/chat/messages/{message_id} → editChatbotMessage  (streaming / corrections)
  DELETE /v2/im/chat/messages/{message_id} → deleteChatbotMessage

All calls carry ``Authorization: Bearer <chatbot_token>``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any

import aiohttp

log = logging.getLogger("zoom_plugin.client")

_OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"
_API = "https://api.zoom.us/v2"
# How long before expiry we proactively refresh the chatbot token.
_DEFAULT_REFRESH_SKEW_SECONDS = 300


def text_body(text: str) -> list[dict]:
    """A content.body with a single plain-text message block."""
    return [{"type": "message", "text": text}]


def markdown_body(markdown: str) -> list[dict]:
    """A content.body whose message block is rendered as markdown by Zoom.

    Must be paired with ``is_markdown_support=True`` on the send payload.
    """
    return [{"type": "message", "text": markdown}]


class TokenManager:
    """In-memory chatbot-token cache with a lock-guarded refresh.

    Concurrent senders share a single token; the lock prevents a thundering
    herd of refreshes when several messages fire at once.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        refresh_skew_seconds: int = _DEFAULT_REFRESH_SKEW_SECONDS,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_skew = refresh_skew_seconds
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._session = session
        self._owns_session = False

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    @property
    def cached(self) -> str | None:
        if self._token and time.time() < self._expires_at - self._refresh_skew:
            return self._token
        return None

    async def get(self) -> str:
        cached = self.cached
        if cached:
            return cached
        async with self._lock:
            cached = self.cached  # re-check inside the lock
            if cached:
                return cached
            await self._refresh()
            assert self._token is not None
            return self._token

    async def _refresh(self) -> None:
        session = await self._session_get()
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        log.debug("Refreshing chatbot token (client_credentials)…")
        async with session.post(
            _OAUTH_TOKEN_URL,
            params={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}"},
        ) as resp:
            data: dict = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    f"chatbot token refresh failed (HTTP {resp.status}): {data}"
                )
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None


class ZoomClient:
    """Thin async wrapper over Zoom's chatbot message endpoints."""

    def __init__(
        self,
        token_manager: TokenManager,
        robot_jid: str,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._tokens = token_manager
        self._robot_jid = robot_jid
        self._session = session
        self._owns_session = False

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def _headers(self) -> dict:
        token = await self._tokens.get()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def send(
        self,
        to_jid: str,
        account_id: str,
        body: list[dict],
        *,
        user_jid: str = "",
        reply_to: str = "",
        is_markdown: bool = False,
    ) -> dict:
        """Send a message. Returns Zoom's response JSON (contains ``message_id``).

        ``user_jid`` is REQUIRED for user-managed chatbot apps (Zoom returns
        code 7001 without it) and is safe to pass for channels (a channel
        ``to_jid`` broadcasts to everyone regardless of ``user_jid``).
        """
        payload: dict[str, Any] = {
            "robot_jid": self._robot_jid,
            "to_jid": to_jid,
            "account_id": account_id,
            "content": {"body": body},
        }
        if user_jid:
            payload["user_jid"] = user_jid
        if is_markdown:
            payload["is_markdown_support"] = True
        if reply_to:
            payload["reply_to"] = reply_to
        return await self._post("/im/chat/messages", payload)

    async def send_text(
        self, to_jid: str, account_id: str, text: str, *, user_jid: str = "", reply_to: str = ""
    ) -> dict:
        return await self.send(
            to_jid, account_id, text_body(text), user_jid=user_jid, reply_to=reply_to
        )

    async def send_markdown(
        self, to_jid: str, account_id: str, markdown: str, *, user_jid: str = "", reply_to: str = ""
    ) -> dict:
        return await self.send(
            to_jid,
            account_id,
            markdown_body(markdown),
            user_jid=user_jid,
            reply_to=reply_to,
            is_markdown=True,
        )

    async def edit(
        self,
        message_id: str,
        to_jid: str,
        account_id: str,
        body: list[dict],
        *,
        user_jid: str = "",
        is_markdown: bool = False,
    ) -> dict:
        """Edit an existing message by id (used for streaming-style updates)."""
        payload = {
            "robot_jid": self._robot_jid,
            "to_jid": to_jid,
            "account_id": account_id,
            "content": {"body": body},
        }
        if user_jid:
            payload["user_jid"] = user_jid
        if is_markdown:
            payload["is_markdown_support"] = True
        return await self._put(f"/im/chat/messages/{message_id}", payload)

    async def edit_markdown(
        self, message_id: str, to_jid: str, account_id: str, markdown: str, *, user_jid: str = ""
    ) -> dict:
        return await self.edit(
            message_id,
            to_jid,
            account_id,
            markdown_body(markdown),
            user_jid=user_jid,
            is_markdown=True,
        )

    async def delete(self, message_id: str, to_jid: str, account_id: str) -> dict:
        params = {"robot_jid": self._robot_jid, "to_jid": to_jid, "account_id": account_id}
        return await self._delete(f"/im/chat/messages/{message_id}", params)

    # ── low-level transport ──────────────────────────────────────────────────
    async def _post(self, path: str, payload: dict) -> dict:
        session = await self._session_get()
        async with session.post(
            f"{_API}{path}", json=payload, headers=await self._headers()
        ) as resp:
            return await self._resp(resp, "POST", path, payload)

    async def _put(self, path: str, payload: dict) -> dict:
        session = await self._session_get()
        async with session.put(
            f"{_API}{path}", json=payload, headers=await self._headers()
        ) as resp:
            return await self._resp(resp, "PUT", path, payload)

    async def _delete(self, path: str, params: dict) -> dict:
        session = await self._session_get()
        async with session.delete(
            f"{_API}{path}", params=params, headers=await self._headers()
        ) as resp:
            return await self._resp(resp, "DELETE", path, params)

    @staticmethod
    async def _resp(resp: aiohttp.ClientResponse, method: str, path: str, sent: Any) -> dict:
        try:
            data = await resp.json()
        except Exception:
            data = {"_raw": (await resp.text())}
        if resp.status >= 400:
            log.warning(
                "Zoom %s %s → HTTP %s — request body: %s — response: %s",
                method, path, resp.status, _compact(sent), data,
            )
            data.setdefault("_status", resp.status)
            data.setdefault("_method", method)
            data.setdefault("_path", path)
            data["_sent_body"] = sent
        else:
            log.debug("Zoom %s %s → HTTP %s", method, path, resp.status)
        return data

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
        await self._tokens.close()


def _compact(obj: Any, limit: int = 300) -> str:
    s = repr(obj)
    return s if len(s) <= limit else s[: limit - 3] + "..."

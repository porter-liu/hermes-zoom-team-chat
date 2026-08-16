# 💬 Hermes · Zoom Team Chat Platform Plugin

A [Hermes Agent](https://hermes-agent.nousresearch.com/) **platform plugin** that lets
your agent talk over **Zoom Team Chat**. Once installed, your Hermes agent can
reply to **direct messages**, **channel slash commands**, and **@mentions** —
just like any other messaging channel.

> [English](README.md) · [简体中文](README.zh-CN.md)

---

## How it works

```
                         ┌─────────────────────────────────────────────┐
  Zoom Team Chat ───────▶│  Zoom Marketplace Chatbot app               │
  (DM / slash / @mention)│  ─ webhook event ──▶  this plugin           │
                         │     (POST /zoom/webhook, HMAC-verified)     │
                         └──────────────────────┬──────────────────────┘
                                                │ MessageEvent
                                                ▼
                                        ┌───────────────┐
                                        │  Hermes Agent │
                                        └───────┬───────┘
                                                │ reply
                          ┌─────────────────────▼──────────────────────┐
                          │ ZoomClient (client_credentials token,       │
                          │   scope imchat:bot) → send/edit/delete      │
                          └─────────────────────────────────────────────┘
```

- **Inbound** — a self-hosted `aiohttp` server receives Zoom chatbot events,
  verifies the `x-zm-signature` HMAC, answers the `endpoint.url_validation` handshake, de-duplicates retries, and
  normalizes both inbound event shapes (`bot_notification` and
  `team_chat.app_mention`) into a single `MessageEvent`.
- **Outbound** — a `ZoomClient` authenticates with the `client_credentials`
  OAuth grant (chatbot token, scope `imchat:bot`) and sends / edits / deletes
  messages through Zoom's REST API. Replies are rendered as **markdown** and
  long replies are automatically split into multiple messages.

## Requirements

- A working **Hermes Agent** install (gateway enabled).
- A **Zoom Team Chat Chatbot app** created on the
  [Zoom App Marketplace](https://marketplace.zoom.us/) — this gives you the
  credentials and webhook configuration the plugin needs.
  ➜ **Follow the step-by-step guide: [Zoom app setup](docs/zoom-app-setup.md)**
- A **public HTTPS endpoint** that forwards to the plugin's webhook port
  (e.g. `ngrok`, `cloudflared`, or a reverse proxy). Zoom must be able to
  reach your webhook to deliver events.

## Installation

This is a **platform plugin**. Drop it under the platforms directory:

```bash
mkdir -p ~/.hermes/plugins/platforms
cp -r . ~/.hermes/plugins/platforms/zoom
```

or install it directly from Git:

```bash
hermes plugins install porter-liu/hermes-zoom-team-chat
```

Restart Hermes so the plugin is discovered, then follow the
[setup guide](docs/zoom-app-setup.md) to finish — it walks through exposing
your webhook, registering the Zoom app, setting the credentials below,
enabling the channel, and pairing with your Zoom account.

## Configuration

The plugin reads its credentials from environment variables (they're also
picked up from `~/.hermes/config.yaml`'s `gateway.platforms.zoom` section, or
prompted by `hermes plugins install`).

### Required

> 📌 Where to find each of these in the Zoom Marketplace app is shown with
> screenshots in [Step 2 of the setup guide](docs/zoom-app-setup.md#step-2--register-a-chatbot-app-on-the-zoom-marketplace).

| Variable | Description |
| --- | --- |
| `ZOOM_CLIENT_ID` | Client ID from the app's **Basic Information** page. |
| `ZOOM_CLIENT_SECRET` | Client Secret from the **Basic Information** page. |
| `ZOOM_BOT_JID` | Bot JID (robot JID) from the Chat Subscription panel under **Features → Surface**, e.g. `xxx@xmpp.zoom.us`. |
| `ZOOM_SECRET_TOKEN` | Secret token from **Features → Access**, used to verify the HMAC signature on inbound webhooks. Enable *Signature* on the Team Chat Subscription. |

### Optional

| Variable | Default | Description |
| --- | --- | --- |
| `ZOOM_WEBHOOK_PORT` | `8646` | Port for the inbound webhook HTTP server. Point your tunnel here. |
| `ZOOM_HOME_CHANNEL` | — | Default chat JID for cron / notification delivery (a channel or user JID). |
| `ZOOM_HOME_CHANNEL_NAME` | `Home` | Display name for the home channel. |
| `ZOOM_ALLOWED_USERS` | — | Comma-separated user JIDs allowed to talk to the bot. |
| `ZOOM_ALLOW_ALL_USERS` | — | When set, allow any user (disables the allow-list). |

> ℹ️ **No `account_id` to configure** — it's auto-captured from your first
> inbound message and cached for cron / push delivery. See
> [Notes & troubleshooting](#notes--troubleshooting) for the "seed it" tip.
>
> 💡 Set the four required variables above before starting Hermes. The
> webhook server refuses to start with `MISSING_CREDENTIALS` if any are blank.

## Supported features

- ✅ **Direct messages** (DM)
- ✅ **Channel slash commands** (group-chat, directed)
- ✅ **@mentions** in channels (`team_chat.app_mention`), with the `@BotName`
  prefix automatically stripped
- ✅ **Markdown replies** — Zoom renders a subset: **bold**, *italic*,
  `inline code`, and links
- ✅ **Automatic long-message splitting** (≈ 4 KB chunks)
- ✅ **Message edit / delete** via the chatbot API (used for streaming-style
  corrections)
- ✅ **Inbound retry de-duplication** (10-minute window)
- ✅ **Self-bot guard** (never replies to its own messages)
- ✅ **Cron / notification delivery** via `ZOOM_HOME_CHANNEL`

## Chat-type inference

Zoom exposes no `isChannel` flag, so the plugin infers the chat type from the
`to_jid` suffix:

| `to_jid` pattern | Chat type |
| --- | --- |
| `*@conference.xmpp.zoom.us` | Channel (broadcasts to all members) |
| `<userJid>/<channelJid>` | Group-chat slash command (directed) |
| `*@xmpp.zoom.us` | Direct message (1:1) |

## Notes & troubleshooting

- **`user_jid` is required for DMs.** User-managed chatbot apps return Zoom
  error code `7001` without it. The plugin learns and caches `user_jid` from
  inbound payloads, so it's always present for chats the bot has already seen.
  For outbound cron pushes with no prior inbound context, target a **channel**
  (`ZOOM_HOME_CHANNEL`) rather than a DM.
- **Account ID is auto-learned.** There is no `ZOOM_ACCOUNT_ID` setting — the
  plugin captures the `account_id` from the first inbound message and
  persists it to `$HERMES_HOME/plugins/zoom-platform/state.json`. If cron
  pushes fail with "account_id not yet captured", send the bot one DM to seed
  it.
- **Webhook 401s.** Every legitimate Zoom request must carry a valid
  `x-zm-signature`. A missing or mismatched signature is rejected with HTTP
  401 — double-check that `ZOOM_SECRET_TOKEN` matches the token in **Features
  → Access**, and that *Signature* is enabled on the Team Chat Subscription.

## Architecture reference

| File | Responsibility |
| --- | --- |
| [`plugin.yaml`](plugin.yaml) | Plugin manifest, required/optional env vars. |
| [`adapter.py`](adapter.py) | `ZoomAdapter` — webhook server, payload normalization, inbound routing, outbound send. |
| [`_zoom_client.py`](_zoom_client.py) | `TokenManager` (chatbot token cache) + `ZoomClient` (send/edit/delete). |
| [`_zoom_verify.py`](_zoom_verify.py) | HMAC signature verification, `endpoint.url_validation` handshake. |
| [`_state.py`](_state.py) | Durable `account_id` persistence (`$HERMES_HOME/plugins/<name>/state.json`). |

The adapter extends `BasePlatformAdapter` and registers itself via
`ctx.register_platform(...)` in the standard Hermes plugin `register(ctx)`
entry point. See the Hermes
[Adding Platform Adapters](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)
guide for the interface contract.

## License

MIT — see [LICENSE](LICENSE).

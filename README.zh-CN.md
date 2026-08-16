# 💬 Hermes · Zoom Team Chat 平台插件

一个 [Hermes Agent](https://hermes-agent.nousresearch.com/) **平台插件**，让你的
agent 可以通过 **Zoom Team Chat** 交流。安装后，你的 Hermes agent 就能回复
**私信（DM）**、**频道斜杠命令** 以及 **@提及** —— 和其它消息渠道一样。

> [English](README.md) · [简体中文](README.zh-CN.md)

---

## 工作原理

```
                         ┌─────────────────────────────────────────────┐
  Zoom Team Chat ───────▶│  Zoom Marketplace Chatbot 应用              │
  (私信 / 斜杠命令 / @提及)│  ─ webhook 事件 ──▶  本插件                │
                         │     (POST /zoom/webhook，HMAC 校验)         │
                         └──────────────────────┬──────────────────────┘
                                                │ MessageEvent
                                                ▼
                                        ┌───────────────┐
                                        │  Hermes Agent │
                                        └───────┬───────┘
                                                │ 回复
                          ┌─────────────────────▼──────────────────────┐
                          │ ZoomClient（client_credentials 令牌，        │
                          │   scope imchat:bot）→ 发送 / 编辑 / 删除     │
                          └─────────────────────────────────────────────┘
```

- **入站** —— 一个自托管的 `aiohttp` 服务器接收 Zoom chatbot 事件，校验
  `x-zm-signature` HMAC，应答
  `endpoint.url_validation` 握手，对重试事件去重，并把两种入站事件格式
  （`bot_notification` 与 `team_chat.app_mention`）归一化为统一的
  `MessageEvent`。
- **出站** —— `ZoomClient` 使用 `client_credentials` OAuth 授权（chatbot 令牌，
  scope `imchat:bot`）通过 Zoom REST API 发送 / 编辑 / 删除消息。回复以
  **markdown** 渲染，过长的回复会自动拆分成多条消息。

## 前置要求

- 一套可正常运行的 **Hermes Agent**（已启用网关）。
- 一个在 [Zoom App Marketplace](https://marketplace.zoom.us/) 上创建的
  **Zoom Team Chat Chatbot 应用** —— 它会提供插件所需的凭据和 webhook 配置。
  ➜ **按步骤操作指南请见：[Zoom 应用创建](docs/zoom-app-setup.zh-CN.md)**
- 一个**公网 HTTPS 端点**，可转发到插件的 webhook 端口
  （例如 `ngrok`、`cloudflared` 或反向代理）。Zoom 必须能访问到你的
  webhook 才能投递事件。

## 安装

这是一个**平台插件**。把它放到 platforms 目录下即可：

```bash
mkdir -p ~/.hermes/plugins/platforms
cp -r . ~/.hermes/plugins/platforms/zoom
```

或直接从 Git 安装：

```bash
hermes plugins install porter-liu/hermes-zoom-team-chat
```

重启 Hermes 以完成插件发现，然后按 [配置指南](docs/zoom-app-setup.zh-CN.md)
完成剩余步骤 —— 它会带你走完暴露 webhook、注册 Zoom 应用、设置下面的凭据、
启用渠道并完成配对。

## 配置

插件从环境变量读取凭据（也可由 `~/.hermes/config.yaml` 中
`gateway.platforms.zoom` 段读取，或在 `hermes plugins install` 时交互式填入）。

### 必填项

> 📌 这些值在 Zoom Marketplace 应用里从哪里获取，配置指南
> [第 2 步](docs/zoom-app-setup.zh-CN.md#第-2-步--在-zoom-marketplace-上注册-chatbot-应用)有截图说明。

| 变量 | 说明 |
| --- | --- |
| `ZOOM_CLIENT_ID` | 应用 **Basic Information** 页面的 Client ID。 |
| `ZOOM_CLIENT_SECRET` | **Basic Information** 页面的 Client Secret。 |
| `ZOOM_BOT_JID` | **Features → Surface** 下 Chat Subscription 面板中的 Bot JID（robot JID），例如 `xxx@xmpp.zoom.us`。 |
| `ZOOM_SECRET_TOKEN` | **Features → Access** 中的 secret token，用于校验入站 webhook 的 HMAC 签名。需要在 Team Chat Subscription 上启用 *Signature*。 |

### 选填项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ZOOM_WEBHOOK_PORT` | `8646` | 入站 webhook HTTP 服务器端口。把你的隧道指向这里。 |
| `ZOOM_HOME_CHANNEL` | — | 用于 cron / 通知投递的默认聊天 JID（频道或用户 JID）。 |
| `ZOOM_HOME_CHANNEL_NAME` | `Home` | Home 频道的显示名称。 |
| `ZOOM_ALLOWED_USERS` | — | 允许与机器人对话的用户 JID，逗号分隔。 |
| `ZOOM_ALLOW_ALL_USERS` | — | 设置后允许所有用户（关闭白名单）。 |

> ℹ️ **无需配置 `account_id`** —— 它会从第一条入站消息自动捕获并缓存，供
> cron / 通知推送使用。“如何写入”的提示见 [说明与排错](#说明与排错)。
>
> 💡 启动 Hermes 前请先设置上述四个必填变量。任一为空时，webhook 服务器会以
> `MISSING_CREDENTIALS` 拒绝启动。

## 支持的功能

- ✅ **私信（DM）**
- ✅ **频道斜杠命令**（群聊，定向投递）
- ✅ **频道内 @提及**（`team_chat.app_mention`），自动剥离 `@BotName` 前缀
- ✅ **Markdown 回复** —— Zoom 渲染一个子集：**粗体**、*斜体*、
  `行内代码` 以及链接
- ✅ **超长消息自动拆分**（约 4 KB 一段）
- ✅ **消息编辑 / 删除**（通过 chatbot API，用于流式纠错）
- ✅ **入站重试去重**（10 分钟窗口）
- ✅ **自我保护**（绝不回复自己的消息）
- ✅ **Cron / 通知投递**（通过 `ZOOM_HOME_CHANNEL`）

## 聊天类型推断

Zoom 不提供 `isChannel` 字段，因此插件依据 `to_jid` 后缀推断聊天类型：

| `to_jid` 格式 | 聊天类型 |
| --- | --- |
| `*@conference.xmpp.zoom.us` | 频道（广播给所有成员） |
| `<userJid>/<channelJid>` | 群聊斜杠命令（定向） |
| `*@xmpp.zoom.us` | 私信（1:1） |

## 说明与排错

- **私信必须带 `user_jid`。** 用户托管的 chatbot 应用如果不带该字段，Zoom 会
  返回错误码 `7001`。插件会从入站 payload 中学习并缓存 `user_jid`，所以对于
  机器人已经见过的会话，它总是存在的。对于没有先前入站上下文的 cron 推送，
  请把目标设为**频道**（`ZOOM_HOME_CHANNEL`）而不是私信。
- **Account ID 自动学习。** 没有 `ZOOM_ACCOUNT_ID` 这个配置项 —— 插件会从
  第一条入站消息中捕获 `account_id`，并持久化到
  `$HERMES_HOME/plugins/zoom-platform/state.json`。如果 cron 推送报
  “account_id not yet captured”，给机器人发一条私信即可写入。
- **Webhook 返回 401。** 每个合法的 Zoom 请求都必须携带有效的
  `x-zm-signature`。签名缺失或不匹配会被以 HTTP 401 拒绝 —— 请核对
  `ZOOM_SECRET_TOKEN` 是否与 **Features → Access** 中一致，并确认已在 Team
  Chat Subscription 上启用 *Signature*。

## 架构参考

| 文件 | 职责 |
| --- | --- |
| [`plugin.yaml`](plugin.yaml) | 插件清单、必填/选填环境变量。 |
| [`adapter.py`](adapter.py) | `ZoomAdapter` —— webhook 服务器、payload 归一化、入站路由、出站发送。 |
| [`_zoom_client.py`](_zoom_client.py) | `TokenManager`（chatbot 令牌缓存）+ `ZoomClient`（发送/编辑/删除）。 |
| [`_zoom_verify.py`](_zoom_verify.py) | HMAC 签名校验、`endpoint.url_validation` 握手。 |
| [`_state.py`](_state.py) | `account_id` 持久化（`$HERMES_HOME/plugins/<name>/state.json`）。 |

适配器继承自 `BasePlatformAdapter`，并通过 Hermes 标准插件 `register(ctx)`
入口里的 `ctx.register_platform(...)` 注册。接口约定详见 Hermes 的
[Adding Platform Adapters](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters)
指南。

## 许可证

MIT，详见 [LICENSE](LICENSE)。

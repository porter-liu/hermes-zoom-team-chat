# Zoom Team Chat 配置指南

> Zoom Team Chat 平台插件配置指南。
> [English](zoom-app-setup.md) · [简体中文](zoom-app-setup.zh-CN.md)

> 📌 **本指南以 macOS 为例进行说明。** Linux/Windows 上的步骤相同，只是服务
> 管理命令不同——那些平台请查阅对应的上游文档（Cloudflare、你的 init 系统）。

> **关于用词。** 本项目是一个 Hermes **平台插件**（platform plugin，即带有
> `plugin.yaml` 的分发/安装单元）。插件内部包含一个**适配器**（adapter，即
> `ZoomAdapter`），它是真正把 Hermes 接入 Zoom 的代码。本指南中，当我们指代「你
> 要安装并启用的那个东西」时，一律说「插件」；只有在谈论其内部实现时才用
> 「适配器」。而 Zoom Team Chat 本身，是这个插件为 Hermes 消息网关增加的一条
> **渠道**（channel）。

本指南**按你实际操作的顺序**组织：

1. **把你的 webhook 暴露到公网。** Zoom 会以 HTTP webhook（`POST /zoom/webhook`）
   的形式把聊天事件投递给插件，所以你首先需要一个公网 HTTPS 端点，指向插件监听
   的本地端口。我们用 **Cloudflare named tunnel** 来做这件事——无需在防火墙开放
   入站端口、自动提供 TLS，而且装成系统服务后重启会自动恢复。
2. **在 Zoom Marketplace 上注册一个 Chatbot 应用。** 这个应用会给插件提供
   **Client ID**、**Client Secret**、**Secret Token** 和 **Bot JID**——插件鉴权
   和回复所需的四项凭据。
3. **在 Hermes 里安装并启用插件**，把这些凭据填进去。

每一步都依赖前一步，请按顺序操作。

完整的配置变量与功能列表请参见 [README](../README.zh-CN.md)。

---

## 第 1 步 —— 用 Cloudflare named tunnel 暴露 webhook

Zoom 会向一个**公网 HTTPS URL** 发送 POST 来投递 chatbot 事件（你会在第 2 步把
这个 URL 注册到应用的 Team Chat Subscription 上）。插件本身只监听本地端口（默认
`8646`）。**Cloudflare named tunnel** 负责打通两者——一旦装成系统服务，重启后它
就能自己回来，你不必每次都记得手动启动。

你需要准备：

- 一个 Cloudflare 账号，且有一个域名由 Cloudflare DNS 托管。
- 已安装 `cloudflared`
  （[安装指南](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-remote-tunnel/#1-install-cloudflared)）；
  在 macOS 上最简单的方式是 `brew install cloudflared`。

### 1.1 登录 cloudflared

```bash
cloudflared tunnel login
```

会打开浏览器，选择你要使用的 zone（域名）。这会在 `~/.cloudflared/` 下生成
`cert.pem`——它只用于创建 tunnel 和 DNS 路由，不用于运行。

### 1.2 创建 tunnel

```bash
cloudflared tunnel create zoom-webhook
```

这会打印一个 tunnel UUID，并在 `~/.cloudflared/<UUID>.json` 写入凭据文件。请妥善
保管——运行中的 tunnel 靠它来鉴权。

### 1.3 把主机名路由到 tunnel

```bash
cloudflared tunnel route dns zoom-webhook zoom.example.com
```

把 `zoom.example.com` 换成你自己的主机名。它会自动创建指向
`<UUID>.cfargotunnel.com` 的 `CNAME` 记录。

### 1.4 配置 ingress

创建 `~/.cloudflared/config.yml`：

```yaml
tunnel: zoom-webhook
credentials-file: /Users/<you>/.cloudflared/<UUID>.json

ingress:
  - hostname: zoom.example.com
    service: http://localhost:8646
  - service: http_status:404
```

注意：

- `8646` 必须与你的 `ZOOM_WEBHOOK_PORT` 一致（默认就是 `8646`）。
- 末尾的 `http_status:404` 兜底规则是**必需的**。
- Zoom 会向 `https://zoom.example.com/zoom/webhook` 发送 POST。插件还暴露了
  `GET /`（一个小的落地页）和 `GET /health` 用于验证。

### 1.5 装成 launch agent（登录即启动）

这一步让 tunnel 在重启后自动恢复——开机时无需手动执行命令。

如果你是通过 Homebrew 安装的 `cloudflared`：

```bash
brew services start cloudflared
```

否则直接安装 launch agent（**不要**加 `sudo`——以你的用户身份运行，读取
`~/.cloudflared/config.yml`，登录时启动）：

```bash
cloudflared service install
```

> 若要在**开机**时（登录之前）就启动，改用 `sudo cloudflared service install`，
> 它会安装一个 launch _daemon_。注意 daemon 以 root 身份运行，因此必须确保 root
> 能读到你的配置——最简单的办法是把 `config.yml` 和凭据 JSON 也放一份到
> `/etc/cloudflared/`。对于需要登录才用的个人 Mac，上面的登录态 agent 形式通常就
> 够用了。

确认服务在运行：

```bash
brew services info cloudflared       # Homebrew 安装
launchctl list | grep cloudflared    # 直接安装的 launch agent
```

### 1.6 验证 tunnel

等几秒，然后通过公网 URL 访问插件的健康检查端点：

```bash
curl https://zoom.example.com/health
# → ok
```

（或者直接在浏览器里打开 `https://zoom.example.com/`，能看到落地页就说明 tunnel
和后端已经打通。）

返回 `ok` 说明 tunnel 已经把流量转发到插件了。如果插件还没起来，`cloudflared` 会
持续重试 `localhost:8646`，等插件一上线就自动接上——tunnel 和 Hermes 之间**没有
严格的启动顺序依赖**。

➡️ **至此你拿到了 webhook URL：** `https://zoom.example.com/zoom/webhook`。第 2 步
会把它填进 Zoom 应用。

---

## 第 2 步 —— 在 Zoom Marketplace 上注册 Chatbot 应用

### 2.1 在 Zoom Marketplace 上创建应用

访问 <https://marketplace.zoom.us/>，登录后确认左下角能看到 **Developer** 入口
（需要相应权限），点击它进入 <https://marketplace.zoom.us/user/build>。

进入后点击右上角的 **Develop** → **Build app**，在弹出的 *What kind of app are
you creating* 对话框里选择 **General App**。

Zoom 会给应用一个默认名字，这个名字决定了它在 Zoom Team Chat 里显示的名称，可以
点击屏幕上方应用名旁边的铅笔图标来修改。

### 2.2 获取 Client ID 和 Secret

接下来是应用的各项设置，如无特别说明均可使用默认值，无需特意修改。

在 **Basic Information** 页面可以获取到 **Client ID** 和 **Client Secret**，记下
来后面要用。**OAuth Redirect URL** 可以填你的自定义域名的根路径，例如
`https://zoom.example.com/`（即插件的落地页）。

![Basic Information](../assets/zoom-marketplace-basic-information.png)

### 2.3 获取 Secret Token

在 **Features → Access** 下可以获取到 **Secret Token**。

![Features/Access](../assets/zoom-marketplace-features_access.png)

### 2.4 获取 Bot JID

在 **Features → Surface** 里，*Select where to use your app* 下选择 **Chat**，
然后启用 **Chat Subscription**，在 **Bot Endpoint URL** 里填入
`https://zoom.example.com/zoom/webhook`，之后点 **Save**。

![Features/Surface](../assets/zoom-marketplace-features_surface.png)

下方 *Chat tabs* 里的 **Home tab** 和 **Channel tab** 复选框可以勾上。

### 2.5 获取 Authorization URL

最后一步 **Local Test** 里，点击 *Authorization URL* 下方的 **Generate** 生成一个
Authorization URL。完成第 3 步后，可以用这个 URL 把应用安装到你的 Zoom client。

![Local Test](../assets/zoom-marketplace-local_test.png)

---

## 第 3 步 —— 在 Hermes 里安装并启用插件

拿到四项凭据和 webhook URL 后，把插件接入 Hermes。完整细节见
[README](../README.zh-CN.md)，这里是要点：

1. **安装插件**，放到 platforms 目录下（`~/.hermes/plugins/platforms/zoom`），或
   通过 `hermes plugins install porter-liu/hermes-zoom-team-chat` 安装。

2. **设置四项必填凭据。** 它们从环境变量读取（或写在 `~/.hermes/.env`）：

   ```bash
   export ZOOM_CLIENT_ID="..."
   export ZOOM_CLIENT_SECRET="..."
   export ZOOM_BOT_JID="xxx@xmpp.zoom.us"
   export ZOOM_SECRET_TOKEN="..."
   ```

3. **启用插件和渠道**（用户安装的平台插件默认是 opt-in 的）：

   ```bash
   hermes plugins enable zoom-platform
   ```

   并在 `~/.hermes/config.yaml` 中：

   ```yaml
   gateway:
     platforms:
       zoom:
         enabled: true
   ```

4. **重启 Hermes。**

5. **把应用安装到 Zoom client。** 用浏览器打开第 2 步生成的 Authorization URL，
   用你的 Zoom 账号登录完成安装。

6. **完成 DM 配对。** 在 Zoom Team Chat 里给机器人发一条私信，Hermes 会回复一个
   配对码（pairing code）。在终端运行以下命令批准配对（把
   `THE_PAIRING_CODE_YOU_GOT` 换成你收到的配对码）：

   ```bash
   hermes pairing approve zoom THE_PAIRING_CODE_YOU_GOT
   ```

   配对完成后，你就可以在 Zoom Team Chat 里跟 Hermes 聊天了。

> 💡 这条私信同时也会缓存插件所需的 `account_id`（自动捕获——详见 README
> 中「Account ID 自动学习」一节）。

---

### 排错

> 凭据相关问题（401、`account_id` 未捕获）请见
> [README](../README.zh-CN.md#说明与排错)。

- **`curl https://…/health` 超时或返回 502 / 503** —— tunnel 在跑，但 `8646` 上
  没有进程在监听。检查 Hermes 是否在运行、Zoom 插件是否已连接。
- **健康检查能通，但 Zoom 的 webhook 始终不到** —— 重新检查 Team Chat
  Subscription 里的 Event notification URL，并确认 *Signature* 已启用、
  `ZOOM_SECRET_TOKEN` 与 **Features → Access** 中的 token 一致。
- **重启后 tunnel 没自动起来** —— 确认 launch agent 已加载：
  `brew services list` / `launchctl print gui/$(id -u)/com.cloudflare.cloudflared`。
  `brew services start cloudflared` 或 `cloudflared service install` 用来打开自启。

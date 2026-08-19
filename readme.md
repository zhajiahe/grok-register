# Grok 账号批量注册

用 DrissionPage 打开 x.ai 注册页，DuckMail 收验证码，写完资料后把 `sso` cookie 追加到文本。可选推送到 [grok2api](https://github.com/chenyme/grok2api)。

Turnstile 依赖真实 Chrome + 干净出口 IP。机房 / 公开代理经常卡在人机验证；住宅代理或本地电脑更稳。

## 环境

- Python 3.10+（建议 3.12）
- 系统 **Google Chrome**（Linux 优先 `/usr/bin/google-chrome`）
- [DuckMail](https://duckmail.sbs) Bearer Token
- Linux 无桌面时需要 `xvfb` + `PyVirtualDisplay`

```bash
pip install -r requirements.txt
# Linux 无头额外：
# apt install -y xvfb && pip install PyVirtualDisplay
cp config.example.json config.json
```

`DrissionPage` 需 `>=4.1.1.2,<4.2`。不要用 snap 版 Chromium。

## 配置

只改 `config.json`（已 gitignore）。字段见 `config.example.json`。

| 字段 | 说明 |
| --- | --- |
| `run.count` | 轮数，`0` 为无限；可被 `--count` 覆盖 |
| `duckmail_bearer` | DuckMail Token |
| `duckmail_domains` | 指定邮箱域名；空则从 API 拉已验证域名 |
| `duckmail_exclude_domains` | 默认排除 `duckmail.sbs`（x.ai 会拒） |
| `proxy` | 仅 DuckMail API 走代理 |
| `browser_proxy` | 单条浏览器代理 |
| `browser_proxies` | 浏览器代理池，每轮轮询；失败（连不上或 Turnstile 不过）自动跳过 |
| `api.base` | 新版 grok2api 根地址，如 `http://127.0.0.1:8000` |
| `api.admin_username` / `api.admin_password` | 新版管理端账号，填了则走 `/accounts/web/import` |
| `api.endpoint` / `api.token` | 旧版 Python grok2api 的 `ssoBasic` 接口；新版填了 `base` 时忽略 |

代理写成 `http://host:port`。SOCKS 对当前 Chrome 启动不稳定，优先 HTTP。`proxy` 和 `browser_proxies` 分开：邮箱 API 不要走不可信的浏览器出口。非本机 `api.base` 必须 HTTPS。

DuckMail Token：登录 [duckmail.sbs](https://duckmail.sbs) → F12 Network → 复制发往 `api.duckmail.sbs` 的 `Authorization: Bearer ...`。

## 运行

```bash
python3 DrissionPage_example.py
python3 DrissionPage_example.py --count 50
python3 DrissionPage_example.py --count 0
python3 DrissionPage_example.py --push-sso sso/sso_batch.txt
```

无 `DISPLAY` 时自动起 Xvfb。强制 / 关闭：

```bash
USE_XVFB=1 python3 DrissionPage_example.py
DISABLE_XVFB=1 python3 DrissionPage_example.py
```

## grok2api

本脚本导入的是 **Grok Web SSO**，不是 Grok Build，也不是 xAI 官方 API Key。

新版（Go）填 `api.base` + 管理员账号，每轮注册成功后调用 `/api/admin/v1/accounts/web/import`。也可只导入已有文件：

```bash
python3 DrissionPage_example.py --push-sso sso/sso_batch.txt
```

旧版 Python grok2api 仍用 `api.endpoint` + `api.token`（`ssoBasic`）。两种都配时优先新版。

### 模型和 grok-4.6

| 调用名 | 实际来源 |
| --- | --- |
| `grok-chat-fast` | grok.com **Fast** 档。网页会换底层，不是官方 API 的 `grok-4.6` |
| `grok-chat-auto` / `expert` / `heavy` | 网页 Auto / Expert / Heavy，分别要 Super / Heavy 订阅 |
| `grok-4.6` | Grok **Build** 或 Console。当前旗舰，需 Build OAuth 账号 |

刚导入 Web 号时，`GET /v1/models` 通常只有 `grok-chat-fast` 和 Imagine。要 `grok-4.6`：

1. 管理端把 Web 转成 Build：`POST /api/admin/v1/accounts/web/convert-to-build`，body `{"all": true, "strategy": "missing"}`（也可用管理界面）
2. 客户端密钥的 `providerScope` 包含 `grok_build`（只开 `grok_web` 时列表里看不到 4.6）
3. 给 `grok_build` 配出口节点；可与 Web 共用同一条 HTTP 代理，fallback 指过去

然后 `model` 用 `grok-4.6`。推理档：`grok-4.6-low` / `-medium` / `-high` / `-xhigh`。

## 产出

- `sso/sso_<时间>.txt`：一行一个 sso（可用 `--output` 指定）
- `logs/run_<时间>.log`：每轮邮箱、密码、代理、成败

## 常见问题

- **邮箱域名被拒**：已默认跳过 `duckmail.sbs`，被拒会换域重试。也可在 `duckmail_domains` 里写自己的已验证域名。
- **Turnstile token 一直为 0**：出口 IP 被 Cloudflare 打分过低。换住宅代理，或在本地有桌面的机器上跑。
- **Chrome 起不来**：确认是系统 Chrome，且不要手动给 DrissionPage 设 `user-data-path`（4.1.1 会清掉 `auto_port`）。
- **chat 没有 grok-4.6**：先确认 SSO 已进 grok2api Web，再转 Build，并让客户端密钥包含 `grok_build`。

## 致谢

- [kevinr229/grok-maintainer](https://github.com/kevinr229/grok-maintainer)
- [AaronL725/grok-register](https://github.com/AaronL725/grok-register)
- [grok2api](https://github.com/chenyme/grok2api)
- [DuckMail](https://duckmail.sbs)

# grok-register

用系统 Chrome 打开 x.ai 注册页，DuckMail 收验证码，把 `sso` cookie 一行一个写到文本。可选推到 [grok2api](https://github.com/chenyme/grok2api)。

Turnstile 要真实 Chrome + 干净出口。公开 / 机房代理经常卡在人机验证；住宅 HTTP 代理或本地有桌面的机器更稳。4 核机器并行 **2–3** 个 Chrome 即可，再加更容易互相拖死。

## 目录

| 路径 | 作用 |
| --- | --- |
| `register.py` | 注册入口 |
| `email_register.py` | DuckMail 创建邮箱、拉验证码 |
| `turnstilePatch/` | Chrome 扩展，辅助 Turnstile |
| `config.example.json` | 配置模板，复制为 `config.json` |
| `sso/` | 产出的 sso 文本（已 gitignore） |
| `logs/` | 运行日志（已 gitignore） |

## 环境

- Python 3.10+（建议 3.12）
- 系统 **Google Chrome**（Linux 用 `/usr/bin/google-chrome`，不要 snap Chromium）
- [DuckMail](https://duckmail.sbs) Bearer Token
- Linux 无桌面：`xvfb` + `PyVirtualDisplay`

```bash
cp config.example.json config.json
# 任选其一
pip install -r requirements.txt
# uv sync
```

无头 Linux 额外：`apt install -y xvfb && pip install PyVirtualDisplay`

`DrissionPage` 需 `>=4.1.1.2,<4.2`。不要手动设 `user-data-path`（4.1.1 会清掉 `auto_port`）。

## 配置

只改 `config.json`（已 gitignore）。字段以 `config.example.json` 为准。

| 字段 | 说明 |
| --- | --- |
| `run.count` | 轮数，`0` 为无限；可被 `--count` 覆盖 |
| `run.workers` | 并行 Chrome 数，默认 `1`；可被 `--workers` 覆盖 |
| `duckmail_bearer` | DuckMail Token |
| `duckmail_domains` | 指定邮箱域名；空则从 API 拉已验证域名 |
| `duckmail_exclude_domains` | 默认排除 `duckmail.sbs`（x.ai 会拒） |
| `proxy` | 仅 DuckMail API 走代理 |
| `browser_proxy` / `browser_proxies` | 浏览器出口；池内预检，失败两次才跳过，按成功次数打分 |
| `api.base` + 管理员账号 | 新版 grok2api，成功后走 `/accounts/web/import` |
| `api.endpoint` / `api.token` | 旧版 Python grok2api 的 `ssoBasic`；配了 `api.base` 时忽略 |

代理写成 `http://host:port`。优先 HTTP，SOCKS 启动 Chrome 不稳定。`proxy` 和 `browser_proxies` 不要混用。非本机 `api.base` 必须 HTTPS。

DuckMail Token：登录 [duckmail.sbs](https://duckmail.sbs) → F12 Network → 复制发往 `api.duckmail.sbs` 的 `Authorization: Bearer ...`。

## 运行

```bash
python3 register.py
python3 register.py --count 50
python3 register.py --count 0
python3 register.py --count 3000 --workers 3 --output sso/sso_batch.txt
python3 register.py --push-sso sso/sso_batch.txt
```

成功一轮后清 cookie，复用同一 Chrome 和代理；Turnstile token 连续约 8 秒仍为 0 则换出口。`--workers` 按进程切分代理池，互不抢同一条 IP。

无 `DISPLAY` 时自动起 Xvfb：

```bash
USE_XVFB=1 python3 register.py
DISABLE_XVFB=1 python3 register.py
```

## grok2api

导入的是 **Grok Web SSO**，不是 Build，也不是官方 API Key。

新版（Go）填 `api.base` + 管理员账号，每轮成功调用 `/api/admin/v1/accounts/web/import`。只导入已有文件用 `--push-sso`。旧版用 `api.endpoint` + `api.token`。两种都配时走新版。

| 调用名 | 实际来源 |
| --- | --- |
| `grok-chat-fast` | grok.com Fast 档，不是官方 `grok-4.6` |
| `grok-chat-auto` / `expert` / `heavy` | 网页对应档，分别要 Super / Heavy |
| `grok-4.6` | Grok Build 或 Console，需 Build OAuth |

要 `grok-4.6`：把 Web 转成 Build（`POST /api/admin/v1/accounts/web/convert-to-build`，`{"all": true, "strategy": "missing"}`），客户端密钥 `providerScope` 含 `grok_build`，并给 Build 配出口。推理档：`grok-4.6-low` / `-medium` / `-high` / `-xhigh`。

## 产出

- `sso/`：一行一个 sso（`--output` 可改路径）
- `logs/`：每轮邮箱、密码、代理、成败

## 常见问题

- **邮箱域名被拒**：已跳过 `duckmail.sbs`，被拒会换域。也可在 `duckmail_domains` 里写已验证域名。
- **Turnstile token 一直为 0**：出口分太低。换住宅代理，或在有桌面的机器上跑。
- **Chrome 起不来**：用系统 Chrome，不要给 DrissionPage 设 `user-data-path`。
- **chat 没有 grok-4.6**：SSO 先入 Web，再转 Build，密钥包含 `grok_build`。

## 致谢

- [kevinr229/grok-maintainer](https://github.com/kevinr229/grok-maintainer)
- [AaronL725/grok-register](https://github.com/AaronL725/grok-register)
- [grok2api](https://github.com/chenyme/grok2api)
- [DuckMail](https://duckmail.sbs)

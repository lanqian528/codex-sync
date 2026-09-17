# codex-sync

个人使用的 Codex 多设备 provider 同步工具。云端只有受 Basic Auth 保护的 JSON；客户端仅 `sync` / `watch`。无数据库、设备注册、状态上报、账号池、额度检测或请求转发。

**首次接入前确保本机 Pro 可用。退出 Codex → 同步 → 重新打开并用新会话验证。运行中的任务不会实时切换。**

## 一行安装（发布 Release 后可用）

Windows x64，普通 PowerShell：

```powershell
irm https://raw.githubusercontent.com/lanqian528/codex-sync/main/scripts/install.ps1 | iex
```

Linux x64 / arm64（需要 systemd、curl、unzip；首次交互配置还需要 python3）：

```bash
bash <(curl --proto '=https' -fsSL https://raw.githubusercontent.com/lanqian528/codex-sync/main/scripts/install.sh)
```

安装器下载 GitHub Release、校验 SHA-256、询问云端读取信息，然后注册后台自启动。不需要给客户端填 API Key。它只对 Codex 目录和 config.toml 收紧权限，不递归操作，不读取登录文件。口令输入隐藏；不要把口令放进命令行或 URL。校验值与发布包来自同一仓库，用于下载完整性检查，并非独立签名。

Windows 注册当前用户登录时运行的 `CodexSync` 计划任务，无命令窗口；不在登录前使用系统账号运行。Linux 使用用户级 systemd，登录后自启；需要服务器重启后、未登录也运行时，再执行 `sudo loginctl enable-linger "$USER"`。后台每 60 秒检查一次，网络错误下轮重试，崩溃由系统服务重启；不承诺进程检查与写入之间绝对不存在启动竞态，请在切换期间保持 Codex 关闭。

安装器可重复用于更新。自定义仓库/版本：Linux 设置 `CODEX_SYNC_REPO=owner/repo` 和 `CODEX_SYNC_VERSION=v0.1.0`；Windows 下载脚本后传 `-Repo owner/repo -Version v0.1.0`。不自动下载软件更新。

## 云端：Vercel（默认）或 Cloudflare

两个平台使用相同的 JSON 和 Basic Auth，客户端只需更换 URL。域名是否在所在网络可达需要实测，不保证任一平台的默认域名在所有地区可用。

### Vercel

将此 GitHub 仓库导入 Vercel，Framework Preset 选 **Other**、Root Directory 保持仓库根目录。无需数据库或前端。也可在根目录运行：

```bash
npx vercel login
npx vercel link
npx vercel env add READ_USERNAME production
npx vercel env add READ_PASSWORD production
npx vercel env add CLOUD_CONFIG production
npx vercel --prod
```

在交互输入中设置独立读取用户名、口令，以及完整 JSON；不要把值写进命令参数。环境变量也可以在 Vercel 项目 Settings → Environment Variables 设置，生产环境中的口令和 JSON 标记为 Sensitive。不要使用 `NEXT_PUBLIC_` 前缀。

生产 URL：`https://<项目生产域名>/config.json`。首次未配置时返回 503，不泄漏数据。未授权时返回 401。成功、认证失败、配置错误均不缓存，显式禁止 Vercel CDN 缓存。使用稳定的生产域名；Preview 的平台登录保护可能阻止客户端读取，不要把需要 Vercel 登录的预览链接填进客户端。

每次更新 `CLOUD_CONFIG`（包括只切 mode）后必须 **重新部署到 Production** 才会生效。旧部署快照保留旧环境变量；轮换凭据时请按平台方式移除旧部署，避免旧部署 URL 长期使用旧口令。程序不读取 Vercel 账号 token，Vercel 管理权限只用于部署。

### Cloudflare Worker（可选）

云端使用 Worker secrets 保存一个完整 JSON，读取口令另外保存，不需要 KV。Cloudflare 账号管理员可以更新秘密；公开仓库、客户端源码中不保存真实值。这里替代了原方案的 SSH 文件编辑。

```bash
cd worker
npm install
npx wrangler login
npx wrangler deploy
npx wrangler secret put READ_USERNAME
npx wrangler secret put READ_PASSWORD
npx wrangler secret put CLOUD_CONFIG
```

最后一个命令的交互输入粘贴完整单行 JSON，例如（请自行替换 Key）：

```json
{"mode":"api","base_url":"https://api.example.com/v1","api_key":"REPLACE_WITH_REAL_KEY"}
```

也可以在 Cloudflare Dashboard → Worker → Settings → Variables and Secrets 中设置上述三个 Secret。读取地址为 `https://codex-sync-config.<你的子域>.workers.dev/config.json`。只开放 GET，无写接口，不重定向；成功和错误均 `Cache-Control: no-store`。Workers 日志采集关闭，代码不记录请求、口令或 JSON。不要开启记录请求头/响应正文的外部日志规则。

更新模式、URL、Key 时重新设置 **完整** `CLOUD_CONFIG` Secret（一次替换），或在控制台修改并部署。切回 Pro 只把 mode 改成 pro，保留 API 设置。Secret 更新依赖 Cloudflare 部署传播时间；无额外版本系统。读口令泄露后更新 `READ_PASSWORD` 并替换全部客户端连接配置。不要把秘密放进 wrangler.jsonc、GitHub Actions 或 Git。

## 手动运行

源码需要 Python 3.10+：

```bash
python -m pip install -r requirements.txt
python codex_sync.py sync
python codex_sync.py watch
```

发布包直接运行 `codex-sync sync` / `codex-sync watch`（Windows 文件名带 `.exe`）。发布矩阵：Windows x64、Linux x64（glibc 2.35+）、Linux arm64（glibc 2.39+）。Windows ARM 和 Alpine/musl 不在二进制支持范围；macOS 可从源码运行，未提供打包版本。

本地配置默认 `~/.config/codex-sync/connection.json`，可用 `CODEX_SYNC_CONFIG` 指定不同文件。复制 `examples/connection.example.json` 后填写 URL、用户名、独立读取口令。`codex_home` 可省略，优先读取 `CODEX_HOME`，否则 `~/.codex`。App/CLI 使用不同目录时，分别设置连接文件、分别运行；不要让两份连接文件同时管理同一目录。

手工设置时，连接目录和 Codex 目录使用权限 700；连接文件和现有 config.toml 使用 600。Windows ACL 只允许当前用户和 SYSTEM。权限不满足时程序拒绝写入，不主动调整已有配置权限。安装脚本是一次性权限设置的入口。

## 修改范围与安全边界

- 首次接入只接受缺省 provider 或 `openai`，且 `synced_api` 必须未占用。原 provider 记录仅保存一次，位于 Codex 目录的 `.codex-sync-state.json`，只含是否存在和原值。
- API 模式只修改根表 `model_provider` 和 `model_providers.synced_api`；Pro 模式恢复原根键并删除专用表。保留模型、MCP、其他表及注释，不恢复整份旧配置。不要在工具管理期间手工编辑专用表；冲突会停下。
- 按项目需求使用 `requires_openai_auth = true`，并设置 `experimental_bearer_token`。官方定义 true 表示 OpenAI 认证，并非通用 Bearer 开关；已测 Windows CLI 0.155.0-alpha.2.6 在这两个字段同时存在时优先使用直接 Bearer，未携带假 Pro token。其他版本必须重新运行请求测试，不能仅凭 TOML 可解析就认定安全。 Key 明文保存在 config.toml，官方更推荐 env_key；这里按桌面 App 免环境变量配置的需求选择直接 Bearer。
- 不读取、复制、上传或写入真实 auth.json，不访问系统凭证库，不执行 login/logout。不会阻止 Codex 自身刷新凭证，也不能保证失效后永远免登录。
- 每次重新获取并完整校验 JSON；只接受 HTTPS，无 URL 凭证、查询串或重定向；15 秒网络超时、64 KiB 响应上限。不继承代理环境变量。只写变化内容。
- 配置写入使用保留注释的 tomlkit、同目录私有临时文件、内容校验、互斥锁、外部修改检查和原子替换。写入失败保留原文件。记录先落盘，进程在两次替换之间退出时，下一次可继续，绝不还原整份旧配置。
- `sync` 和 `watch` 均检查当前用户 Codex App/CLI/相关后台。发现运行或无法确认则暂缓。系统进程短暂消失可忽略；其他未知错误不会绕过检查。无法识别被任意重命名的进程。
- profile provider 覆盖和目录内策略文件会阻止同步。项目配置、命令行 `-c`、系统/企业策略仍可能覆盖用户配置；工具无法完整判断这些生效层，不强制改写它们。请在新会话确认实际 provider。
- 日志仅输出固定状态/错误，不输出异常原文、URL、Key 或口令。没有自动状态上报。文件符号链接、重解析点和硬链接不支持。

## 自启动管理

Linux：

```bash
systemctl --user status codex-sync
journalctl --user -u codex-sync -n 30
systemctl --user disable --now codex-sync
```

Windows：

```powershell
Get-ScheduledTaskInfo -TaskName CodexSync
Stop-ScheduledTask -TaskName CodexSync
Disable-ScheduledTask -TaskName CodexSync
& "$env:LOCALAPPDATA/codex-sync/codex-sync.exe" sync
```

Windows 使用系统 WScript 隐藏窗口启动；禁用 WScript 的环境不支持此安装方式。更新/停用时可检查自己的 `codex-sync.exe` 进程是否已退出；程序不会关闭 Codex App/CLI。

macOS 手动自启动示例：在 `~/Library/LaunchAgents/local.codex-sync.plist` 写入以下内容并替换绝对路径，使用安装了依赖的 Python。然后 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.codex-sync.plist`；卸载用 `launchctl bootout gui/$(id -u)/local.codex-sync`。本平台未实际运行验证。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.codex-sync</string>
<key>ProgramArguments</key><array><string>/absolute/venv/bin/python</string><string>/absolute/codex_sync.py</string><string>watch</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>60</integer>
</dict></plist>
```

## 验证与发布

```bash
python -m unittest discover -s tests -v
cd worker && node --test
```

可选真实 CLI 检查：`python tests/probe_cli.py /absolute/path/to/codex`。这个测试只创建临时目录和假登录数据，启动本机 HTTP 捕获服务，验证 API Bearer、Pro token 不外发和假 auth 文件不变。生产同步程序仍严格要求 HTTPS；测试不会访问第三方 API。测试会暂时启动 Codex，期间 watcher 暂缓是预期行为。

已实际验证：Windows Python 3.11、14 项基本测试、5 项云端测试（含 Vercel 适配）；Windows `codex-cli 0.155.0-alpha.2.6` 在 requires_openai_auth=true 下真实发出的 API 请求使用假 API Key、未携带假 Pro token、未更改假登录数据。GitHub Actions 的 Windows x64、Linux x64/ARM64 已通过测试、打包及成品断网保护测试；从 Release 下载的 Windows 成品另行通过 SHA-256 和断网不改配置验证。

尚未验证：桌面 App 新会话端到端切换、真实第三方 API/模型响应、Windows 登录后任务启动、Linux 实机开机与桌面环境、macOS。GitHub Actions 测试和打包结果以对应运行记录为准，不能将编译通过视为上述兼容性验证通过。

推送 main / PR 自动测试和打包；推送 `v*` tag 会在全部平台通过后创建 GitHub Release 并附带 ZIP 和 SHA-256。Actions 不需要任何业务秘密，使用内置 GITHUB_TOKEN 上传发布资产。不要上传 connection.json、cloud.json、auth.json、真实 config.toml 或任何个人配置。

官方依据：[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)、[认证](https://learn.chatgpt.com/docs/auth)、[Cloudflare Worker Secrets](https://developers.cloudflare.com/workers/configuration/secrets/)。

Vercel 依据：[Node.js Functions](https://vercel.com/docs/functions/runtimes/node-js)、[环境变量](https://vercel.com/docs/environment-variables)。

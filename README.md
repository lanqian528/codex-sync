# Codex Sync

个人使用的 Codex 多设备 provider 同步工具。一个带密钥登录的管理页、一份私有 JSON、一个本地 Python 脚本。支持 **Vercel + Private Blob** 和 **Cloudflare Workers + R2**。

没有设备注册、状态上报、账号池、额度检测或请求转发。安装后直接输入 **`codex-sync`**，即可查看状态并选择启动、停止或修改配置。管理页可切换 Pro / API、更新 API 地址和 Key；保存后生效，无需重新部署。

## 认证与存储

云端环境变量 **`ACCESS_KEY`** 是唯一的访问密钥，使用随机的 32–256 个非空白 ASCII 字符。**同一把密钥既能管理，也能读取配置**；它与第三方 `api_key` 是两把不同的密钥。所有客户端持有者都具有管理权限。

- 管理登录提交到 `/api/session`，成功后获得 1 小时的 HttpOnly、Secure、SameSite=Strict 签名 Cookie；密钥不保存到 localStorage/sessionStorage。
- 客户端以 `Authorization: Bearer <ACCESS_KEY>` 请求 `/config.json`，URL 不含密钥。
- `/api/admin` 读写接口同样要求认证；Cookie 写请求还检查同源和 CSRF。读取管理配置只返回 Key 是否存在，不回显已保存的 API Key。
- 未认证返回 401；未设置有效 ACCESS_KEY 返回 503。所有配置和错误响应禁止缓存。登录页静态内容可以公开访问，但里面没有密钥或业务配置。
- 私有存储只保存一个 `config.json`，内容恰好是 `mode`、`base_url`、`api_key`。不是公开 Blob，没有公开下载权限。Vercel 的设备轮询使用 Blob 私有缓存，管理页和保存前检查仍以 `useCache: false` 读取源站；Cloudflare 使用私有 R2。对外配置接口始终认证且 `no-store`。
- 保存先校验，再条件替换；冲突返回 409，要求重新读取，防止旧页面覆盖新配置。网络中断可能导致保存结果不确定，页面会要求重新读取，不虚报成功。

示例 JSON（真实值不要提交 Git）：

```json
{"mode":"pro","base_url":"https://api.example.com/v1","api_key":""}
```

API 模式需要非空真实 Key。切回 Pro 仍保留 API 设置。可选环境变量 `CLOUD_CONFIG` 仅作为存储文件尚不存在时的初始配置；一旦页面保存成功，以私有文件为准。

## 部署 Vercel

1. 导入此 GitHub 仓库，Framework Preset 选 **Other**，根目录保持仓库根目录。
2. 为 Production 设置 **Sensitive 环境变量 `ACCESS_KEY`**，不要使用 `NEXT_PUBLIC_` 前缀。
3. 项目 Storage 中创建 **Private** Blob，并连接到 Production。平台自动配置存储凭据，不要复制到前端或 Git。CLI 也可执行 `npx vercel@latest blob create-store codex-sync-private --access private --yes --environment production`。
4. 部署到 Production，打开 `https://<生产域名>/` 登录。客户端 URL 为 `https://<生产域名>/config.json`。

源码方式：

```bash
npm ci
npx vercel@latest login
npx vercel@latest link
npx vercel@latest env add ACCESS_KEY production
npx vercel@latest blob create-store codex-sync-private --access private --yes --environment production
npx vercel@latest --prod
```

已有私有存储时连接它即可，不要重复创建。只把存储连接到需要的环境，不要让测试部署用弱测试密钥连接真实配置存储。私有存储不是内存或 `/tmp` 文件，重新部署、函数重启后配置仍保留。

设置或轮换 ACCESS_KEY 后需要重新部署；页面修改业务配置不需要。Vercel 旧部署可能保留旧环境变量且仍能访问共享存储，**轮换密钥后应删除旧部署或通过平台限制其访问，并替换所有客户端密钥**。使用稳定生产域名，不要给客户端填写受 Vercel 登录保护的 Preview 地址。域名可达性需在目标设备网络实测。

### Vercel 免费额度优化

每台设备每分钟直接回源一次，按 30 天计约 43,200 次 Blob 读取，超过 Hobby 的 10,000 次 Simple Operations。因此设备轮询默认走 Blob 的**私有**缓存；写入时设置一小时缓存，函数固定在 iad1，避免不同设备分别打到多个源站缓存区域。

缓存命中不计 Simple Operations。按一个缓存节点持续命中估算，定时刷新约为每月 720 次，再加管理页直读、修改后的缓存刷新和缓存提前失效；这不是用量硬上限。函数调用和 Edge Requests 仍按设备轮询次数累计，免费额度也与同账号其他项目共享。

覆盖配置后 Blob 会刷新其缓存，传播可能约 60 秒，叠加客户端轮询后通常约 1–2 分钟跟随；管理页直接读取最新值。不会把带密钥的 HTTP 响应开放为公共 CDN 缓存。已有旧部署的用户需在更新后原样保存一次配置，使既有 Blob 的缓存时长从旧值升级到一小时。

依据：[Blob 计费](https://vercel.com/docs/vercel-blob/usage-and-pricing)、[私有 Blob 的缓存一致性](https://vercel.com/changelog/vercel-blob-now-supports-consistent-reads-on-private-storage)。

## 部署 Cloudflare

```bash
cd worker
npm ci
npx wrangler login
npx wrangler r2 bucket create codex-sync-config
npx wrangler secret put ACCESS_KEY
npx wrangler deploy
```

`wrangler.jsonc` 的 `CONFIG_BUCKET` 绑定私有 R2 bucket，`ASSETS` 提供同一套管理页。不启用 R2 公开访问或自定义公开下载域名。若 bucket 已存在，跳过创建；可修改示例 bucket 名后部署。管理页路径 `/`，客户端路径 `/config.json`。Cloudflare 运行时日志采集关闭，代码不记录请求密钥、JSON 或口令。

## 一行安装客户端

**首次接入前确认本机 Pro 正常。同步会直接更新配置文件，无需先退出 Codex；之后重新打开 Codex 并用新会话验证。**程序不会主动关闭或重启 Codex，也不强制切换当前会话。

Windows x64，普通 PowerShell：

```powershell
irm https://raw.githubusercontent.com/lanqian528/codex-sync/main/scripts/install.ps1 | iex
```

Linux x64 / ARM64：

```bash
bash <(curl --proto '=https' -fsSL https://raw.githubusercontent.com/lanqian528/codex-sync/main/scripts/install.sh)
```

首次询问 Codex 目录、云端网址和 ACCESS_KEY，认证读取验证通过后才保存连接并启用后台自启动。可直接填写域名或管理首页，不必手工补 `/config.json`。不需要在客户端另外填写第三方 API Key。Linux 需要 systemd、curl、unzip，成品安装不需要 Python。SHA-256 用于下载完整性检查，不是独立发布签名。

安装脚本会收紧连接目录、Codex 目录和 config.toml 的权限；不递归处理登录文件。Windows 登录后使用隐藏窗口计划任务运行，Linux 使用用户级 systemd。Linux 服务器希望重启后、未登录也运行时：`sudo loginctl enable-linger "$USER"`。

Windows 安装器将程序目录加入用户 PATH，并更新执行安装的 PowerShell 窗口。Linux 创建 `~/.local/bin/codex-sync` 命令，并为 Bash/Zsh 配置 PATH。其他已经打开的终端可能需要关闭后重新打开。特殊 shell 或符号链接管理的 shell 配置文件需要自行把 `~/.local/bin` 加入 PATH。

可重复执行安装脚本更新，**已有连接文件不会被覆盖**。Linux 可设置 `CODEX_SYNC_REPO=owner/repo`、`CODEX_SYNC_VERSION=v0.3.0`；Windows 下载脚本后传 `-Repo owner/repo -Version v0.3.0`。不会自动下载程序更新。

## 终端菜单与状态

安装完成后，在终端运行：

```bash
codex-sync
```

先显示后台运行状态、自启动状态、连接地址、本地用户配置中的 provider、最近检查时间及结果，再显示菜单：

```text
1. 启动监控（开启自启动）
2. 停止监控（关闭自启动）
3. 修改连接配置
4. 同步一次
5. 刷新状态
0. 退出菜单（后台继续运行）
```

“启动”启用系统自启动并启动后台监控；“停止”停用自启动并停止监控，不关闭 Codex，也不切换 provider。退出菜单或按 Ctrl+C 只退出当前菜单。监控会直接更新配置文件，不再等待 Codex 退出；运行中会话是否重新加载由 Codex 自身决定。

状态页仅只读查看本地信息，不主动写 Codex 配置、不显示密钥。云端模式与同步结果来自后台上次检查，在连接目录的私有 `connection.status.json` 中记录，不上传。时间超过 150 秒会标为历史记录。进程身份记录位于 `connection.watch.json`，核对 PID、启动时间、用户、程序路径和 watch 参数后才允许结束进程，避免误杀 Codex。一个连接文件只允许一个 watch 实例。

也支持直接命令：

```bash
codex-sync status   # 只查看状态
codex-sync start    # 启动监控并开启自启动
codex-sync stop     # 停止监控并关闭自启动
codex-sync config  # 修改连接信息
codex-sync sync    # 同步一次
codex-sync watch   # 当前终端前台监控，Ctrl+C 结束
```

后台启停管理支持 Windows / Linux 的默认安装服务。使用自定义 CODEX_SYNC_CONFIG 或 macOS 时，请自行管理对应的 watch 进程或系统服务，程序不会误操作默认服务。

## 本地连接配置

连接文件默认 `~/.config/codex-sync/connection.json`，可通过 `CODEX_SYNC_CONFIG` 指定其他文件：

```json
{
  "url": "https://YOUR_PROJECT.vercel.app/config.json",
  "access_key": "REPLACE_WITH_YOUR_32_CHARACTER_ACCESS_KEY",
  "codex_home": "~/.codex"
}
```

`codex_home` 可省略：优先 `CODEX_HOME`，否则 `~/.codex`。App / CLI 路径不同时分别配置，不自动发现。不要让不同云端同时管理一个目录。

```bash
python -m pip install -r requirements.txt
python codex_sync.py sync
python codex_sync.py watch
python codex_sync.py config
```

发布成品的 Windows 文件名带 `.exe`，加入 PATH 后可省略扩展名。watch 每轮结束后等待 60 秒；网络超时为 15 秒。

### 自动识别网址

域名 `example.vercel.app`、首页 `https://example.vercel.app/`、`https://example.vercel.app/index.html` 都会识别为 `https://example.vercel.app/config.json`。Vercel、Cloudflare 和自定义域名适用同一规则。

完整的 `/config.json`、`/api/config` 或自定义配置文件路径保持不变。不会自动扫描其他路径或跟随重定向；显式 HTTP 地址、含用户名/口令/查询参数的 URL 会拒绝。已有连接文件即使填了首页，升级后的 sync/watch 也能识别，无需先改文件。

### 修改客户端连接信息

Windows：

```powershell
codex-sync config
```

Linux：

```bash
codex-sync config
```

交互修改网址、ACCESS_KEY、Codex 目录，直接按 Enter 保留原值。密钥输入隐藏，绝不显示旧密钥，也不提供把密钥写进命令参数的选项。

只修改一项也可以（以下省略程序的完整路径）：

```bash
codex-sync config --url https://YOUR_PROJECT.vercel.app
codex-sync config --codex-home /absolute/path/to/.codex
codex-sync config --set-key
```

先验证目录、权限及云端认证读取，再原子保存。失败时保留原连接；修改网址跨到另一个域名时，需要重新输入该域名的访问密钥，不自动发送原密钥。正在运行的 watch 会在下一轮读取新连接，无需重启监控。

`config` 不修改 Codex 的 config.toml，也不要求退出 Codex；它管理本机连接信息。云端 mode、API 地址和第三方 API Key 在管理页修改。sync/watch 会直接写入 provider 配置文件，不以 Codex 是否运行作为写入条件。首次从源码使用时，先按下方说明设置 Codex 目录及 config.toml 权限。

**从 v0.2.x 升级**：将连接文件的 `username` / `password` 替换为 `access_key`，不要同时配置两种认证。旧版客户端也可以手工设置 `username: "sync"`、`password: "与 ACCESS_KEY 相同的值"` 过渡使用；原 READ_USERNAME / READ_PASSWORD 已停用，不能绕过 ACCESS_KEY。此前保存在 CLOUD_CONFIG 中的配置可作为初始值，管理页面第一次保存后进入私有存储。

Linux / macOS：连接目录和 Codex 目录权限 700；连接文件和 config.toml 权限 600。Windows ACL 仅当前用户与 SYSTEM。同步程序发现权限过宽时拒绝写入，不主动改变既有文件权限。

## 本地保护范围

- 首次只接受根 provider 缺省或 `openai`，`synced_api` 表必须未占用。只记录一次原值和是否存在到 `.codex-sync-state.json`，不含登录凭证。
- API 模式只修改根 `model_provider` 和 `model_providers.synced_api`；Pro 模式恢复原根键并删除专用表。不还原整份旧文件，保留模型、MCP、其他表和注释。
- 按项目要求使用 **`requires_openai_auth = true`** 和 `experimental_bearer_token`。官方定义 true 表示 OpenAI 认证，不是通用 Bearer 开关。已测 CLI 在这两个字段同时存在时采用直接 Bearer；其他版本仍需实测。API Key 明文落在私有 config.toml，官方更推荐 env_key。
- 不读取、上传、复制或改写真实 auth.json，不访问系统凭证库，不执行 login/logout。不阻止 Codex 自身刷新，也不保证失效凭证永远免登录。
- HTTPS、不跟随云端重定向、不继承代理环境、限制响应大小。出错不打印异常原文、密钥或配置；只输出固定状态。没有状态上报。
- 同目录私有临时文件、内容校验、原子替换、本地互斥锁和外部修改检查；配置未变化不写入。符号链接、重解析点和硬链接不支持。
- sync/watch 不检查 Codex 是否运行；拉取与校验成功后直接更新配置文件，不主动关闭或重启 Codex。仍检测写入前的外部文件修改并在冲突时停止本轮。psutil 仅用于管理同步器自身的后台进程。
- profile provider 覆盖、目录内策略文件会阻止同步。项目配置、启动参数和系统管理策略仍可能覆盖它；工具不强行更改这些配置。

## 后台管理与平台支持

Windows：`Get-ScheduledTaskInfo -TaskName CodexSync`；停用：`Stop-ScheduledTask -TaskName CodexSync`、`Disable-ScheduledTask -TaskName CodexSync`。如果隐藏子进程尚在运行，关闭自己的 `codex-sync.exe`，再手动运行 sync 排查。Windows 依赖 WScript；禁用它的环境需要自行配置等效任务。

Linux：`systemctl --user status codex-sync`；查看输出：`journalctl --user -u codex-sync -n 30`；停用：`systemctl --user disable --now codex-sync`。

二进制：Windows x64、Linux x64（glibc 2.35+）、Linux ARM64（glibc 2.39+）。不支持 Alpine/musl 二进制。macOS 可从源码运行，未提供原生安装包。

macOS 自启动：创建 `~/Library/LaunchAgents/local.codex-sync.plist`，填入绝对路径：

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

运行 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.codex-sync.plist`；停用用 `launchctl bootout gui/$(id -u)/local.codex-sync`。

## 测试与发布

```bash
python -m unittest discover -s tests -v
npm ci
npm test
```

可选真实 CLI 验证：`python tests/probe_cli.py /absolute/path/to/codex`。仅使用临时 CODEX_HOME、假 Pro 凭证和本机捕获端点；生产同步仍严格 HTTPS。浏览器开发测试用 `node tests/dev_server.mjs`，它只监听回环地址、只用假密钥和内存配置，绝不能用于部署。

已测 Windows `codex-cli 0.155.0-alpha.2.6`：requires_openai_auth=true 时，实际请求使用假 API Key、未携带假 Pro token、假登录文件不变。管理页已通过本机浏览器的登录、保存、刷新读取、切回 Pro 保留 Key、退出测试；后端覆盖未授权请求、CSRF、冲突和存储错误。Actions 在 Windows/Linux x64/ARM64 运行客户端/云端测试、打包和成品断网保护测试。

未实测：真实第三方 API/模型、桌面 App 新会话端到端切换、设备重启后的自启动、macOS、Cloudflare R2 实际部署。编译通过不代表这些场景已验证。

推送 main / PR 自动测试和打包，`v*` 标签自动发布三平台 ZIP 与 SHA-256。Git 中只有占位符和测试假密钥；不要提交真实连接文件、config.toml、auth.json、环境变量或存储令牌。

依据：[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、[认证](https://learn.chatgpt.com/docs/auth)、[Vercel Private Blob](https://vercel.com/docs/vercel-blob/using-blob-sdk)、[Cloudflare R2](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/)。

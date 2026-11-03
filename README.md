# Telegram Task Runner

通过 Telegram 群命令执行本机脚本和可信插件，自动排队、记录状态并回报结果。适合团队共用构建机、部署及其他已注册的本地任务。运行时独立于 Codex，也不需要大模型 API。

通信、鉴权、插件发现、任务队列和通知共用一套 Python 核心；系统凭据与自动启动由薄适配层处理。

- **私有插件**：自动发现和热加载，可在 Git 忽略目录内 clone 独立的业务仓库。
- **按权限显示命令**：未授权用户、普通授权用户和应用管理员看到各自可用的菜单。
- **可追踪执行**：持久化队列、任务状态、进度和结果，查询与长任务分开处理。
- **用户会话自动启动**：Windows 计划任务、macOS LaunchAgent、Linux systemd 用户服务。

[安装与绑定教程](docs/setup.md) · [插件开发接口](docs/plugins.md) · [平台说明](docs/platforms.md) · [验证说明](docs/validation.md) · [权限与本机数据](SECURITY.md)

## 环境要求

| 项目 | 要求 |
|---|---|
| Python | 3.10+；Windows 使用 64 位 Python，macOS/Linux 需提供 venv |
| Git | 用于检出宿主及独立业务插件 |
| Windows | PowerShell、当前用户 DPAPI 和计划任务 |
| macOS | 当前用户 Keychain、LaunchAgent |
| Linux | 已解锁的 Secret Service、用户 D-Bus；自动启动需 systemd 用户服务 |
| 网络 | 执行机可访问 Telegram Bot API；macOS/Linux 安装时需下载 keyring |

每份部署绑定一个机器人和一个群或私人聊天。同一个 Bot Token 同时只由一个轮询进程使用。业务插件需要的 Git 凭据、编译工具和第三方程序由执行机提供。

## 快速开始

先在官方 [@BotFather](https://t.me/BotFather) 发送 `/newbot`，按提示创建机器人并保存 Token。把机器人添加到目标群，允许发送文字，隐私模式可保持默认。Token 在本机向导中隐藏输入。

检出主分支并进入仓库：

```bash
git clone https://github.com/QingC2206/app-telegram-task-runner.git
cd app-telegram-task-runner
```

下面先用公开的 `hello` 插件验证完整流程。`plugins/` 与 `runtime/` 会由准备命令创建。

### Windows PowerShell

```powershell
.\Install.ps1 -PrepareOnly
Copy-Item -LiteralPath .\examples\plugins\hello -Destination .\plugins\hello -Recurse
.\run.ps1 setup
.\run.ps1 doctor --online
.\run.ps1 run
```

前台收到群命令并验证后，Ctrl+C 退出。在同一 Windows 账户的普通 PowerShell 中登记**登录后自动启动**：

```powershell
.\run.ps1 service install
```

Windows 使用当前用户登录后的会话，不采集账户密码。也可直接运行 `Install.ps1` 完成绑定及登录启动登记；`-PrepareOnly` 只准备环境，`-StartupMode none` 完成绑定后保留为手动运行。

### macOS / Linux

```bash
bash install.sh --prepare-only
cp -R examples/plugins/hello plugins/hello
bash run.sh setup
bash run.sh doctor --online
bash run.sh run
# 前台验证后退出，再安装用户会话自动启动
bash run.sh service install --mode logon
```

macOS 使用 LaunchAgent，Linux 使用 systemd 用户服务；两者默认在用户会话启动后运行。凭据存储分别为 macOS Keychain 和 Linux Secret Service。Windows 使用当前用户 DPAPI。安全存储不可用时停止配置，不降级为明文。首次绑定需要访问 Telegram Bot API；macOS/Linux 首次安装还需下载 `keyring` 依赖。

### 完成绑定并验证

执行 `setup` 后，依次完成：

1. 在本机终端隐藏输入 BotFather 给出的 Token。
2. 在目标群发送终端实际显示的一次性命令，例如 `/bind@your_task_runner_bot 12ab34cd`。
3. 回到终端，核对群名称、群 ID 和管理员 ID，输入 `y`。其他授权用户填写纯数字 ID，也可直接回车跳过。
4. 看到终端“绑定完成”和群内成功消息后，运行前台宿主，在群中发送 `/hello@your_task_runner_bot 朋友`。

机器人返回问候和任务编号，即完成端到端验证。只发送群内 `/bind` 还没有完成绑定，终端确认和配置保存也必须完成。`your_task_runner_bot` 应替换为自己的机器人用户名，绑定口令以当前终端为准。

需要代理时，使用 `run.ps1 setup --proxy http://127.0.0.1:7890`；macOS/Linux 用 `bash run.sh setup --proxy http://127.0.0.1:7890`。使用执行机实际可用的代理地址。完整步骤及重新绑定方式见 [安装与绑定教程](docs/setup.md)。

## 群内使用

`机器人名` 替换为 BotFather 创建的用户名，在多人或多机器人群内使用明确命令：

| 命令 | 用途 | 权限 |
|---|---|---|
| `/help` | 列出自己可用的命令 | 所有用户 |
| `/whoami` | 查询自己的数字用户 ID | 所有用户 |
| `/status` | 查询宿主、Telegram 连接和通知状态 | 授权用户 |
| `/plugins` | 查看插件及自己可用的插件命令 | 授权用户 |
| `/jobs` | 查看最近任务 | 授权用户 |
| `/job 任务编号` | 查看指定任务状态、进度和结果 | 授权用户 |
| `/users` | 查看授权用户和管理员 | 应用管理员 |
| `/allow 用户ID` | 授权普通用户 | 应用管理员 |
| `/revoke 用户ID` | 撤销普通用户授权 | 应用管理员 |

例如 `/job@your_task_runner_bot 任务编号`。同一群内的消息及回复遵循群聊本身的可见性，按权限过滤菜单不会隐藏已有的群消息。

只有绑定的群和授权用户可以执行任务。初次绑定的人作为管理员；同事在群内发送 `/whoami@机器人名` 获取自身 ID，由管理员 `/allow@机器人名 用户ID` 授权。`/users` 查看授权列表，`/revoke 用户ID` 撤销普通用户。管理员身份通过本机 `setup` 管理。

群内输入 `/` 时，宿主按用户权限注册不同的命令菜单；`/help` 和 `/plugins` 同样过滤命令：

| 身份 | 可见命令 |
|---|---|
| 未授权用户 | `/help`、`/whoami` |
| 普通授权用户 | 状态、插件、任务查询，以及 `view` / `execute` 插件命令 |
| 应用管理员 | 普通用户命令、用户授权管理，以及 `admin` 插件命令 |

Telegram 群管理员身份不自动获得应用管理员权限。授权或撤权后唤醒菜单同步，插件变动每 5 秒检测；撤权会清除原有成员菜单，重启后也会补做清理。网络失败时自动重试，客户端可能需要重新打开群聊刷新。菜单使用 Telegram 的[群成员作用域](https://core.telegram.org/bots/api#botcommandscopechatmember)，执行时仍独立检查权限。

## 私有插件

公开仓库提供宿主与 `hello` 示例，具体业务插件需自行取得和部署。一个插件目录就是一个独立单元：

```text
app-telegram-task-runner/
  src/telegram_task_runner/        # 公共宿主
  examples/plugins/hello/          # 公开示例
  plugins/                        # 主仓库 Git 忽略
    your_private_plugin/          # 可独立 clone 私有仓库
      plugin.json
      main.py
  runtime/                        # Git 忽略：凭据引用、配置、任务、日志
```

插件目录名须与清单 `id` 一致；清单中的 `files` 显式列出代码和资源，`.git` 不进入任务快照。后台每 5 秒刷新插件与菜单，收到命令时再检查；停用、移除、错误和命令冲突在 `/plugins` 展示。已接受的任务固定代码及配置快照，更新插件不会改变排队和运行中的任务。

```bash
git clone <私有仓库地址> plugins/your_private_plugin
```

各插件独立管理 Git 历史，主仓库不记录私有仓库 URL 和提交指针。示例插件需复制 `examples/plugins/hello` 到 `plugins/hello` 后启用。

公开文档只使用宿主功能与 `hello` 通用示例。业务插件的名称、命令、流程、部署路径和执行记录保存在其私有仓库或本机忽略目录中，不写入宿主公开文档。

`plugin.json` 声明插件 ID、版本、平台、入口、文件清单及命令；命令声明 `mode`（`query` / `job`）和 `permission`（`view` / `execute` / `admin`）。当前 `view` 与 `execute` 均限授权用户，`admin` 限应用管理员。插件通过 `handle(request, ctx)` 接收参数、报告进度并返回结果，完整清单和接口见 [插件开发接口](docs/plugins.md)。

本机查看插件并验证示例：

```powershell
.\run.ps1 plugins
.\run.ps1 run-plugin hello 朋友
```

macOS/Linux 使用 `bash run.sh plugins` 和 `bash run.sh run-plugin hello 朋友`。本机直接运行 `mode=job` 的命令需追加 `--execute`；群内已授权命令按清单执行。插件以当前系统账户运行，应部署可信代码。

## 服务管理与更新

前台验证完成并退出后安装自动启动服务。Windows 在普通 PowerShell 中执行，macOS/Linux 将 `run.ps1` 替换为 `bash run.sh`：

```powershell
.\run.ps1 service install
.\run.ps1 service status
.\run.ps1 service stop
.\run.ps1 service start
.\run.ps1 service uninstall
```

`install` 登记并立即启动；`stop` 停止并禁用自动启动，`start` 恢复；`uninstall` 仅移除本应用的服务定义，保留本机配置、插件和记录。更改安装目录或 Python 路径前，应先在旧位置卸载服务，再在新位置重新绑定和登记。

升级宿主前用 `/jobs`、`/job` 确认任务已完成，再停止服务并拉取主分支：

```powershell
.\run.ps1 service stop
git switch main
git pull --ff-only origin main
.\run.ps1 service start
.\run.ps1 doctor --online
```

私有插件在各自目录更新，宿主自动检测新版本。`plugins/` 和 `runtime/` 继续保留在本机，不随宿主提交。

## 状态、数据与恢复

普通长任务单队列串行执行，查询使用独立工作线程；资源锁可跨插件共享。消息先持久化，再确认 Telegram 更新；同一更新只受理一次。服务中断后，排队任务保留，执行中任务标记 `uncertain`，先核对结果再恢复，不自动重新执行有副作用的命令。

执行状态与通知状态独立存储。Telegram 明确限流按 `retry_after` 等待；网络超时造成投递不明时记录 `unknown`，不会据此重跑任务。授权用户可用 `/jobs`、`/job` 查看已经保存的结果。业务任务的恢复方式由插件提供。

| 路径 | 内容 |
|---|---|
| `runtime/robot.json` | 机器人绑定、授权及系统保护凭据的引用或密文 |
| `runtime/state-*.sqlite3` | 消息收件箱、任务、资源锁和通知队列 |
| `runtime/runner.log` | 宿主日志 |
| `runtime/health.json` | 心跳、连接状态和工作线程诊断 |
| `runtime/jobs/<任务编号>/` | 固定代码快照、`worker.log`、`events.jsonl` 和 `result.json` |
| `runtime/plugin-state/<插件ID>/` | 插件本机配置和持久状态 |

这些目录可能包含业务参数和执行结果，保持本机管理。Bot Token 使用操作系统凭据保护，迁移到其他账户或平台时重新执行 `setup`。备份 SQLite 数据前先停止服务，并连同插件持久状态一起保留。

宿主每 5 秒更新 `runtime/health.json`。群内 `/status` 会显示最近一次成功连接 Telegram 的时间、工作线程状态和通知计数；本机 `doctor --offline` 同时读取心跳。进程存在但长时间没有心跳会报告 `stale`，连接失败会报告重连，避免把计划任务的 Running 状态直接当作业务正常。

## 常见问题

| 现象 | 检查方法 |
|---|---|
| 群里发命令没有回复 | 确认已完成本机绑定、服务在运行、群 ID 正确；先发送 `/whoami@机器人名`，再由管理员授权 |
| 显示的命令少 | 菜单按当前权限过滤；确认授权和插件已加载，重新进入群聊刷新客户端缓存 |
| 插件没有出现 | 用本机 `plugins` 或群内 `/plugins` 检查；目录名必须匹配清单 ID，平台声明、文件、语法和命令冲突均影响加载 |
| 提示轮询冲突 / 409 | 同一 Token 有其他轮询进程；关闭重复前台实例或其他设备上的同 Token 服务 |
| Telegram 网络失败 | 用 `doctor --online` 核对身份和网络，确认后台使用的代理正在运行 |
| Windows 提示禁止运行脚本 | 可在当前 PowerShell 窗口执行 `Set-ExecutionPolicy -Scope Process Bypass` 后重试，仅对该窗口生效 |
| macOS/Linux 凭据不可用 | 检查系统钥匙串授权；Linux 还需已解锁的 Secret Service 和用户 D-Bus 会话 |
| 更新 Token 或重新绑定 | 先停止本服务，再运行 `setup`，完成后重新启动；不要同时运行绑定向导与宿主 |

## 验证

```powershell
.\runtime\venv\Scripts\python.exe -m unittest discover -s tests -v
.\run.ps1 doctor --offline
.\run.ps1 doctor --online
```

macOS/Linux 测试命令为 `runtime/venv/bin/python -m unittest discover -s tests -v`。GitHub Actions 在 Windows、macOS 和 Ubuntu 上运行同一套公共测试，当前 39 项测试已通过。测试范围与平台验收方法见 [验证说明](docs/validation.md)；CI 不包含业务插件或真实部署验收。

## 许可证与来源

使用 [MIT 许可证](LICENSE)。机器人绑定、凭据及管理思路参考 [app-codex-telegram-bridge](https://github.com/QingC2206/app-codex-telegram-bridge)，本应用通过自己的 Bot Token 独立运行。复用代码声明见 [第三方说明](THIRD_PARTY_NOTICES.md)。

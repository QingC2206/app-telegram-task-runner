# 创建、绑定和安装机器人

## 1. 创建独立 Bot

在 Telegram 中打开官方 [@BotFather](https://t.me/BotFather)，核对用户名后发送 `/newbot`。设置显示名称（例如“团队任务助手”）和可用的用户名（例如 `your_task_runner_bot`，以 `bot` 结尾）。保存 BotFather 返回的 Token。

为本服务使用独立 Bot。同一个 Token 只由一个轮询服务使用。Token 不要发送到群聊、Issue 或对话中；安装向导会在本机隐藏输入。

## 2. 准备运行环境

Windows 需要 64 位 Python 3.10+。在仓库目录的 PowerShell 运行：

```powershell
.\Install.ps1 -PrepareOnly
```

它只在本仓库的 `runtime/venv` 创建环境并检查插件，不修改全局 Python、GitHub、Gitee 或 SSH 配置。

macOS/Linux 使用 `bash install.sh --prepare-only`，需要 Python 的 venv 支持。Linux 还需正在运行且已解锁的 Secret Service，例如 GNOME Keyring，以及用户 D-Bus 会话；系统缺失这些能力时需先由系统包管理器配置。macOS 可能提示允许实际 Python 程序访问 Keychain。

## 3. 绑定目标群

创建群，把自己、同事和新 Bot 加入群。机器人一般只需普通成员及发送文字权限，隐私模式保持默认即可。

Windows 执行：

```powershell
.\run.ps1 setup
```

macOS/Linux 替换为 `bash run.sh setup`。按提示隐藏输入 Token。程序调用 `getMe` 验证机器人，然后打印本次独有的一次性绑定口令，例如：

```text
/bind@your_task_runner_bot 12ab34cd
```

由你在目标群发送终端实际显示的命令。回到终端，核实群名称、群 ID 和管理员用户 ID，输入 `y` 确认。其他授权用户 ID 可暂时留空。配置和系统加密凭据保存完成后，群里会收到绑定成功消息。

只发送群里的 `/bind` 命令还没有完成绑定，必须继续完成本机确认。授权用户栏填写的是纯数字 ID，不是 `@用户名`；输入无效时会原地重试，也可以直接回车跳过。

绑定窗口为 10 分钟，口令每次重新生成。私人聊天也可以绑定。不会根据不带口令的消息自动选群；匿名管理员身份不能完成绑定。

如后台网络需要代理：

```powershell
.\run.ps1 setup --proxy http://127.0.0.1:7890
```

填写你实际可用的代理地址。桌面客户端能联网不代表后台进程一定能使用其代理；代理程序尚未运行时，Bot 会等待网络恢复。

## 4. 前台验收和同事授权

```powershell
.\run.ps1 doctor --online
.\run.ps1 run
```

群中发送 `/help@机器人名`、`/plugins@机器人名`。若按 README 启用了公开 `hello` 示例，再发送 `/hello@机器人名 朋友` 完成任务验证。其他业务命令以自行部署的插件为准。

同事在群中发送 `/whoami@机器人名`，获取自己的数字 ID。你作为管理员发送 `/allow@机器人名 数字ID`。未授权用户无法执行插件；群内用户名更改不影响数字 ID 授权。

输入 `/` 时，未授权同事只看到 `/help` 和 `/whoami`；授权后会出现普通用户命令和可执行插件，管理员额外看到用户管理和管理员插件。`/help`、`/plugins` 也遵循相同权限。授权和撤权会自动更新菜单，网络恢复或重启后会重试未完成的更新；客户端显示旧列表时可重新打开群聊。无需在 BotFather 手工填写全量命令，Telegram 群管理员也不会自动成为应用管理员。

## 5. Windows 自动启动

前台验证后，Ctrl+C 退出。使用绑定 Token 的同一个 Windows 账户，在普通 PowerShell 执行：

```powershell
.\run.ps1 service install
```

它使用 LogonTrigger 和当前账户的 InteractiveToken，不采集 Windows 账户密码，登录后自动启动，注销后停止。也可以运行 `& .\tools\Install-Logon.ps1`，依次完成尚未完成的绑定、连接检查、登录启动登记和状态查询；已有绑定会保留。

### 管理

```powershell
.\run.ps1 service status
.\run.ps1 service stop
.\run.ps1 service start
.\run.ps1 service uninstall
```

任务名称按仓库根目录生成独立标识，与旧 Codex 机器人分开。卸载只移除本应用登记的自动启动任务，保留插件、配置和执行记录。

安装后检查任务状态、群内 `/status` 和首次执行结果。登录会话内的业务工具凭据、授权及网络代理继续由当前账户提供。

## 6. macOS / Linux 自动启动

前台验证后退出，再执行 `bash run.sh service install --mode logon`。

macOS 注册当前用户的 LaunchAgent，随登录加载；Linux 注册 systemd 用户服务，随用户管理器启动。默认不安装系统级守护进程，也不自动开启 Linux lingering，因为当前凭据依赖用户系统钥匙串。需要未登录的服务器部署时，应另行设计可用的凭据和服务账户，再做实机验收。

管理命令仍为 `bash run.sh service status|start|stop|uninstall`。

## 重新配置与故障排查

先停止本应用服务，再运行 `setup` 更新 Token 或绑定。相同 Bot、相同聊天重新绑定会保留现有授权，重新绑定的人也登记为管理员。换 Bot 或聊天使用独立的状态数据库，旧任务和日志留在本地，不向新群恢复执行。

Token 失效先在 BotFather 轮换，然后在本机重新配置。若提示轮询冲突，检查是否有其他服务使用同一个 Token；不要同时运行前台和后台实例。Webhook 已配置时不会擅自删除，需先核实原服务。

参考：[Telegram 隐私模式](https://core.telegram.org/bots/features#privacy-mode)、[Bot API](https://core.telegram.org/bots/api)、[Windows 任务登录方式](https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_logon_type)。

# 跨平台结构与启动边界

| 能力 | Windows | macOS | Linux |
|---|---|---|---|
| 公共宿主 | 同一 Python 核心 | 同一 Python 核心 | 同一 Python 核心 |
| 凭据 | 当前用户 DPAPI | 显式 macOS Keychain 后端 | 显式 Secret Service 后端 |
| 安装入口 | `Install.ps1` | `bash install.sh` | `bash install.sh` |
| 运行入口 | `run.ps1` | `bash run.sh` | `bash run.sh` |
| 自动启动方案 | 当前用户 LogonTrigger / InteractiveToken | 当前用户 LaunchAgent | systemd 用户服务 |
| 账户会话 | 登录后启动，注销后停止 | 随用户登录加载 | 随用户管理器启动 |
| 工作进程清理 | Job Object | POSIX 进程组 | POSIX 进程组 |

`host.py`、`storage.py`、`plugins.py`、`plugin_api.py` 和业务路由不包含桌面自动化依赖。`platforms/` 负责凭据、进程生命周期和原生服务定义；Shell / PowerShell 入口只准备独立 Python 环境和调用公共 CLI。

系统钥匙串仅在对应平台导入。macOS/Linux 通过 `keyring` 的显式系统后端访问，不选择第三方明文或空实现；系统钥匙串缺失、锁定或拒绝授权时停止。参考 [keyring 官方文档](https://keyring.readthedocs.io/en/latest/)。

应用根目录与 Bot 有各自的运行锁，服务名称由根目录生成。Token 只能交给一个活动轮询进程，包括其他设备和其他软件中的进程。

macOS LaunchAgent 通过 `launchctl` 管理当前 GUI 会话，Linux 单元通过 `systemctl --user` 管理。后台运行环境需要能够访问钥匙串、Git SSH agent 和代理；完整原生验收包括登录、锁屏、注销、重启和断网恢复。

业务插件通过清单的 `platforms` 声明支持的系统。平台不适用时只停用该插件，其他插件继续可用；平台工具和依赖由插件维护者自行配置。

CI 在三个系统上运行同一套公共测试；原生服务安装、系统钥匙串和桌面会话行为仍需在对应部署系统验收。

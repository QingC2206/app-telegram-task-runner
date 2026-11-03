# 验证与平台边界

本页说明公开宿主的测试范围和安装验收方法。业务插件及现场运行记录由部署者在私有仓库或本机忽略目录中管理。

## 公共自动化测试

Windows：

```powershell
.\runtime\venv\Scripts\python.exe -m unittest discover -s tests -v
```

macOS / Linux：

```bash
runtime/venv/bin/python -m unittest discover -s tests -v
```

[GitHub Actions](https://github.com/QingC2206/app-telegram-task-runner/actions/workflows/tests.yml) 在 Windows、macOS 和 Ubuntu 上运行公共测试。当前套件包含 39 项；仅适用于特定系统的测试在其他系统上按声明跳过。

覆盖范围：

- 聊天和用户授权、命令目标、匿名或机器人发送者、过期命令及授权管理。
- 按权限分发菜单、帮助和插件清单，授权变更、重启清理与 API 重试。
- 插件发现、热更新、平台声明、冲突处理，以及代码和配置快照。
- 消息去重、持久化收件箱、查询与长任务分离、资源队列和中断恢复。
- 子进程超时清理、插件异常、环境变量白名单及通知投递状态。
- 绑定向导与输入校验、合成凭据保护、心跳诊断。
- Windows 登录启动定义、macOS plist 参数和 Linux 用户服务定义。

公共集成测试模拟 Telegram API，并实际启动示例工作进程。CI 不使用真实 Bot Token，不连接业务系统，也不安装本机登录服务。

## 安装后验收

按 [安装教程](setup.md) 完成绑定后，启用公开的 `hello` 插件，并核对以下行为：

1. 前台宿主可接受群内命令，返回示例任务结果。
2. 未授权用户、授权用户和管理员的菜单符合各自权限。
3. 服务启动后可用 `doctor --online` 核对连接，用群内 `/status` 查看运行状态。
4. 使用当前用户的原生自动启动方式验证登录、注销、重启和网络恢复行为。
5. 核对队列与通知状态后再重启服务，确认已完成任务不会自动重跑。

平台原生服务和系统钥匙串需要在实际部署系统验收。自动化测试通过不等于所有桌面会话环境均完成验收。

现场验收记录可能包含账户、聊天和任务信息，应保存在私有位置，不提交到本仓库。

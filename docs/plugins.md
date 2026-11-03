# 本地插件接口 v1

插件是可信本机代码。每次调用使用独立进程来隔离崩溃和超时，拥有宿主运行账户的本机权限；不是操作系统安全沙箱。鉴权与业务执行由宿主统一调度，插件无需持有 Bot Token。

## 目录与清单

将插件仓库 clone 到 `plugins/<id>/`。宿主只扫描直接子目录中的 `plugin.json`；主仓库忽略整个 `plugins/`，插件内部的 `.git` 继续独立工作。仓库目录名与清单 id 必须完全一致（小写字母、数字、下划线，最多 32 字符）。清单格式：

```json
{
  "api_version": 1,
  "id": "hello",
  "version": "1.0.0",
  "platforms": ["windows", "macos", "linux"],
  "entrypoint": "main.py",
  "files": ["main.py", "helpers.py"],
  "enabled": true,
  "commands": {
    "hello": {
      "description": "查询示例",
      "mode": "query",
      "permission": "view",
      "timeout_seconds": 15
    },
    "hello_build": {
      "description": "执行长任务",
      "mode": "job",
      "permission": "execute",
      "timeout_seconds": 3600,
      "resources": ["my-build-workspace"]
    }
  }
}
```

`platforms` 可省略，表示跨三平台。平台不适用时只停用该插件。`files` 显式列出入口、导入模块和所需资源；所有路径必须位于插件目录，最多 200 文件、总计 10 MB，不收录 `.git` 等隐藏目录。清单与文件在扫描期间改变会停止加载该版，后续扫描重试。

查询最大 60 秒、默认 30 秒；普通任务最大 7200 秒、默认 3600 秒。长任务单独排队，查询使用另一执行线程，互不阻塞。声明相同 `resources` 的任务共享资源锁；代码里仍应维护 Git 和具体工具自己的工作区一致性检查。

`permission` 支持 `view`、`execute`、`admin`；当前 view/execute 均限授权用户，admin 限管理员。宿主保留 `/help`、`/status`、`/plugins`、`/jobs`、`/job`、`/whoami` 和用户管理命令，插件不能覆盖。插件命令冲突时冲突双方停用，其余插件继续可用。

该字段同时决定群成员的 `/` 菜单、`/help` 和 `/plugins` 可见命令，无需插件额外注册菜单。未授权用户只看到帮助和身份查询；普通授权用户看不到 `admin` 命令。插件热加载、停用或权限变化后自动同步各成员菜单；Telegram 每个菜单最多 100 项。应用管理员由本机配置管理，独立于 Telegram 群管理员。

## 入口

```python
from telegram_task_runner.plugin_api import result

def handle(request, ctx):
    ctx.progress("正在检查输入", stage="check")
    name = request["argument"] or "朋友"
    return result("你好，" + name, data={"job_id": ctx.job_id})
```

`request` 包含 `command`、`argument`、`user_id`、`chat_id`、`message_id`。权限已由宿主在受理和任务开始时检查；参数由插件继续按自身业务规则验证。使用固定程序路径和参数列表调用外部工具，避免把聊天原文拼成 shell 命令。

`ctx` 提供：

| 成员 | 含义 |
|---|---|
| `plugin_dir` | 本次固定的代码与资源快照目录，作为只读代码来源 |
| `state_dir` | `runtime/plugin-state/<id>/`，插件持久状态目录 |
| `job_dir` | `runtime/jobs/<id>/`，本次任务目录 |
| `config` | 受理时的本机配置快照 |
| `job_id` | 宿主任务编号 |
| `progress(text, stage)` | 追加结构化进度，群内 `/job 编号` 可查询 |
| `load_state()` / `save_state(value)` | 读取/原子保存插件 `state.json` |

配置优先来自 `runtime/plugin-state/<id>/config.json`，否则采用清单的 `config` 对象，最大 64 KB。每次发现时重新读取；新的任务使用新配置，已接受的任务保留旧配置。

`result(message, ok=True, data=None, status=None)` 返回普通文字、结构化数据和状态。合法状态为 `succeeded`、`failed`、`push_pending`、`uncertain`。失败应 `ok=False`。不支持插件自行轮询 Telegram，也不把 Bot Token、聊天历史或任意父进程环境交给插件；标准网络代理、SSH agent socket 等运行环境按白名单传递。

stdout/stderr 保存到本次 `worker.log`，不是通信协议；结果写入 `result.json`，进度写入 `events.jsonl`。插件必须等待自己启动的业务流程完成，避免启动游离的后台进程。超时或宿主退出时，Windows Job Object / POSIX 进程组会清理本次进程树。

## 热更新与执行记录

后台每 5 秒刷新，收到命令时再验证；菜单随命令变化同步。修改清单、版本或本机配置后，新任务获取新指纹。任务在受理时复制清单列出的文件及配置，排队时就固定版本，不在插件更新后重新解释旧任务。

服务重启后已受理的排队任务仍可运行；执行中任务结果标记不确定，避免盲目重跑。新的业务插件应提供可核对的执行标识和必要的恢复命令。通知失败不会改变业务结果，也不会自动重执行业务。

## 独立仓库管理

私有源码放入 `plugins/<id>`，凭据和本机路径配置优先放入 `runtime/plugin-state/<id>`。主仓库的忽略规则不替代私有仓库自己的 `.gitignore`；插件仓库提交前仍需检查自身暂存内容。不要把任务日志、工作区 DLL、账户 Token 或 `.git` 加入 `files`。

本机独立验证：Windows 用 `run.ps1 run-plugin 命令`；macOS/Linux 用 `bash run.sh run-plugin 命令`。`mode=job` 的本机执行必须显式追加 `--execute`。Telegram 内已经通过鉴权的命令按清单直接受理。

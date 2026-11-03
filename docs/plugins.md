# 本地插件接口与公共约束规范 v1

插件是可信本机代码。每次调用使用独立进程来隔离崩溃和超时，拥有宿主运行账户的本机权限；不是操作系统安全沙箱。鉴权、菜单同步、附件下载与回传、队列调度及表情反馈均由宿主统一收口，插件无需持有 Bot Token，也不直接连接 Telegram API。

## 一、 公共边界约束（什么必须遵守，什么明确不做）

1. **非交互与确定性执行**：宿主固定 `stdin=DEVNULL` 且无窗口运行。严禁在插件或脚本中使用 `input()`、`Read-Host`、GUI 弹窗等交互式等待。
2. **禁止后台游离进程**：插件必须等待自己启动的业务子进程结束；超时或宿主退出时，Windows Job Object / POSIX 进程组会清理整个进程树。
3. **配置下沉、参数极简**：本机路径、仓库位置和编译开关统一放入 `runtime/plugin-state/<id>/config.json`，禁止通过聊天参数拼接 Shell 命令或透传本机路径。
4. **标准四动作收敛**：建议业务插件按统一前缀收敛为 4 类标准命令：
   - `<prefix>_version` / `<prefix>_info`（`mode=query`, `permission=view`）：只读查询当前版本与产物摘要。
   - `<prefix>_check`（`mode=query`, `permission=view`）：环境、路径与依赖预检，零副作用。
   - `<prefix>_<action>`（`mode=job`, `permission=execute`）：主执行任务，声明 `resources` 互斥锁并支持幂等跳过。
   - `<prefix>_retry_<step>`（`mode=job`, `permission=execute`）：针对后置推送/发布失败的恢复命令。

## 二、 目录与清单规范（`plugin.json`）

将插件仓库 clone 到 `plugins/<id>/`。宿主只扫描直接子目录中的 `plugin.json`；主仓库仅跟踪 `plugins/.gitkeep` 占位文件，其余插件内容均忽略，插件内部的 `.git` 继续独立工作。仓库目录名与清单 `id` 必须完全一致（小写字母、数字、下划线，最多 32 字符）。清单格式：

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
      "description": "执行长任务并处理附件",
      "mode": "job",
      "permission": "execute",
      "timeout_seconds": 3600,
      "resources": ["my-build-workspace"],
      "input_files": {
        "required": true,
        "kinds": ["document"],
        "extensions": [".bin", ".zip"],
        "max_size_mb": 500
      },
      "output_files": {
        "max_count": 3,
        "max_size_mb": 500
      }
    }
  }
}
```

- `platforms` 可省略，表示跨三平台。平台不适用时只停用该插件。
- `files` 显式列出入口、导入模块和所需资源；所有路径必须位于插件目录，最多 200 文件、总计 10 MB，不收录 `.git` 等隐藏目录。
- 查询（`query`）最大 60 秒、默认 30 秒；普通任务（`job`）最大 7200 秒、默认 3600 秒。长任务单独排队，查询使用独立执行线程，互不阻塞。声明相同 `resources` 的任务共享互斥资源锁。
- `permission` 支持 `view`、`execute`、`admin`；当前 `view`/`execute` 均限授权用户，`admin` 限管理员。宿主保留 `/help`、`/status`、`/plugins`、`/jobs`、`/job`、`/whoami` 和用户管理命令，插件不能覆盖。

## 三、 图片/附件收发与大文件机制

### 1. 触发方式（无状态原子绑定）
对于声明了 `input_files` 的命令，用户在群聊或管理员私聊中可通过以下方式触发（不支持多文件相册连发 `media_group`，多文件请打包为单个 `.zip`）：
- **附言直发（Caption）**：发送文件或图片时，直接在说明文字中填写 `/command [参数]`（群聊支持 `/command@bot` 或 `/command @bot`）。
- **回复引用（Reply）**：选中聊天中已有的文件或图片消息，**Reply（回复）** 发送 `/command [参数]`。
- **紧邻发送**：同一用户在当前聊天发送附件后 10 分钟内直接发送 `/command`，自动关联并消费该附件。

宿主在入队前自动校验 `required`、`kinds`（`document` 原文件 / `photo` 预览图）、`extensions` 后缀白名单及 `max_size_mb`（1–2000 MB）。

### 2. 大文件支持（突破 20MB / 50MB 公网限制）
- **插件声明上限最高可达 2000 MB（2 GB）**，插件代码始终直接读写本地完整文件。
- **自动分卷回传**：在默认公网 Bot API 下，若插件产出的单个 `document` 超过 49 MB，宿主 `outbox` 会自动压缩切分为 `.zip.001`、`.zip.002` 等分卷依序回发。
- **Local Bot API Server 通道**：在 `runtime/robot.json` 中配置本机 `api_base_url` 后，单文件直传与接收上限直接提升至 2000 MB，且支持本机磁盘零拷贝读取。

## 四、 入口协议与单次任务目录隔离

```python
from pathlib import Path
from telegram_task_runner.plugin_api import document, result

def handle(request, ctx):
    ctx.progress("正在检查输入", stage="check")
    if request.get("files"):
        src = Path(request["files"][0]["path"])
        out = ctx.outputs_dir / src.name
        out.write_bytes(src.read_bytes())
        return result(
            f"已处理文件：{src.name}",
            files=[document(out, name=src.name, caption="处理结果")],
            data={"job_id": ctx.job_id},
        )
    name = request["argument"] or "朋友"
    return result("你好，" + name, data={"job_id": ctx.job_id})
```

### 1. 请求对象（`request`）
`request` 包含 `command`、`argument`、`user_id`、`chat_id`、`message_id` 以及已由宿主下载到本地的 `files` 列表：
```python
request["files"] = [
    {
        "kind": "document",              # "document" | "photo"
        "name": "example.bin",           # 清洗后的安全文件名
        "path": ".../runtime/jobs/<job_id>/inputs/example.bin",
        "size": 102400,
        "mime_type": "application/octet-stream",
        "md5": "...",
        "sha256": "..."
    }
]
```

### 2. 上下文与目录隔离（`ctx`）
**每次调用脚本都会分配全局唯一的 16 位 `job_id`**，生成独立的 `runtime/jobs/<job_id>/` 目录，多次并发或连续调用互不干扰：

| 成员 | 含义 |
|---|---|
| `plugin_dir` | `runtime/jobs/<job_id>/code/`，本次固定的只读代码与资源快照 |
| `inputs_dir` | `runtime/jobs/<job_id>/inputs/`，本次任务宿主下载的输入附件目录 |
| `outputs_dir` | `runtime/jobs/<job_id>/outputs/`，本次任务待回传给聊天框的输出文件目录 |
| `job_dir` | `runtime/jobs/<job_id>/`，包含 `worker.log`、`events.jsonl` 和 `result.json` |
| `state_dir` | `runtime/plugin-state/<id>/`，跨任务保留的插件持久状态与配置目录 |
| `config` | 受理时的本机配置快照（优先读取 `state_dir/config.json`） |
| `job_id` | 本次宿主任务唯一编号 |
| `progress(text, stage)` | 追加结构化进度，群内/私聊可通过 `/job 编号` 查询 |
| `load_state()` / `save_state(value)` | 读取/原子保存插件跨任务 `state.json` |

### 3. 返回值与回传文件安全校验
- `result(message, ok=True, data=None, status=None, files=None)` 返回结果文字、结构化数据、状态码及待回传文件列表。
- 合法状态为 `succeeded`、`failed`、`push_pending`、`uncertain`。失败应 `ok=False`。
- `files` 可使用 `document(path, name=..., caption=...)` 或 `photo(path, name=..., caption=...)` 构造。**所有回传文件必须位于本次任务的 `ctx.job_dir`（推荐 `ctx.outputs_dir`）内**，宿主强制校验路径包含关系，拒绝回传任务目录外的任意本机文件。

## 五、 缓存容量管理（Cache GC）与原生表情反馈

### 1. 任务目录 Cache 自动淘汰
为防止多次大文件任务占满磁盘，宿主在**每次创建新任务目录前**自动检测 `runtime/jobs/` 容量：
- 默认配额上限为 `max_jobs_cache_mb = 512`（512 MB）及 `max_retained_jobs = 100`（可在 `runtime/robot.json` 调整）。
- 排队中（`queued`）、执行中（`running`）、待恢复（`push_pending` / `uncertain`）以及尚有未送达通知/附件的任务目录受保护不会被删除。
- 当预计容量或目录数超限时，按创建时间**从早到晚（FIFO）自动删除最早的已终结任务目录**。

### 2. 原生表情状态反馈（Reaction Lifecycle）
宿主在用户发起命令的原消息上自动维护四态表情反馈（基于 `setMessageReaction`）：
- `👀`：命令已受理入队，或结果/投递处于待核对状态（`uncertain` / `push_pending` / `unknown`）。
- `🤔`：工作线程已认领任务，正在下载输入附件或执行插件脚本。
- `🎉`：脚本执行成功（`ok=True`）**且**所有结果通知与回传附件均已确认送达 Telegram。
- `😢`：前置校验被拒、脚本返回 `failed`，或通知投递被明确拒绝。

终态表情（`🎉` / `😢`）具有单向锁定期，不会被延迟的中间态覆盖；网络闪断导致的表情更新失败会在后台自动补试。

## 六、 独立仓库管理与本机验证

私有源码放入 `plugins/<id>`，凭据和本机路径配置放入 `runtime/plugin-state/<id>`。不要把任务日志、工作区产物、账户 Token 或 `.git` 加入 `files`。

本机独立验证：
- Windows：`.\run.ps1 run-plugin 命令 [参数] [--file 本地文件路径] [--execute]`
- macOS/Linux：`bash run.sh run-plugin 命令 [参数] [--file 本地文件路径] [--execute]`
`mode=job` 的本机执行必须显式追加 `--execute`。


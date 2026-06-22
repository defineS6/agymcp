# agymcp

`agymcp` 是一个 stdio MCP 服务，把本机已安装并登录的 Google Antigravity CLI（`agy`）封装成可被 Claude Code、Codex、Cursor 等 MCP 客户端调用的工具。

本项目参考了 [GuDaStudio/geminimcp](https://github.com/GuDaStudio/geminimcp) 的轻量 FastMCP 结构：Python 包、console script 入口、stdio transport。`agy` 本身没有 Gemini CLI 的 `stream-json` 输出，所以这里采用稳定的 stdout/stderr 封装，并补充 `doctor` 与模型列表工具。

## 前置要求

- Python 3.11+
- 已安装 `uv`
- 已安装并完成登录的 `agy` CLI

本项目不会读取本地 `.env` 文件。需要指定 CLI 路径时，请使用进程环境变量：

- `AGY_PATH`：`agy` 可执行文件路径或命令名
- `AGY_CMD`：兼容其它桥接项目的可执行文件变量，优先级低于 `AGY_PATH`

## 快速安装

### Windows PowerShell

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv tool install git+https://github.com/defineS6/agymcp.git
agy-doctor --skip-models
claude mcp add agy -s user --transport stdio -- agymcp
```

### Linux / macOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/defineS6/agymcp.git
agy-doctor --skip-models
claude mcp add agy -s user --transport stdio -- agymcp
```

如果不想安装到 `uv tool`，也可以让 MCP 客户端每次通过 `uvx` 从 GitHub 启动：

```bash
claude mcp add agy -s user --transport stdio -- uvx --from git+https://github.com/defineS6/agymcp.git agymcp
```

## 本地开发

```powershell
uv sync
uv run pytest
uv run agy-doctor
uv run agymcp
```

`uv run agymcp` 会启动 stdio MCP 服务，通常由 MCP 客户端托管，不需要手动长期运行。

## 客户端注册

Claude Code 示例：

```bash
claude mcp add agy -s user --transport stdio -- uvx --from git+https://github.com/defineS6/agymcp.git agymcp
```

Codex CLI 示例：

```toml
[mcp_servers.agy]
command = "uvx"
args = ["--from", "git+https://github.com/defineS6/agymcp.git", "agymcp"]
```

如果已通过 `uv tool install git+https://github.com/defineS6/agymcp.git` 安装，也可以把命令简化为 `agymcp`。

如果本机访问 Google/Antigravity 需要代理，请把代理变量写进 MCP 配置，让 `agy` 子进程继承：

```json
{
  "command": "agymcp",
  "args": [],
  "type": "stdio",
  "startup_timeout_sec": 300,
  "env": {
    "HTTP_PROXY": "http://127.0.0.1:7890",
    "HTTPS_PROXY": "http://127.0.0.1:7890",
    "NO_PROXY": "localhost,127.0.0.1"
  }
}
```

## MCP 工具

| 工具 | 用途 |
| --- | --- |
| `agy` | 同步执行一次 `agy --print`，支持工作目录、模型、沙箱、会话续接、额外目录、超时和输出上限 |
| `agy_continue` | 续接指定 `SESSION_ID`，或使用 `agy --continue` 续接最近会话 |
| `agy_models` | 执行 `agy models`，用于查看本机可用模型 |
| `agy_doctor` | 检查 `agy` 路径、版本、帮助输出、模型命令和工作目录，不触发真实模型调用 |

`agy` 与 `agy_continue` 会启动真实 Antigravity 请求，可能消耗额度。`agy_doctor` 与 `agy_models` 只做本地/元数据检查。`agy_doctor` 的 `healthy` 只代表 MCP 服务具备启动和调用 `agy --print` 的基础能力，`optional_healthy` 会把 `agy models` 这类可选检查也计算进去。

## `agy` 参数

- `PROMPT`：发送给 `agy --print` 的完整提示词
- `cd`：执行命令的工作目录
- `sandbox`：追加 `--sandbox`
- `SESSION_ID`：追加 `--conversation <id>` 续接指定会话
- `continue_last`：追加 `--continue` 续接最近会话，不能与 `SESSION_ID` 同时使用
- `model`：追加 `--model <name>`，默认使用 `Gemini 3.1 Pro (High)`
- `add_dirs`：重复追加 `--add-dir <path>`
- `skip_permissions`：追加 `--dangerously-skip-permissions`
- `timeout_seconds`：本包装器等待进程完成的超时时间
- `print_timeout_seconds`：传给 `agy --print-timeout` 的超时时间
- `max_output_chars`：stdout/stderr 的最大保留字符数
- `extra_env`：本次调用额外注入的环境变量

返回结构包含 `success`、`agent_messages`、`SESSION_ID`、`returncode`、`timed_out`、`truncated`、`stderr` 和必要的 `error` 字段。

## 安全边界

- 不读取、不解析、不引用任何本地 `.env` 文件。
- 子进程使用 `shell=False`，参数列表传递，避免 shell 拼接。
- 返回内容会对 Home 路径、Bearer/JWT/PEM/AKIA 等常见敏感片段做脱敏。
- `--dangerously-skip-permissions` 只有调用方显式传入 `skip_permissions=true` 才会启用。
- `SESSION_ID` 只能续接 `agy` 自己的会话，不能继承 MCP 客户端上下文。

"""agy CLI 调用、校验和诊断逻辑。"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_PRINT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_OUTPUT_CHARS = 200_000

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer <redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted:aws-access-key>"),
    (re.compile(r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----", re.DOTALL), "<redacted:private-key>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "<redacted:jwt>"),
)

_CONVERSATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:conversation|session)[\s_-]*(?:id)?\s*[:=]\s*([A-Za-z0-9._:-]{8,})\b", re.IGNORECASE),
    re.compile(r"\b(?:conversation|session)\s+([A-Za-z0-9._:-]{8,})\b", re.IGNORECASE),
    re.compile(r"\bCreated conversation\s+([A-Za-z0-9._:-]{8,})\b", re.IGNORECASE),
    re.compile(r"\bPrint mode:\s*conversation=([A-Za-z0-9._:-]{8,})\b", re.IGNORECASE),
)

_AUTH_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"keyringAuth:\s*timed out", re.IGNORECASE),
    re.compile(r"silent auth failed", re.IGNORECASE),
    re.compile(r"auth timed out", re.IGNORECASE),
    re.compile(r"You are not logged into Antigravity", re.IGNORECASE),
)


class AgyMcpError(Exception):
    """agymcp 可预期错误基类。"""


class AgyNotFoundError(AgyMcpError):
    """找不到 agy 可执行文件。"""


class AgyValidationError(AgyMcpError):
    """调用参数校验失败。"""


@dataclass(frozen=True)
class ProcessResult:
    """子进程执行结果。"""

    command: list[str]
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_seconds: float

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def redact_text(value: str) -> str:
    """对返回给 MCP 客户端的文本做基础脱敏。"""
    if not value:
        return value

    redacted = value
    home = str(Path.home())
    if home:
        redacted = redacted.replace(home, "~").replace(Path(home).as_posix(), "~")

    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def resolve_agy_binary(binary: str | None = None) -> str:
    """解析 agy 可执行文件路径，不读取任何 .env 文件。"""
    explicit = binary or os.environ.get("AGY_PATH") or os.environ.get("AGY_CMD")
    if explicit:
        return _resolve_explicit_binary(explicit)

    discovered = shutil.which("agy") or shutil.which("agy.exe")
    if discovered:
        return discovered

    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            fallback = Path(local_app_data) / "agy" / "bin" / "agy.exe"
            if fallback.is_file():
                return str(fallback)

    raise AgyNotFoundError("未找到 agy CLI，请安装 Antigravity CLI 或设置 AGY_PATH/AGY_CMD。")


def ensure_directory(path: str | Path, label: str) -> Path:
    """确认目录存在并返回绝对路径。"""
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise AgyValidationError(f"{label} 不存在：{resolved}")
    if not resolved.is_dir():
        raise AgyValidationError(f"{label} 不是目录：{resolved}")
    return resolved


def build_agy_print_args(
    prompt: str,
    *,
    sandbox: bool = False,
    session_id: str = "",
    continue_last: bool = False,
    model: str = "",
    add_dirs: Sequence[str | Path] | None = None,
    skip_permissions: bool = False,
    print_timeout_seconds: int = DEFAULT_PRINT_TIMEOUT_SECONDS,
    base_dir: str | Path | None = None,
    log_file: str | Path | None = None,
) -> list[str]:
    """构造 agy --print 参数列表。"""
    if not prompt or not prompt.strip():
        raise AgyValidationError("PROMPT 不能为空。")
    if session_id and continue_last:
        raise AgyValidationError("SESSION_ID 与 continue_last 不能同时使用。")
    if print_timeout_seconds <= 0:
        raise AgyValidationError("print_timeout_seconds 必须大于 0。")

    args: list[str] = ["--print-timeout", f"{int(print_timeout_seconds)}s"]

    if sandbox:
        args.append("--sandbox")
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    if model:
        args.extend(["--model", model])
    if log_file:
        args.extend(["--log-file", str(Path(log_file).expanduser())])
    if session_id:
        args.extend(["--conversation", session_id])
    elif continue_last:
        args.append("--continue")

    for directory in add_dirs or ():
        args.extend(["--add-dir", str(_resolve_directory(directory, "add_dirs", base_dir=base_dir))])

    args.extend(["--print", prompt])
    return args


def run_agy_command(
    args: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    extra_env: Mapping[str, str] | None = None,
    binary: str | None = None,
) -> ProcessResult:
    """执行 agy 命令并返回有界输出。"""
    agy_binary = resolve_agy_binary(binary)
    _prewarm_windows_credential_manager()
    command = [agy_binary, *args]
    return run_process(
        command,
        cwd=ensure_directory(cwd, "cwd"),
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        extra_env=extra_env,
    )


def run_process(
    command: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: int,
    max_output_chars: int,
    extra_env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """执行任意子进程；用于测试和 agy 包装。"""
    if timeout_seconds <= 0:
        raise AgyValidationError("timeout_seconds 必须大于 0。")
    if max_output_chars < 1024:
        raise AgyValidationError("max_output_chars 至少为 1024。")

    resolved_cwd = ensure_directory(cwd, "cwd")
    env = os.environ.copy()
    if extra_env:
        env.update(_validate_extra_env(extra_env))

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_state = {"total": 0, "truncated": False}
    stderr_state = {"total": 0, "truncated": False}
    start = time.monotonic()

    process = subprocess.Popen(
        list(command),
        cwd=str(resolved_cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    stdout_thread = threading.Thread(
        target=_read_stream,
        args=(process.stdout, stdout_chunks, stdout_state, max_output_chars),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_read_stream,
        args=(process.stderr, stderr_chunks, stderr_state, max_output_chars),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process)
        returncode = process.returncode

    stdout_thread.join(timeout=2)
    stderr_thread.join(timeout=2)

    duration = time.monotonic() - start
    return ProcessResult(
        command=build_command_preview(command),
        cwd=str(resolved_cwd),
        returncode=returncode,
        stdout=redact_text("".join(stdout_chunks)),
        stderr=redact_text("".join(stderr_chunks)),
        timed_out=timed_out,
        truncated=bool(stdout_state["truncated"] or stderr_state["truncated"]),
        duration_seconds=round(duration, 3),
    )


def build_command_preview(command: Sequence[str]) -> list[str]:
    """生成不会泄漏完整 prompt 的命令预览。"""
    preview: list[str] = []
    skip_next = False
    for index, item in enumerate(command):
        if skip_next:
            skip_next = False
            continue
        if index == 0:
            preview.append(Path(item).name)
            continue
        if item == "--print":
            preview.extend(["--print", "<PROMPT>"])
            skip_next = True
            continue
        preview.append(item)
    return preview


def find_conversation_id(text: str) -> str | None:
    """从 agy 输出中尽力提取会话 ID；不同版本可能不输出该字段。"""
    for pattern in _CONVERSATION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def find_conversation_id_from_file(path: str | Path) -> str:
    """从 agy 日志文件中提取本次调用创建或使用的会话 ID。"""
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return find_conversation_id(content) or ""


def find_auth_failure_from_file(path: str | Path) -> str:
    """从 agy 日志文件中识别认证失败，避免把空输出误判为成功。"""
    try:
        content = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if re.search(r"Print mode:\s*silent auth succeeded|Created conversation", content, re.IGNORECASE):
        return ""
    if not any(pattern.search(content) for pattern in _AUTH_FAILURE_PATTERNS):
        return ""
    if re.search(r"auth timed out|silent auth failed|keyringAuth:\s*timed out", content, re.IGNORECASE):
        return "agy 读取系统登录凭据超时并触发 OAuth，未完成本次调用。请先在独立终端运行一次 agy 完成/预热登录后重试。"
    return "agy 未获得可用的 Antigravity 登录态，请先完成 agy 登录后重试。"


def recover_latest_agent_message(
    *,
    cwd: str | Path,
    session_id: str = "",
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> tuple[str, str]:
    """从 agy 本地 transcript 兜底读取最近一次模型回复。"""
    if max_output_chars < 1024:
        raise AgyValidationError("max_output_chars 至少为 1024。")

    resolved_cwd = ensure_directory(cwd, "cwd")
    conversation_id = session_id or _find_latest_conversation_id(resolved_cwd)
    if not conversation_id:
        return "", ""

    transcript_path = _agy_app_data_dir() / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_path.is_file():
        return conversation_id, ""

    latest_content = ""
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as transcript:
            for line in transcript:
                item = _parse_json_line(line)
                if not item:
                    continue
                if item.get("source") == "MODEL" and item.get("status") == "DONE":
                    content = item.get("content")
                    if isinstance(content, str) and content.strip():
                        latest_content = content.strip()
    except OSError:
        return conversation_id, ""

    return conversation_id, redact_text(latest_content[:max_output_chars])


def build_doctor_report(
    *,
    cwd: str | Path | None = None,
    check_models: bool = True,
    timeout_seconds: int = 30,
) -> dict[str, object]:
    """构造不会触发真实模型调用的诊断报告。"""
    report_cwd = ensure_directory(cwd or Path.cwd(), "cwd")
    checks: list[dict[str, object]] = []
    binary = ""

    try:
        binary = resolve_agy_binary()
        checks.append({"name": "binary", "ok": True, "required": True, "detail": redact_text(binary)})
    except AgyMcpError as error:
        checks.append({"name": "binary", "ok": False, "required": True, "detail": redact_text(str(error))})

    if binary:
        version = run_process(
            [binary, "--version"],
            cwd=report_cwd,
            timeout_seconds=timeout_seconds,
            max_output_chars=4096,
        )
        checks.append(_command_check("version", version, required=True))

        help_result = run_process(
            [binary, "--help"],
            cwd=report_cwd,
            timeout_seconds=timeout_seconds,
            max_output_chars=16_384,
        )
        help_output = f"{help_result.stdout}\n{help_result.stderr}"
        help_ok = help_result.success and "--print" in help_output and "--conversation" in help_output
        checks.append(
            {
                "name": "help",
                "ok": help_ok,
                "required": True,
                "detail": "检测到 --print 与 --conversation" if help_ok else _result_error(help_result),
            }
        )

        if check_models:
            models = run_process(
                [binary, "models"],
                cwd=report_cwd,
                timeout_seconds=timeout_seconds,
                max_output_chars=32_768,
            )
            checks.append(_command_check("models", models, required=False))

    checks.append({"name": "cwd", "ok": True, "required": True, "detail": str(report_cwd)})
    required_checks = [item for item in checks if bool(item.get("required", True))]
    return {
        "healthy": all(bool(item["ok"]) for item in required_checks),
        "optional_healthy": all(bool(item["ok"]) for item in checks),
        "checks": checks,
        "cwd": str(report_cwd),
    }


def _resolve_explicit_binary(value: str) -> str:
    candidate = os.path.expandvars(os.path.expanduser(value))
    path_like = any(separator in candidate for separator in (os.sep, "/", "\\")) or Path(candidate).is_absolute()
    if path_like:
        path = Path(candidate).resolve()
        if path.is_file():
            return str(path)
        raise AgyNotFoundError(f"配置的 agy 路径不存在：{path}")

    discovered = shutil.which(candidate)
    if discovered:
        return discovered
    raise AgyNotFoundError(f"配置的 agy 命令不可执行：{value}")


def _resolve_directory(path: str | Path, label: str, *, base_dir: str | Path | None = None) -> Path:
    raw_path = Path(path).expanduser()
    if base_dir is not None and not raw_path.is_absolute():
        raw_path = Path(base_dir).expanduser() / raw_path
    return ensure_directory(raw_path, label)


def _validate_extra_env(extra_env: Mapping[str, str]) -> dict[str, str]:
    validated: dict[str, str] = {}
    for key, value in extra_env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise AgyValidationError(f"环境变量名不合法：{key}")
        validated[key] = str(value)
    return validated


def _prewarm_windows_credential_manager() -> None:
    """预热 Windows 凭据管理器，降低 agy keyring 读取超时概率。"""
    if os.name != "nt":
        return
    if os.environ.get("AGYMCP_SKIP_KEYRING_PREWARM"):
        return
    try:
        subprocess.run(
            ["cmdkey", "/list"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            shell=False,
        )
    except Exception:
        return


def _agy_app_data_dir() -> Path:
    app_data_dir = os.environ.get("AGY_APP_DATA_DIR")
    if app_data_dir:
        return Path(app_data_dir).expanduser()
    return Path.home() / ".gemini" / "antigravity-cli"


def _find_latest_conversation_id(cwd: Path) -> str:
    cache_path = _agy_app_data_dir() / "cache" / "last_conversations.json"
    if not cache_path.is_file():
        return ""

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""

    cwd_key = os.path.normcase(str(cwd))
    for raw_path, conversation_id in data.items():
        if not isinstance(raw_path, str) or not isinstance(conversation_id, str):
            continue
        try:
            candidate = os.path.normcase(str(Path(raw_path).expanduser().resolve()))
        except OSError:
            candidate = os.path.normcase(raw_path)
        if candidate == cwd_key:
            return conversation_id
    return ""


def _parse_json_line(line: str) -> dict[str, object] | None:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return None
    return item if isinstance(item, dict) else None


def _read_stream(stream: object, chunks: list[str], state: dict[str, int | bool], limit: int) -> None:
    if stream is None:
        return

    while True:
        chunk = stream.read(4096)  # type: ignore[attr-defined]
        if not chunk:
            break
        total = int(state["total"])
        if total < limit:
            remaining = limit - total
            chunks.append(chunk[:remaining])
            if len(chunk) > remaining:
                state["truncated"] = True
        else:
            state["truncated"] = True
        state["total"] = total + len(chunk)

    stream.close()  # type: ignore[attr-defined]


def _terminate_process(process: subprocess.Popen[str]) -> None:
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except Exception:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        finally:
            process.wait(timeout=2)


def _command_check(name: str, result: ProcessResult, *, required: bool) -> dict[str, object]:
    return {
        "name": name,
        "ok": result.success,
        "required": required,
        "detail": result.stdout.strip() if result.success else _result_error(result),
    }


def _result_error(result: ProcessResult) -> str:
    if result.timed_out:
        return f"命令超时：{result.command}"
    message = (result.stderr or result.stdout).strip()
    return message or f"命令退出码：{result.returncode}"

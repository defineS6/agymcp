"""FastMCP server implementation for agymcp."""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from agymcp.core import (
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_PRINT_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    AgyMcpError,
    build_agy_print_args,
    build_doctor_report,
    find_auth_failure_from_file,
    find_conversation_id,
    find_conversation_id_from_file,
    redact_text,
    recover_latest_agent_message,
    run_agy_command,
)

mcp = FastMCP("AGY MCP Server")


@mcp.tool(
    name="agy",
    description="""
    调用 Google Antigravity CLI 的 `agy --print` 执行一次非交互任务。

    返回结构：
    - `success`: 是否执行成功
    - `SESSION_ID`: 传入的会话 ID，或从输出中尽力提取的会话 ID
    - `agent_messages`: agy stdout 文本
    - `stderr`: agy stderr 文本
    - `error`: 失败时的错误摘要

    注意：
    - 这会启动真实 agy 请求，可能消耗额度。
    - `SESSION_ID` 与 `continue_last` 不能同时使用。
    - 只有显式设置 `skip_permissions=True` 才会追加危险权限跳过参数。
    """,
    meta={"version": "0.1.0", "author": "agy-mcp contributors"},
)
async def agy(
    PROMPT: Annotated[str, "发送给 agy 的完整提示词。"],
    cd: Annotated[Path, "agy 的工作目录。"],
    sandbox: Annotated[bool, Field(description="追加 `--sandbox`。")] = False,
    SESSION_ID: Annotated[str, "通过 `--conversation` 续接指定 agy 会话。"] = "",
    continue_last: Annotated[bool, Field(description="通过 `--continue` 续接最近会话。")] = False,
    model: Annotated[str, "通过 `--model` 指定模型。"] = "",
    add_dirs: Annotated[list[Path] | None, "通过 `--add-dir` 追加到 agy workspace 的目录。"] = None,
    skip_permissions: Annotated[
        bool,
        Field(description="追加 `--dangerously-skip-permissions`，默认关闭。"),
    ] = False,
    timeout_seconds: Annotated[int, Field(ge=1, le=7200, description="包装器等待进程结束的超时时间。")] = DEFAULT_TIMEOUT_SECONDS,
    print_timeout_seconds: Annotated[int, Field(ge=1, le=7200, description="传给 agy `--print-timeout` 的秒数。")] = DEFAULT_PRINT_TIMEOUT_SECONDS,
    max_output_chars: Annotated[int, Field(ge=1024, le=2_000_000, description="stdout/stderr 最大保留字符数。")] = DEFAULT_MAX_OUTPUT_CHARS,
    extra_env: Annotated[dict[str, str] | None, "本次调用额外注入的环境变量。"] = None,
) -> dict[str, Any]:
    """执行一次 agy 调用。"""
    return _run_agy_print(
        prompt=PROMPT,
        cd=cd,
        sandbox=sandbox,
        session_id=SESSION_ID,
        continue_last=continue_last,
        model=model,
        add_dirs=add_dirs,
        skip_permissions=skip_permissions,
        timeout_seconds=timeout_seconds,
        print_timeout_seconds=print_timeout_seconds,
        max_output_chars=max_output_chars,
        extra_env=extra_env,
    )


@mcp.tool(
    name="agy_continue",
    description="续接指定 agy 会话；未传 SESSION_ID 时续接最近会话。",
)
async def agy_continue(
    PROMPT: Annotated[str, "发送给 agy 的完整提示词。"],
    cd: Annotated[Path, "agy 的工作目录。"],
    SESSION_ID: Annotated[str, "通过 `--conversation` 续接指定 agy 会话；为空则使用 `--continue`。"] = "",
    sandbox: Annotated[bool, Field(description="追加 `--sandbox`。")] = False,
    model: Annotated[str, "通过 `--model` 指定模型。"] = "",
    timeout_seconds: Annotated[int, Field(ge=1, le=7200)] = DEFAULT_TIMEOUT_SECONDS,
    print_timeout_seconds: Annotated[int, Field(ge=1, le=7200)] = DEFAULT_PRINT_TIMEOUT_SECONDS,
    max_output_chars: Annotated[int, Field(ge=1024, le=2_000_000)] = DEFAULT_MAX_OUTPUT_CHARS,
) -> dict[str, Any]:
    """续接 agy 会话。"""
    return _run_agy_print(
        prompt=PROMPT,
        cd=cd,
        sandbox=sandbox,
        session_id=SESSION_ID,
        continue_last=not bool(SESSION_ID),
        model=model,
        add_dirs=None,
        skip_permissions=False,
        timeout_seconds=timeout_seconds,
        print_timeout_seconds=print_timeout_seconds,
        max_output_chars=max_output_chars,
        extra_env=None,
    )


@mcp.tool(name="agy_models", description="执行 `agy models`，返回本机 agy 可用模型列表。")
async def agy_models(
    cd: Annotated[Path, "执行命令的工作目录。"] = Path("."),
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30,
    max_output_chars: Annotated[int, Field(ge=1024, le=500_000)] = 100_000,
) -> dict[str, Any]:
    """列出 agy 模型。"""
    try:
        result = run_agy_command(
            ["models"],
            cwd=cd,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )
        payload = _process_result_payload(result, session_id="")
        payload["models"] = result.stdout.strip()
        return payload
    except AgyMcpError as error:
        return {"success": False, "error": redact_text(str(error))}


@mcp.tool(name="agy_doctor", description="检查 agy 路径、版本、帮助输出、模型命令和工作目录。")
async def agy_doctor(
    cd: Annotated[Path, "诊断时使用的工作目录。"] = Path("."),
    check_models: Annotated[bool, Field(description="是否执行 `agy models`。")] = True,
    timeout_seconds: Annotated[int, Field(ge=1, le=300)] = 30,
) -> dict[str, Any]:
    """返回 agymcp 环境诊断报告。"""
    try:
        report = build_doctor_report(cwd=cd, check_models=check_models, timeout_seconds=timeout_seconds)
        return {"success": bool(report["healthy"]), **report}
    except AgyMcpError as error:
        return {"success": False, "healthy": False, "error": redact_text(str(error))}


def run() -> None:
    """通过 stdio transport 启动 MCP 服务。"""
    mcp.run(transport="stdio")


def _run_agy_print(
    *,
    prompt: str,
    cd: Path,
    sandbox: bool,
    session_id: str,
    continue_last: bool,
    model: str,
    add_dirs: list[Path] | None,
    skip_permissions: bool,
    timeout_seconds: int,
    print_timeout_seconds: int,
    max_output_chars: int,
    extra_env: dict[str, str] | None,
) -> dict[str, Any]:
    try:
        log_file = Path(tempfile.gettempdir()) / f"agymcp-{uuid.uuid4().hex}.log"
        args = build_agy_print_args(
            prompt,
            sandbox=sandbox,
            session_id=session_id,
            continue_last=continue_last,
            model=model,
            add_dirs=add_dirs,
            skip_permissions=skip_permissions,
            print_timeout_seconds=print_timeout_seconds,
            base_dir=cd,
            log_file=log_file,
        )
        result = run_agy_command(
            args,
            cwd=cd,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            extra_env=extra_env,
        )
        detected_session_id = (
            session_id
            or find_conversation_id(f"{result.stdout}\n{result.stderr}")
            or find_conversation_id_from_file(log_file)
            or ""
        )
        recovered_message = ""
        auth_error = ""
        if result.success and not result.stdout.strip():
            auth_error = find_auth_failure_from_file(log_file)
        if result.success and not result.stdout.strip() and detected_session_id and not auth_error:
            recovered_session_id, recovered_message = recover_latest_agent_message(
                cwd=cd,
                session_id=detected_session_id,
                max_output_chars=max_output_chars,
            )
            detected_session_id = detected_session_id or recovered_session_id
        payload = _process_result_payload(result, session_id=detected_session_id, recovered_message=recovered_message)
        if auth_error:
            payload["success"] = False
            payload["error"] = auth_error
        return payload
    except AgyMcpError as error:
        return {"success": False, "error": redact_text(str(error))}


def _process_result_payload(result: Any, *, session_id: str, recovered_message: str = "") -> dict[str, Any]:
    stdout = result.stdout.strip() or recovered_message.strip()
    stderr = result.stderr.strip()
    payload: dict[str, Any] = {
        "success": result.success,
        "SESSION_ID": session_id,
        "agent_messages": stdout,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "truncated": result.truncated,
        "duration_seconds": result.duration_seconds,
        "command": result.command,
        "cwd": result.cwd,
    }
    if recovered_message:
        payload["message_source"] = "transcript"
    if stderr:
        payload["stderr"] = stderr
    if not result.success:
        if result.timed_out:
            payload["error"] = "agy 调用超时。"
        else:
            payload["error"] = stderr or stdout or f"agy 退出码：{result.returncode}"
    return payload

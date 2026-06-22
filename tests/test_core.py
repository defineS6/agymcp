from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agymcp.core import (
    AgyValidationError,
    DEFAULT_MODEL,
    build_agy_print_args,
    build_command_preview,
    find_auth_failure_from_file,
    find_conversation_id,
    find_conversation_id_from_file,
    run_agy_command,
    recover_latest_agent_message,
    redact_text,
    run_process,
)


def test_build_agy_print_args_contains_expected_flags(tmp_path: Path) -> None:
    args = build_agy_print_args(
        "请检查这个项目",
        sandbox=True,
        session_id="conversation-123456",
        model="gemini-test",
        add_dirs=[tmp_path],
        skip_permissions=True,
        print_timeout_seconds=42,
    )

    assert args == [
        "--print-timeout",
        "42s",
        "--sandbox",
        "--dangerously-skip-permissions",
        "--model",
        "gemini-test",
        "--conversation",
        "conversation-123456",
        "--add-dir",
        str(tmp_path.resolve()),
        "--print",
        "请检查这个项目",
    ]


def test_build_agy_print_args_rejects_conflicting_resume_modes() -> None:
    with pytest.raises(AgyValidationError):
        build_agy_print_args("hello", session_id="conversation-123456", continue_last=True)


def test_build_agy_print_args_resolves_add_dirs_from_base_dir(tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()

    args = build_agy_print_args("hello", add_dirs=[Path("extra")], base_dir=tmp_path)

    assert args == [
        "--print-timeout",
        "300s",
        "--model",
        DEFAULT_MODEL,
        "--add-dir",
        str(extra.resolve()),
        "--print",
        "hello",
    ]


def test_build_agy_print_args_accepts_log_file(tmp_path: Path) -> None:
    log_file = tmp_path / "agy.log"

    args = build_agy_print_args("hello", log_file=log_file)

    assert args == ["--print-timeout", "300s", "--model", DEFAULT_MODEL, "--log-file", str(log_file), "--print", "hello"]


def test_command_preview_hides_prompt() -> None:
    preview = build_command_preview(["agy", "--model", "x", "--print", "secret prompt"])

    assert preview == ["agy", "--model", "x", "--print", "<PROMPT>"]


def test_find_conversation_id() -> None:
    assert find_conversation_id("Conversation ID: abcdef12-3456") == "abcdef12-3456"


def test_find_conversation_id_from_file(tmp_path: Path) -> None:
    log_file = tmp_path / "agy.log"
    log_file.write_text("I server.go:789] Created conversation abcdef12-3456", encoding="utf-8")

    assert find_conversation_id_from_file(log_file) == "abcdef12-3456"


def test_find_auth_failure_from_file_detects_keyring_timeout(tmp_path: Path) -> None:
    log_file = tmp_path / "agy.log"
    log_file.write_text(
        "W keyring.go:92] keyringAuth: timed out after 5s, skipping keyring auth\n"
        "I printmode.go:196] Print mode: silent auth failed, triggering OAuth\n"
        "E printmode.go:244] Print mode: auth timed out",
        encoding="utf-8",
    )

    assert "读取系统登录凭据超时" in find_auth_failure_from_file(log_file)


def test_find_auth_failure_from_file_ignores_early_auth_noise_after_success(tmp_path: Path) -> None:
    log_file = tmp_path / "agy.log"
    log_file.write_text(
        "E log.go:398] error getting token source: You are not logged into Antigravity.\n"
        "I auth.go:132] ChainedAuth: authenticated via keyring (effective: keyring)\n"
        "I printmode.go:192] Print mode: silent auth succeeded\n"
        "I server.go:789] Created conversation abcdef12-3456",
        encoding="utf-8",
    )

    assert find_auth_failure_from_file(log_file) == ""


def test_redact_text_masks_common_sensitive_values() -> None:
    text = "Bearer abc.def.ghi eyJabc.def.ghi AKIA1234567890ABCDEF"
    redacted = redact_text(text)

    assert "Bearer <redacted>" in redacted
    assert "<redacted:jwt>" in redacted
    assert "<redacted:aws-access-key>" in redacted


def test_run_process_success() -> None:
    result = run_process(
        [sys.executable, "-c", "print('ok')"],
        cwd=Path.cwd(),
        timeout_seconds=10,
        max_output_chars=4096,
    )

    assert result.success
    assert result.stdout.strip() == "ok"


def test_run_agy_command_honors_skip_keyring_prewarm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGYMCP_SKIP_KEYRING_PREWARM", "1")

    with pytest.raises(AgyValidationError):
        run_agy_command([], cwd=Path.cwd(), timeout_seconds=0)


def test_recover_latest_agent_message_from_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app_data = tmp_path / "appdata"
    cwd = tmp_path / "workspace"
    conversation_id = "conversation-123456"
    transcript_dir = app_data / "brain" / conversation_id / ".system_generated" / "logs"
    cache_dir = app_data / "cache"
    cwd.mkdir()
    transcript_dir.mkdir(parents=True)
    cache_dir.mkdir(parents=True)
    monkeypatch.setenv("AGY_APP_DATA_DIR", str(app_data))

    (cache_dir / "last_conversations.json").write_text(json.dumps({str(cwd): conversation_id}), encoding="utf-8")
    (transcript_dir / "transcript.jsonl").write_text(
        "\n".join(
            [
                '{"source":"USER_EXPLICIT","type":"USER_INPUT","status":"DONE","content":"hello"}',
                '{"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"ok"}',
            ]
        ),
        encoding="utf-8",
    )

    recovered_session_id, message = recover_latest_agent_message(cwd=cwd)

    assert recovered_session_id == conversation_id
    assert message == "ok"

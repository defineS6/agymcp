from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agymcp.core import (
    AgyValidationError,
    build_agy_print_args,
    build_command_preview,
    find_conversation_id,
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

    assert args[-3:] == [str(extra.resolve()), "--print", "hello"]


def test_command_preview_hides_prompt() -> None:
    preview = build_command_preview(["agy", "--model", "x", "--print", "secret prompt"])

    assert preview == ["agy", "--model", "x", "--print", "<PROMPT>"]


def test_find_conversation_id() -> None:
    assert find_conversation_id("Conversation ID: abcdef12-3456") == "abcdef12-3456"


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

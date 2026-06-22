"""本地诊断命令。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agymcp.core import AgyMcpError, build_doctor_report, redact_text


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="检查 agymcp 运行环境")
    parser.add_argument("--cwd", default=".", help="诊断时使用的工作目录")
    parser.add_argument("--skip-models", action="store_true", help="跳过 agy models 检查")
    parser.add_argument("--timeout", type=int, default=30, help="单条诊断命令超时时间，单位秒")
    args = parser.parse_args(argv)

    try:
        report = build_doctor_report(
            cwd=Path(args.cwd),
            check_models=not args.skip_models,
            timeout_seconds=args.timeout,
        )
    except AgyMcpError as error:
        report = {"healthy": False, "error": redact_text(str(error))}

    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report.get("healthy") else 1)


if __name__ == "__main__":
    main()


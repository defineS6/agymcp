"""agymcp 的控制台入口。"""

from agymcp.server import run


def main() -> None:
    """启动 stdio MCP 服务。"""
    run()


if __name__ == "__main__":
    main()


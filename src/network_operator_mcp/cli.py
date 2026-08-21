from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path

from .config import AppConfig, ConfigError, load_config
from .server import create_server
from .ssh_terminal import SessionError, SSHTerminalManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="network-operator-mcp",
        description="MCP server for operating network devices through multiple backends",
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML device file")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="run the MCP server")
    serve.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default="stdio",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--http-path", default="/mcp")
    serve.add_argument("--json-response", action="store_true")
    subparsers.add_parser("validate-config", help="validate configuration and exit")
    subparsers.add_parser("list-devices", help="list configured devices as JSON")
    probe = subparsers.add_parser("probe", help="open a session, capture, and close")
    probe.add_argument("device")
    probe.add_argument("--quiet-timeout-ms", type=int)
    probe.add_argument("--deadline-ms", type=int)
    return parser


def _json(data: object) -> None:
    print(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            default=lambda value: asdict(value) if is_dataclass(value) else str(value),
        )
    )


async def _probe(
    config: AppConfig,
    device: str,
    quiet_ms: int | None,
    deadline_ms: int | None,
) -> None:
    manager = SSHTerminalManager(config)
    try:
        session, initial = await manager.open(
            device,
            quiet_timeout_ms=quiet_ms,
            deadline_ms=deadline_ms,
        )
        _json(
            {
                "session": session.public_info(),
                "initial_output": initial,
            }
        )
    finally:
        await manager.close_all()


def main() -> None:
    args = build_parser().parse_args()
    command = args.command or "serve"
    try:
        config = load_config(args.config)
        if command == "validate-config":
            _json({"valid": True, "config": str(config.source)})
            return
        if command == "list-devices":
            _json([device.public_info() for device in config.devices.values()])
            return
        if command == "probe":
            asyncio.run(
                _probe(
                    config,
                    args.device,
                    args.quiet_timeout_ms,
                    args.deadline_ms,
                )
            )
            return
        mcp, _ = create_server(
            config,
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8000),
            http_path=getattr(args, "http_path", "/mcp"),
            json_response=getattr(args, "json_response", False),
        )
        mcp.run(transport=getattr(args, "transport", "stdio"))
    except (ConfigError, SessionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

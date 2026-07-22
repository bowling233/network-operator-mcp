from __future__ import annotations

import asyncio

from network_operator_mcp.cli import build_parser
from network_operator_mcp.config import AccountConfig, AppConfig, DeviceConfig
from network_operator_mcp.server import create_server


def test_http_cli_options():
    args = build_parser().parse_args(
        [
            "--config",
            "devices.yaml",
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9000",
            "--http-path",
            "/network/mcp",
            "--json-response",
        ]
    )

    assert args.transport == "streamable-http"
    assert args.host == "0.0.0.0"
    assert args.port == 9000
    assert args.http_path == "/network/mcp"
    assert args.json_response


def test_http_settings_are_only_startup_configuration():
    account = AccountConfig(name="admin", username="admin")
    device = DeviceConfig(
        name="sw1",
        type="ssh-terminal",
        host="192.0.2.1",
        account=account,
    )
    config = AppConfig(accounts={"admin": account}, devices={"sw1": device})

    mcp, manager = create_server(
        config,
        host="0.0.0.0",
        port=9000,
        http_path="/network/mcp",
        json_response=True,
    )

    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 9000
    assert mcp.settings.streamable_http_path == "/network/mcp"
    assert mcp.settings.json_response
    assert manager.config is config


def test_tool_schemas_are_structured():
    async def run() -> None:
        account = AccountConfig(name="admin", username="admin")
        device = DeviceConfig(
            name="sw1",
            type="ssh-terminal",
            host="192.0.2.1",
            account=account,
        )
        mcp, _ = create_server(
            AppConfig(accounts={"admin": account}, devices={"sw1": device})
        )
        tools = {tool.name: tool for tool in await mcp.list_tools()}

        exchange_input = tools["exchange"].inputSchema
        assert exchange_input["required"] == [
            "session_id",
            "request_id",
            "data",
            "cursor",
        ]
        assert exchange_input["properties"]["input_type"]["enum"] == [
            "line",
            "text",
            "key",
        ]
        assert "ctx" not in exchange_input["properties"]

        exchange_output = tools["exchange"].outputSchema
        assert exchange_output["properties"]["write_cursor"]["type"] == "integer"
        assert exchange_output["properties"]["read_stop_reason"]["enum"] == [
            "quiet",
            "deadline",
            "response_limit",
            "eof",
        ]
        assert "command_status" not in exchange_output["properties"]
        assert "may_have_more" not in exchange_output["properties"]

        open_output = tools["open_session"].outputSchema
        session_schema = open_output["$defs"]["SessionInfo"]
        assert "server_version" in session_schema["properties"]

        device_schema = tools["list_devices"].outputSchema["$defs"]["DeviceInfo"]
        assert device_schema["properties"]["type"]["enum"] == [
            "ssh-terminal",
            "ssh-exec",
        ]

        exec_output = tools["ssh_execute"].outputSchema
        assert exec_output["properties"]["exit_status"]["anyOf"] == [
            {"type": "integer"},
            {"type": "null"},
        ]

    asyncio.run(run())

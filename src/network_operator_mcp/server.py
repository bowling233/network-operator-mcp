from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from mcp.server.fastmcp import FastMCP

from .config import AppConfig, DeviceInfo
from .http_backend import HTTPBackendManager, HTTPMethod, HTTPResponseResult
from .ssh_terminal import (
    ExchangeResult,
    InputType,
    ReadResult,
    SessionInfo,
    SSHTerminalManager,
)
from .ssh_exec import SSHExecResult, execute


SERVER_INSTRUCTIONS = """
Devices have a type. ssh-terminal devices expose persistent interactive terminal
sessions. Their output is collected continuously and read through byte cursors;
quiet only means no new bytes arrived during quiet_timeout_ms. Pass
initial_output.next_cursor to the first exchange and carry next_cursor forward.
The server does not parse terminal prompts or infer command success.

ssh-exec devices use a standard SSH exec channel. Use ssh_execute for one command
and inspect stdout, stderr, exit_status, and exit_signal. Start with read-only
commands.

HTTP devices expose their WebUI APIs through http_request. Authentication is
managed by this server. Pass only a device-relative path; start with GET and
other read-only requests while discovering an API.
""".strip()


@dataclass(frozen=True)
class OpenSessionResult:
    session: SessionInfo
    initial_output: ReadResult


@dataclass(frozen=True)
class CloseSessionResult:
    closed: bool
    device: str


def create_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    http_path: str = "/mcp",
    json_response: bool = False,
) -> tuple[FastMCP, SSHTerminalManager]:
    manager = SSHTerminalManager(config)
    http_manager = HTTPBackendManager(config)

    @asynccontextmanager
    async def lifespan(_: FastMCP[Any]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await manager.close_all()
            await http_manager.close_all()

    mcp = FastMCP(
        "network-operator-mcp",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        host=host,
        port=port,
        streamable_http_path=http_path,
        json_response=json_response,
        stateless_http=True,
    )

    @mcp.tool()
    def list_devices() -> list[DeviceInfo]:
        """List device names and backend types available to this MCP server."""
        return [device.public_info() for device in config.devices.values()]

    @mcp.tool()
    async def ssh_execute(
        device: str,
        command: str,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SSHExecResult:
        """Execute one command on an ssh-exec device and return its SSH result."""
        return await execute(
            config,
            device,
            command,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    async def http_request(
        device: str,
        method: HTTPMethod,
        path: str,
        query: dict[str, str | list[str]] | None = None,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        body_base64: str | None = None,
        form: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> HTTPResponseResult:
        """Send an authenticated request to one HTTP device.

        The path must be relative to the selected device, beginning with `/`.
        Cookies, authorization data, and vendor session tokens are managed by
        the server. Supply at most one of body, body_base64, and form. Binary
        responses are returned with body_encoding set to base64.
        """
        return await http_manager.request(
            device,
            method,
            path,
            query=query,
            headers=headers,
            body=body,
            body_base64=body_base64,
            form=form,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    async def open_session(
        device: str,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> OpenSessionResult:
        """Open or reuse a device's SSH terminal and return its initial output."""
        session, initial = await manager.open(
            device,
            quiet_timeout_ms=quiet_timeout_ms,
            deadline_ms=deadline_ms,
            response_limit_bytes=response_limit_bytes,
        )
        return OpenSessionResult(session=session.public_info(), initial_output=initial)

    @mcp.tool()
    async def list_sessions() -> list[SessionInfo]:
        """List open SSH terminals by device."""
        return await manager.list()

    @mcp.tool()
    async def exchange(
        device: str,
        request_id: str,
        data: str,
        cursor: int,
        input_type: InputType = "line",
        expected_outbound_seq: int | None = None,
        force_write: bool = False,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> ExchangeResult:
        """Write one terminal input and read output from the caller's cursor.

        Reusing request_id returns the original result without writing twice.
        Carry next_cursor from each read into the next call. write_cursor marks
        where this write occurred, so earlier bytes in output are late output.
        input_type is line, text, or key. A deadline or response limit makes the
        session unsettled and blocks ordinary writes until more output is read.
        """
        session = await manager.get(device)
        return await session.exchange(
            request_id=request_id,
            data=data,
            cursor=cursor,
            input_type=input_type,
            expected_outbound_seq=expected_outbound_seq,
            force_write=force_write,
            quiet_timeout_ms=quiet_timeout_ms,
            deadline_ms=deadline_ms,
            response_limit_bytes=response_limit_bytes,
        )

    @mcp.tool()
    async def read_session(
        device: str,
        cursor: int,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> ReadResult:
        """Read session output from a byte cursor without consuming it."""
        session = await manager.get(device)
        return await session.read(
            cursor,
            quiet_timeout_ms=quiet_timeout_ms,
            deadline_ms=deadline_ms,
            response_limit_bytes=response_limit_bytes,
        )

    @mcp.tool()
    async def close_session(device: str) -> CloseSessionResult:
        """Close and forget one device's SSH terminal."""
        await manager.close(device)
        return CloseSessionResult(closed=True, device=device)

    return mcp, manager

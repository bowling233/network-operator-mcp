from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

from mcp.server.fastmcp import Context, FastMCP

from .config import AppConfig, DeviceInfo
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
""".strip()


@dataclass(frozen=True)
class OpenSessionResult:
    session: SessionInfo
    initial_output: ReadResult


@dataclass(frozen=True)
class CloseSessionResult:
    closed: bool
    session_id: str


def create_server(
    config: AppConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    http_path: str = "/mcp",
    json_response: bool = False,
) -> tuple[FastMCP, SSHTerminalManager]:
    manager = SSHTerminalManager(config)

    @asynccontextmanager
    async def lifespan(_: FastMCP[Any]) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await manager.close_all()

    mcp = FastMCP(
        "network-operator-mcp",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        host=host,
        port=port,
        streamable_http_path=http_path,
        json_response=json_response,
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
    async def open_session(
        device: str,
        ctx: Context,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> OpenSessionResult:
        """Open an owned SSH session; return initial output and SSH server identity."""
        session, initial = await manager.open(
            device,
            ctx.session,
            quiet_timeout_ms=quiet_timeout_ms,
            deadline_ms=deadline_ms,
            response_limit_bytes=response_limit_bytes,
        )
        return OpenSessionResult(session=session.public_info(), initial_output=initial)

    @mcp.tool()
    async def list_sessions(ctx: Context) -> list[SessionInfo]:
        """List sessions owned by this MCP client."""
        return await manager.list(ctx.session)

    @mcp.tool()
    async def exchange(
        session_id: str,
        request_id: str,
        data: str,
        cursor: int,
        ctx: Context,
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
        session = await manager.get(session_id, ctx.session)
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
        session_id: str,
        cursor: int,
        ctx: Context,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> ReadResult:
        """Read session output from a byte cursor without consuming it."""
        session = await manager.get(session_id, ctx.session)
        return await session.read(
            cursor,
            quiet_timeout_ms=quiet_timeout_ms,
            deadline_ms=deadline_ms,
            response_limit_bytes=response_limit_bytes,
        )

    @mcp.tool()
    async def close_session(session_id: str, ctx: Context) -> CloseSessionResult:
        """Close and forget one SSH session."""
        await manager.close(session_id, ctx.session)
        return CloseSessionResult(closed=True, session_id=session_id)

    return mcp, manager

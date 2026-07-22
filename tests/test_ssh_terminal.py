from __future__ import annotations

import asyncio

import pytest

from network_operator_mcp.config import (
    AccountConfig,
    AppConfig,
    BackendsConfig,
    DeviceConfig,
    SSHTerminalBackendConfig,
)
from network_operator_mcp.ssh_terminal import (
    SSHTerminalSession,
    SessionError,
    SSHTerminalManager,
)


class FakeReader:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def read(self, _: int) -> bytes:
        return await self.queue.get()

    def feed(self, data: bytes) -> None:
        self.queue.put_nowait(data)


class FakeWriter:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process

    def write(self, data: bytes) -> None:
        self.process.writes.append(data)
        if data == b"display version\r":
            self.process.stdout.feed(b"display version\r\nVRP V200R025\r\n<MOCK>")
        elif data == b"\x03":
            self.process.stdout.feed(b"^C\r\n<MOCK>")
        else:
            self.process.stdout.feed(data + b"\r\n<MOCK>")

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = FakeReader()
        self.stdin = FakeWriter(self)
        self.writes: list[bytes] = []

    def close(self) -> None:
        self.stdout.feed(b"")


class FakeConnection:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.process.stdout.feed(b"<MOCK>")

    async def create_process(self, **_: object) -> FakeProcess:
        return self.process

    def get_extra_info(self, name: str) -> str | None:
        if name == "server_version":
            return "SSH-2.0-MockSSH_1.0"
        return None

    def close(self) -> None:
        self.process.close()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


def make_config() -> AppConfig:
    terminal = SSHTerminalBackendConfig(
        default_quiet_timeout_ms=20,
        default_deadline_ms=200,
        default_response_limit_bytes=10_000,
    )
    account = AccountConfig(name="operator", username="operator")
    device = DeviceConfig(
        name="mock",
        type="ssh-terminal",
        host="mock.invalid",
        account=account,
    )
    return AppConfig(
        backends=BackendsConfig(ssh_terminal=terminal),
        accounts={"operator": account},
        devices={"mock": device},
    )


async def open_session(monkeypatch) -> tuple[SSHTerminalSession, FakeConnection]:
    connection = FakeConnection()

    async def fake_connect(*_: object, **__: object) -> FakeConnection:
        return connection

    monkeypatch.setattr(
        "network_operator_mcp.ssh_terminal.asyncssh.connect", fake_connect
    )
    session = SSHTerminalSession(
        "session-1",
        make_config().devices["mock"],
        make_config(),
        object(),
    )
    await session.connect()
    return session, connection


def test_interactive_exchange_uses_cursor_and_is_idempotent(monkeypatch):
    async def run() -> None:
        session, connection = await open_session(monkeypatch)
        try:
            initial = await session.read(0)
            assert initial.output == "<MOCK>"
            assert initial.read_stop_reason == "quiet"
            assert session.public_info().server_version == "SSH-2.0-MockSSH_1.0"

            result = await session.exchange(
                request_id="request-1",
                data="display version",
                cursor=initial.next_cursor,
                expected_outbound_seq=0,
            )
            duplicate = await session.exchange(
                request_id="request-1",
                data="this must not be sent",
                cursor=0,
            )

            assert "VRP V200R025" in result.output
            assert duplicate == result
            assert connection.process.writes == [b"display version\r"]

            replay = await session.read(result.from_cursor)
            assert replay.output == result.output
        finally:
            await session.close()

    asyncio.run(run())


def test_deadline_marks_session_unsettled(monkeypatch):
    async def run() -> None:
        session, connection = await open_session(monkeypatch)
        try:
            await session.read(0)
            result = await session.read(session.cursor, deadline_ms=30)
            assert result.output == ""
            assert result.read_stop_reason == "deadline"
            assert session.unsettled

            with pytest.raises(SessionError, match="stream became quiet"):
                await session.exchange(
                    request_id="request-2",
                    data="display version",
                    cursor=result.next_cursor,
                )

            interrupted = await session.exchange(
                request_id="request-3",
                data="CTRL_C",
                cursor=result.next_cursor,
                input_type="key",
            )
            assert "^C" in interrupted.output
            assert connection.process.writes == [b"\x03"]
        finally:
            await session.close()

    asyncio.run(run())


def test_late_output_is_included_from_caller_cursor(monkeypatch):
    async def run() -> None:
        session, connection = await open_session(monkeypatch)
        try:
            initial = await session.read(0)
            connection.process.stdout.feed(b"late output\r\n<MOCK>")
            await asyncio.sleep(0)

            result = await session.exchange(
                request_id="request-late",
                data="display version",
                cursor=initial.next_cursor,
            )

            assert result.from_cursor == initial.next_cursor
            assert result.write_cursor >= initial.next_cursor
            assert "late output" in result.output
            assert "VRP V200R025" in result.output
        finally:
            await session.close()

    asyncio.run(run())


def test_manager_enforces_owner_and_target_occupancy(monkeypatch):
    async def run() -> None:
        connections: list[FakeConnection] = []

        async def fake_connect(*_: object, **__: object) -> FakeConnection:
            connection = FakeConnection()
            connections.append(connection)
            return connection

        monkeypatch.setattr(
            "network_operator_mcp.ssh_terminal.asyncssh.connect", fake_connect
        )
        manager = SSHTerminalManager(make_config())
        owner_a = object()
        owner_b = object()
        try:
            session, _ = await manager.open(
                "mock", owner_a, quiet_timeout_ms=20, deadline_ms=200
            )

            with pytest.raises(SessionError, match="this MCP client"):
                await manager.open("mock", owner_a)
            with pytest.raises(SessionError, match="another MCP client"):
                await manager.open("mock", owner_b)
            with pytest.raises(SessionError, match="another MCP client"):
                await manager.get(session.id, owner_b)
            with pytest.raises(SessionError, match="another MCP client"):
                await manager.close(session.id, owner_b)

            assert len(await manager.list(owner_a)) == 1
            assert await manager.list(owner_b) == []

            await manager.close(session.id, owner_a)
            replacement, _ = await manager.open(
                "mock", owner_b, quiet_timeout_ms=20, deadline_ms=200
            )
            assert replacement.owner is owner_b
        finally:
            await manager.close_all()

    asyncio.run(run())


def test_read_rejects_invalid_cursor(monkeypatch):
    async def run() -> None:
        session, _ = await open_session(monkeypatch)
        try:
            with pytest.raises(SessionError, match="cannot be negative"):
                await session.read(-1)
            with pytest.raises(SessionError, match="ahead of session cursor"):
                await session.read(session.cursor + 1)
        finally:
            await session.close()

    asyncio.run(run())


def test_response_limit_can_be_continued_with_cursor(monkeypatch):
    async def run() -> None:
        session, connection = await open_session(monkeypatch)
        try:
            initial = await session.read(0)
            connection.process.stdout.feed(b"0123456789")
            first = await session.read(initial.next_cursor, response_limit_bytes=4)
            second = await session.read(first.next_cursor, response_limit_bytes=20)

            assert first.output == "0123"
            assert first.read_stop_reason == "response_limit"
            assert second.output == "456789"
            assert second.read_stop_reason == "quiet"
        finally:
            await session.close()

    asyncio.run(run())

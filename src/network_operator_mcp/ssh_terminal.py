from __future__ import annotations

import asyncio
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import asyncssh

from .config import AppConfig, DeviceConfig


class SessionError(RuntimeError):
    """Raised when an SSH session cannot perform an operation."""


KEYS = {
    "ENTER": b"\r",
    "SPACE": b" ",
    "CTRL_C": b"\x03",
    "CTRL_Z": b"\x1a",
    "Q": b"q",
}

InputType = Literal["line", "text", "key"]
ReadStopReason = Literal["quiet", "deadline", "response_limit", "eof"]
ConnectionState = Literal["open", "closed"]


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    device: str
    connection_state: ConnectionState
    server_version: str | None
    opened_at: float
    last_activity_at: float
    cursor: int
    outbound_seq: int
    unsettled: bool


@dataclass(frozen=True)
class ReadResult:
    output: str
    from_cursor: int
    next_cursor: int
    read_stop_reason: ReadStopReason
    elapsed_ms: int
    connection_state: ConnectionState


@dataclass(frozen=True)
class ExchangeResult:
    output: str
    from_cursor: int
    write_cursor: int
    next_cursor: int
    read_stop_reason: ReadStopReason
    elapsed_ms: int
    connection_state: ConnectionState
    request_id: str
    outbound_seq: int


class SSHTerminalSession:
    """One interactive SSH channel with an append-only output transcript."""

    def __init__(
        self,
        session_id: str,
        device: DeviceConfig,
        config: AppConfig,
        owner: object,
    ) -> None:
        self.id = session_id
        self.device = device
        self.config = config
        self.owner = owner
        self.connection: Any = None
        self.process: Any = None
        self.opened_at = time.time()
        self.last_activity_at = self.opened_at
        self._opened_monotonic = time.monotonic()
        self._last_activity_monotonic = self._opened_monotonic
        self._transcript = tempfile.TemporaryFile()
        self._cursor = 0
        self._last_output_at = self._opened_monotonic
        self._condition = asyncio.Condition()
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._requests: dict[str, asyncio.Task[ExchangeResult]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._eof = False
        self._closed = False
        self.unsettled = False
        self.outbound_seq = 0
        self.server_version: str | None = None

    @property
    def connected(self) -> bool:
        return not self._closed and not self._eof and self.process is not None

    @property
    def cursor(self) -> int:
        return self._cursor

    def expired(self, now: float) -> bool:
        server = self.config.backends.ssh_terminal
        return (
            now - self._last_activity_monotonic >= server.session_idle_ttl_seconds
            or now - self._opened_monotonic >= server.max_session_lifetime_seconds
        )

    async def connect(self) -> None:
        account = self.device.account
        if account.private_key:
            client_keys = [
                asyncssh.import_private_key(
                    account.private_key,
                    account.private_key_passphrase,
                )
            ]
        else:
            client_keys = []
        try:
            self.connection = await asyncssh.connect(
                self.device.host,
                port=self.device.port,
                username=account.username,
                password=account.password,
                client_keys=client_keys,
                known_hosts=None,
                config=None,
                connect_timeout=self.config.backends.ssh_terminal.connect_timeout_seconds,
                keepalive_interval=15,
                keepalive_count_max=2,
            )
            self.server_version = self.connection.get_extra_info("server_version")
            self.process = await self.connection.create_process(
                term_type="vt100",
                term_size=(4096, 200),
                encoding=None,
                stderr=asyncssh.STDOUT,
            )
        except (OSError, asyncssh.Error) as exc:
            await self.close()
            raise SessionError(f"cannot connect to {self.device.name}: {exc}") from exc

        self._reader_task = asyncio.create_task(
            self._read_output(), name=f"ssh-reader-{self.id}"
        )

    async def _read_output(self) -> None:
        try:
            while True:
                chunk = await self.process.stdout.read(65536)
                if not chunk:
                    break
                now = time.monotonic()
                async with self._condition:
                    os.write(self._transcript.fileno(), chunk)
                    self._cursor += len(chunk)
                    self._last_output_at = now
                    self._touch(now)
                    self._condition.notify_all()
        finally:
            async with self._condition:
                self._eof = True
                self._condition.notify_all()

    def _touch(self, now: float | None = None) -> None:
        self._last_activity_monotonic = now or time.monotonic()
        self.last_activity_at = time.time()

    async def read(
        self,
        cursor: int,
        *,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> ReadResult:
        if cursor < 0:
            raise SessionError("cursor cannot be negative")
        if cursor > self.cursor:
            raise SessionError(
                f"cursor {cursor} is ahead of session cursor {self.cursor}"
            )

        server = self.config.backends.ssh_terminal
        quiet_ms = (
            server.default_quiet_timeout_ms
            if quiet_timeout_ms is None
            else quiet_timeout_ms
        )
        hard_ms = server.default_deadline_ms if deadline_ms is None else deadline_ms
        limit = (
            server.default_response_limit_bytes
            if response_limit_bytes is None
            else response_limit_bytes
        )
        started = time.monotonic()
        deadline = started + hard_ms / 1000

        async with self._condition:
            while True:
                now = time.monotonic()
                available = self._cursor - cursor

                if available >= limit:
                    reason = "response_limit"
                    break
                if self._eof:
                    reason = "eof"
                    break
                if available > 0 and now - self._last_output_at >= quiet_ms / 1000:
                    reason = "quiet"
                    break
                if now >= deadline:
                    reason = "deadline"
                    break

                if available > 0:
                    quiet_left = self._last_output_at + quiet_ms / 1000 - now
                    wait = min(deadline - now, quiet_left)
                else:
                    wait = deadline - now

                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=max(0, wait))
                except TimeoutError:
                    pass

            end = min(self._cursor, cursor + limit)
            data = os.pread(self._transcript.fileno(), end - cursor, cursor)

        self._touch()
        self.unsettled = reason not in {"quiet", "eof"}
        return ReadResult(
            output=data.decode(self.device.encoding, errors="replace"),
            from_cursor=cursor,
            next_cursor=end,
            read_stop_reason=reason,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            connection_state="open" if self.connected else "closed",
        )

    async def exchange(
        self,
        *,
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
        async with self._request_lock:
            task = self._requests.get(request_id)
            if task is None:
                task = asyncio.create_task(
                    self._exchange_once(
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
                )
                self._requests[request_id] = task
        return await asyncio.shield(task)

    async def _exchange_once(
        self,
        *,
        request_id: str,
        data: str,
        cursor: int,
        input_type: InputType,
        expected_outbound_seq: int | None,
        force_write: bool,
        quiet_timeout_ms: int | None,
        deadline_ms: int | None,
        response_limit_bytes: int | None,
    ) -> ExchangeResult:
        payload = self._encode_input(data, input_type)
        can_interrupt = input_type == "key" and data.upper() == "CTRL_C"

        async with self._write_lock:
            if not self.connected:
                raise SessionError(f"session {self.id} is closed")
            if (
                expected_outbound_seq is not None
                and expected_outbound_seq != self.outbound_seq
            ):
                raise SessionError(
                    f"outbound sequence is {self.outbound_seq}, expected {expected_outbound_seq}"
                )
            if self.unsettled and not force_write and not can_interrupt:
                raise SessionError(
                    "previous read ended before the stream became quiet; "
                    "read again, interrupt, or set force_write"
                )
            if cursor > self.cursor:
                raise SessionError(
                    f"cursor {cursor} is ahead of session cursor {self.cursor}"
                )

            write_cursor = self.cursor
            self.outbound_seq += 1
            outbound_seq = self.outbound_seq
            self.unsettled = True
            self.process.stdin.write(payload)
            await self.process.stdin.drain()
            self._touch()

        result = await self.read(
            cursor,
            quiet_timeout_ms=quiet_timeout_ms,
            deadline_ms=deadline_ms,
            response_limit_bytes=response_limit_bytes,
        )
        return ExchangeResult(
            output=result.output,
            from_cursor=result.from_cursor,
            write_cursor=write_cursor,
            next_cursor=result.next_cursor,
            read_stop_reason=result.read_stop_reason,
            elapsed_ms=result.elapsed_ms,
            connection_state=result.connection_state,
            request_id=request_id,
            outbound_seq=outbound_seq,
        )

    def _encode_input(self, data: str, input_type: InputType) -> bytes:
        if input_type == "line":
            return data.encode(self.device.encoding) + b"\r"
        if input_type == "text":
            return data.encode(self.device.encoding)
        if input_type == "key":
            try:
                return KEYS[data.upper()]
            except KeyError as exc:
                raise SessionError(f"unknown key: {data}") from exc
        raise SessionError(f"unknown input_type: {input_type}")

    def public_info(self) -> SessionInfo:
        return SessionInfo(
            session_id=self.id,
            device=self.device.name,
            connection_state="open" if self.connected else "closed",
            server_version=self.server_version,
            opened_at=self.opened_at,
            last_activity_at=self.last_activity_at,
            cursor=self.cursor,
            outbound_seq=self.outbound_seq,
            unsettled=self.unsettled,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.process is not None:
            self.process.close()
        if self.connection is not None:
            self.connection.close()
            await self.connection.wait_closed()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
        async with self._condition:
            self._eof = True
            self._condition.notify_all()
        self._transcript.close()


class SSHTerminalManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._sessions: dict[str, SSHTerminalSession] = {}
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None

    async def open(
        self,
        device_name: str,
        owner: object,
        *,
        quiet_timeout_ms: int | None = None,
        deadline_ms: int | None = None,
        response_limit_bytes: int | None = None,
    ) -> tuple[SSHTerminalSession, ReadResult]:
        try:
            device = self.config.devices[device_name]
        except KeyError as exc:
            raise SessionError(f"unknown device: {device_name}") from exc
        if device.type != "ssh-terminal":
            raise SessionError(
                f"device {device_name} has type {device.type}, expected ssh-terminal"
            )

        async with self._lock:
            if len(self._sessions) >= self.config.backends.ssh_terminal.max_sessions:
                raise SessionError("maximum number of SSH sessions reached")
            occupied = next(
                (
                    session
                    for session in self._sessions.values()
                    if session.device.name == device_name
                ),
                None,
            )
            if occupied is not None:
                if occupied.owner is owner:
                    raise SessionError(
                        f"device already has a session in this MCP client: {device_name}; "
                        f"reuse or close session {occupied.id}"
                    )
                raise SessionError(
                    f"device is occupied by another MCP client: {device_name}; "
                    "wait for its session to close"
                )
            session = SSHTerminalSession(str(uuid.uuid4()), device, self.config, owner)
            self._sessions[session.id] = session

        try:
            await session.connect()
            initial = await session.read(
                0,
                quiet_timeout_ms=quiet_timeout_ms,
                deadline_ms=deadline_ms,
                response_limit_bytes=response_limit_bytes,
            )
        except Exception:
            await session.close()
            async with self._lock:
                self._sessions.pop(session.id, None)
            raise

        self._ensure_reaper()
        return session, initial

    async def get(self, session_id: str, owner: object) -> SSHTerminalSession:
        async with self._lock:
            try:
                session = self._sessions[session_id]
            except KeyError as exc:
                raise SessionError(f"unknown session: {session_id}") from exc
            if session.owner is not owner:
                raise SessionError(
                    "session is owned by another MCP client; wait for it to close"
                )
            return session

    async def list(self, owner: object) -> list[SessionInfo]:
        async with self._lock:
            return [
                session.public_info()
                for session in self._sessions.values()
                if session.owner is owner
            ]

    async def close(self, session_id: str, owner: object) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError(f"unknown session: {session_id}")
        if session.owner is not owner:
            raise SessionError(
                "session is owned by another MCP client; wait for it to close"
            )
        await session.close()
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def close_all(self) -> int:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        return len(sessions)

    def _ensure_reaper(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(
                self._reap_loop(), name="ssh-session-reaper"
            )

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            async with self._lock:
                expired = [
                    session
                    for session in self._sessions.values()
                    if not session.connected or session.expired(now)
                ]
            for session in expired:
                await session.close()
                async with self._lock:
                    self._sessions.pop(session.id, None)

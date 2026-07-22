from __future__ import annotations

import time
from dataclasses import dataclass

import asyncssh

from .config import AppConfig


class SSHExecError(RuntimeError):
    """Raised when an SSH exec request cannot be completed."""


@dataclass(frozen=True)
class SSHExecResult:
    device: str
    server_version: str | None
    stdout: str
    stderr: str
    exit_status: int | None
    exit_signal: str | None
    elapsed_ms: int


async def execute(
    config: AppConfig,
    device_name: str,
    command: str,
    *,
    stdin: str | None = None,
    timeout_seconds: float | None = None,
) -> SSHExecResult:
    try:
        device = config.devices[device_name]
    except KeyError as exc:
        raise SSHExecError(f"unknown device: {device_name}") from exc
    if device.type != "ssh-exec":
        raise SSHExecError(
            f"device {device_name} has type {device.type}, expected ssh-exec"
        )

    account = device.account
    if account.private_key:
        client_keys = [
            asyncssh.import_private_key(
                account.private_key,
                account.private_key_passphrase,
            )
        ]
    else:
        client_keys = []

    backend = config.backends.ssh_exec
    command_timeout = (
        backend.default_command_timeout_seconds
        if timeout_seconds is None
        else timeout_seconds
    )
    started = time.monotonic()
    connection = None
    try:
        connection = await asyncssh.connect(
            device.host,
            port=device.port,
            username=account.username,
            password=account.password,
            client_keys=client_keys,
            known_hosts=None,
            config=None,
            connect_timeout=backend.connect_timeout_seconds,
        )
        server_version = connection.get_extra_info("server_version")
        result = await connection.run(
            command,
            input=stdin,
            encoding=device.encoding,
            check=False,
            timeout=command_timeout,
        )
    except TimeoutError as exc:
        raise SSHExecError(
            f"command timed out on {device_name} after {command_timeout} seconds"
        ) from exc
    except (OSError, asyncssh.Error) as exc:
        raise SSHExecError(f"SSH exec failed on {device_name}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
            await connection.wait_closed()

    return SSHExecResult(
        device=device_name,
        server_version=server_version,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        exit_status=result.exit_status,
        exit_signal=result.exit_signal[0] if result.exit_signal else None,
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )

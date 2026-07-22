from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml


class ConfigError(ValueError):
    """Raised when the configuration cannot be loaded."""


DeviceType = Literal["ssh-terminal", "ssh-exec"]
DEVICE_TYPES = {"ssh-terminal", "ssh-exec"}


@dataclass(frozen=True)
class AccountConfig:
    name: str
    username: str
    password: str | None = None
    private_key: str | None = None
    private_key_passphrase: str | None = None


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    type: DeviceType


@dataclass(frozen=True)
class DeviceConfig:
    name: str
    type: DeviceType
    host: str
    account: AccountConfig
    port: int = 22
    encoding: str = "utf-8"

    def public_info(self) -> DeviceInfo:
        return DeviceInfo(name=self.name, type=self.type)


@dataclass(frozen=True)
class SSHTerminalBackendConfig:
    connect_timeout_seconds: float = 15.0
    default_quiet_timeout_ms: int = 1000
    default_deadline_ms: int = 15_000
    default_response_limit_bytes: int = 200_000
    max_sessions: int = 10
    session_idle_ttl_seconds: float = 600.0
    max_session_lifetime_seconds: float = 3600.0


@dataclass(frozen=True)
class SSHExecBackendConfig:
    connect_timeout_seconds: float = 15.0
    default_command_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class BackendsConfig:
    ssh_terminal: SSHTerminalBackendConfig = field(
        default_factory=SSHTerminalBackendConfig
    )
    ssh_exec: SSHExecBackendConfig = field(default_factory=SSHExecBackendConfig)


@dataclass(frozen=True)
class AppConfig:
    backends: BackendsConfig = field(default_factory=BackendsConfig)
    accounts: dict[str, AccountConfig] = field(default_factory=dict)
    devices: dict[str, DeviceConfig] = field(default_factory=dict)
    source: Path | None = None


def load_config(path: str | Path) -> AppConfig:
    source = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
        backend_values = raw.get("backends") or {}
        backends = BackendsConfig(
            ssh_terminal=SSHTerminalBackendConfig(
                **(backend_values.get("ssh-terminal") or {})
            ),
            ssh_exec=SSHExecBackendConfig(**(backend_values.get("ssh-exec") or {})),
        )
        accounts = {
            name: AccountConfig(name=name, **values)
            for name, values in (raw.get("accounts") or {}).items()
        }
        devices = _load_devices(raw.get("devices") or {}, accounts)
    except (OSError, TypeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load configuration {source}: {exc}") from exc

    if not devices:
        raise ConfigError("configuration contains no devices")

    return AppConfig(
        backends=backends,
        accounts=accounts,
        devices=devices,
        source=source,
    )


def _load_devices(
    values: dict[str, dict[str, object]],
    accounts: dict[str, AccountConfig],
) -> dict[str, DeviceConfig]:
    devices: dict[str, DeviceConfig] = {}
    for name, device_values in values.items():
        fields = dict(device_values)
        device_type = fields.pop("type", None)
        if device_type not in DEVICE_TYPES:
            raise ConfigError(f"unknown device type for {name}: {device_type}")

        account_name = fields.pop("account", None)
        try:
            account = accounts[str(account_name)]
        except KeyError as exc:
            raise ConfigError(f"unknown account for {name}: {account_name}") from exc

        devices[name] = DeviceConfig(
            name=name,
            type=device_type,
            account=account,
            **fields,
        )
    return devices

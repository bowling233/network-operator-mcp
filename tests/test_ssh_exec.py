from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from network_operator_mcp.config import (
    AccountConfig,
    AppConfig,
    BackendsConfig,
    DeviceConfig,
    SSHExecBackendConfig,
)
from network_operator_mcp.ssh_exec import SSHExecError, execute


def make_config(device_type="ssh-exec") -> AppConfig:
    account = AccountConfig(
        name="key-user",
        username="operator",
        private_key="inline-key",
    )
    device = DeviceConfig(
        name="router",
        type=device_type,
        host="router.invalid",
        account=account,
    )
    return AppConfig(
        backends=BackendsConfig(
            ssh_exec=SSHExecBackendConfig(default_command_timeout_seconds=12)
        ),
        accounts={account.name: account},
        devices={device.name: device},
    )


def test_exec_returns_standard_ssh_result(monkeypatch):
    async def run() -> None:
        calls = {}

        class FakeConnection:
            def get_extra_info(self, name):
                assert name == "server_version"
                return "SSH-2.0-Test"

            async def run(self, command, **kwargs):
                calls["run"] = (command, kwargs)
                return SimpleNamespace(
                    stdout="output\n",
                    stderr="warning\n",
                    exit_status=7,
                    exit_signal=None,
                )

            def close(self):
                calls["closed"] = True

            async def wait_closed(self):
                pass

        async def fake_connect(*args, **kwargs):
            calls["connect"] = (args, kwargs)
            return FakeConnection()

        monkeypatch.setattr(
            "network_operator_mcp.ssh_exec.asyncssh.import_private_key",
            lambda key, passphrase: (key, passphrase),
        )
        monkeypatch.setattr(
            "network_operator_mcp.ssh_exec.asyncssh.connect", fake_connect
        )

        result = await execute(make_config(), "router", "show version", stdin="yes\n")

        assert result.stdout == "output\n"
        assert result.stderr == "warning\n"
        assert result.exit_status == 7
        assert result.server_version == "SSH-2.0-Test"
        assert calls["connect"][1]["username"] == "operator"
        assert calls["connect"][1]["client_keys"] == [("inline-key", None)]
        assert calls["run"][0] == "show version"
        assert calls["run"][1]["input"] == "yes\n"
        assert calls["run"][1]["timeout"] == 12
        assert calls["closed"]

    asyncio.run(run())


def test_exec_rejects_terminal_device():
    with pytest.raises(SSHExecError, match="expected ssh-exec"):
        asyncio.run(execute(make_config("ssh-terminal"), "router", "show version"))

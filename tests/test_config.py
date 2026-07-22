from __future__ import annotations

import os

import pytest

from network_operator_mcp.config import ConfigError, load_config


def test_loads_shared_account_and_only_publishes_name_and_type(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text(
        """
accounts:
  operators:
    username: admin
    password: secret
devices:
  sw1:
    type: ssh-terminal
    host: 192.0.2.1
    account: operators
  router1:
    type: ssh-exec
    host: 192.0.2.2
    account: operators
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o644)

    config = load_config(path)

    assert config.devices["sw1"].account is config.accounts["operators"]
    assert config.devices["router1"].account is config.accounts["operators"]
    assert config.accounts["operators"].password == "secret"
    assert config.devices["sw1"].public_info().name == "sw1"
    assert config.devices["sw1"].public_info().type == "ssh-terminal"


def test_loads_private_key_and_backend_settings(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text(
        """
backends:
  ssh-terminal:
    default_quiet_timeout_ms: 250
  ssh-exec:
    default_command_timeout_seconds: 12
accounts:
  key-user:
    username: admin
    private_key: inline-private-key
devices:
  router1:
    type: ssh-exec
    host: 192.0.2.1
    account: key-user
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.backends.ssh_terminal.default_quiet_timeout_ms == 250
    assert config.backends.ssh_exec.default_command_timeout_seconds == 12
    assert config.accounts["key-user"].private_key == "inline-private-key"


@pytest.mark.parametrize(
    ("device", "message"),
    [
        (
            "type: telnet-terminal\nhost: 192.0.2.1\naccount: admin",
            "unknown device type",
        ),
        (
            "type: ssh-exec\nhost: 192.0.2.1\naccount: missing",
            "unknown account",
        ),
    ],
)
def test_rejects_unknown_type_or_account(tmp_path, device, message):
    path = tmp_path / "devices.yaml"
    path.write_text(
        f"""
accounts:
  admin:
    username: admin
    password: secret
devices:
  router1:
    {device.replace(chr(10), chr(10) + "    ")}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_config(path)

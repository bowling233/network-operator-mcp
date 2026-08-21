# Network Operator MCP

`network-operator-mcp` lets an agent operate network-device management
interfaces while keeping device credentials inside the MCP server.

The server supports these device backends:

- `ssh-terminal`: a persistent SSH PTY for interactive network CLIs.
- `ssh-exec`: one standard SSH exec request per tool call.
- `http-tplink-switch`: TP-Link switch WebUI APIs.
- `http-zte-be7200`: ZTE BE7200 Pro+ WebUI APIs.
- `http-mellanox-onyx`: Mellanox Onyx WebUI APIs.

The server does not interpret commands, prompts, or API responses. It transports
requests and responses between the agent and the selected device.

## Tested devices

| Vendor and model | Software | Backend |
|---|---|---|
| Huawei S1730S-S48T4X-A1 | VRP 5.170 (V200R022C00SPC500) | `ssh-terminal` |
| Huawei S5720-28P-LI-AC | VRP 5.170 (V200R011C10SPC600) | `ssh-terminal` |
| Huawei S5720S-52P-LI-AC | VRP 5.170 (V200R011C10SPC600) | `ssh-terminal` |
| Huawei FutureMatrix S6720S-S24S28X-A | VRP 5.170 (V200R022C00SPC500) | `ssh-terminal` |
| MikroTik CCR2004-1G-12S+2XS (r3) | RouterOS 7.23.1 stable | `ssh-exec` |
| OpenWrt and ImmortalWrt devices | Various | `ssh-exec` |
| TP-Link TL-SG2226 | 2023 WebUI | `http-tplink-switch` |
| TP-Link TL-SG2024D | 2023 WebUI | `http-tplink-switch` |
| TP-Link TL-SE2206 | 2024 WebUI | `http-tplink-switch` |
| ZTE BE7200 Pro+ | V1.0.0.4B8.8000 | `http-zte-be7200` |
| Mellanox SN2700 | Onyx 3.7.1134 | `http-mellanox-onyx` |

## Configuration

```yaml
backends:
  ssh-terminal:
    connect_timeout_seconds: 15
    default_quiet_timeout_ms: 1000
    default_deadline_ms: 15000
    default_response_limit_bytes: 200000
    max_sessions: 10
    session_idle_ttl_seconds: 600
    max_session_lifetime_seconds: 3600
  ssh-exec:
    connect_timeout_seconds: 15
    default_command_timeout_seconds: 60
  http:
    connect_timeout_seconds: 10
    default_request_timeout_seconds: 30
    max_response_bytes: 2000000

accounts:
  ssh-operator:
    username: netadmin
    password: plaintext-password

  web-operator:
    username: webadmin
    password: plaintext-password

  zte-password:
    password: plaintext-password

devices:
  example-huawei:
    type: ssh-terminal
    host: 192.0.2.10
    account: ssh-operator

  example-tplink:
    type: http-tplink-switch
    host: 192.0.2.20
    account: web-operator

  example-zte:
    type: http-zte-be7200
    host: 192.0.2.30
    account: zte-password

  example-onyx:
    type: http-mellanox-onyx
    host: 192.0.2.40
    account: web-operator
    verify_tls: false
```

Accounts can be shared by multiple devices. SSH accounts may use `password`, an
inline `private_key`, and an optional `private_key_passphrase`. The ZTE backend
accepts a password-only account because that product has a fixed WebUI username.

HTTP defaults to port 80, except for `http-mellanox-onyx`, which defaults to
HTTPS port 443. Set `scheme`, `port`, and `verify_tls` on a device when its setup
differs from those defaults. The complete placeholder configuration is in
[`config/devices.example.yaml`](config/devices.example.yaml).

## MCP tools

| Tool | Device backend | Purpose |
|---|---|---|
| `list_devices` | All | List configured device names and backend types. |
| `ssh_execute` | `ssh-exec` | Run one SSH exec request. |
| `open_session` | `ssh-terminal` | Open or reuse a device's terminal. |
| `list_sessions` | `ssh-terminal` | List open terminals. |
| `exchange` | `ssh-terminal` | Write terminal input and read output. |
| `read_session` | `ssh-terminal` | Read output from a byte cursor. |
| `close_session` | `ssh-terminal` | Close a device's terminal. |
| `http_request` | HTTP backends | Send an authenticated WebUI API request. |

### HTTP requests

`http_request` accepts a configured `device`, an HTTP `method`, and a
device-relative `path`. Optional arguments are `query`, `headers`, `body`,
`body_base64`, `form`, and `timeout_seconds`. Supply at most one of `body`,
`body_base64`, and `form`.

The MCP server manages credentials, cookies, and device tokens. Callers cannot
supply credential-bearing headers, and sensitive authentication headers are not
returned. Redirects from agent requests are returned to the agent instead of
being followed automatically. Text responses are returned directly; binary
responses use base64 and set `body_encoding` to `base64`.

Example:

```json
{
  "device": "example-zte",
  "method": "POST",
  "path": "/?_type=vueData&_tag=vuecfg_data",
  "form": {
    "IF_ACTION": "Get"
  }
}
```

### SSH terminals

Each terminal has a background reader that appends device output to a temporary
transcript. Carry `initial_output.next_cursor` into the first `exchange`, then
carry each returned `next_cursor` forward. A quiet timeout means only that no new
bytes arrived during that interval; it does not prove that a command completed.

An MCP server restart necessarily closes active SSH connections and discards
their transcripts. Call `open_session` again for the device and continue with the
new initial cursor.

## State

SSH terminals are keyed by device name and are not owned by an MCP client
session. The tools do not expose or accept a terminal session identifier.
Streamable HTTP transport is also stateless, so restarting the MCP server does
not leave the client holding an obsolete MCP session identifier.

## Running the server

Validate a configuration:

```console
network-operator-mcp --config config/devices.local.yaml validate-config
```

Run over stdio:

```console
network-operator-mcp --config config/devices.local.yaml serve
```

Run with Streamable HTTP:

```console
network-operator-mcp --config config/devices.local.yaml serve \
  --transport streamable-http --host 127.0.0.1 --port 8000
```

The server also supports the `sse` transport.

## Security notes

- Protect configuration files because they contain plaintext credentials.
- SSH server host keys are not verified.
- Keep TLS verification enabled when a device has a trusted certificate. Use
  `verify_tls: false` only for devices whose WebUI certificate cannot be
  validated.
- Expose the MCP transport only to trusted agents and users. HTTP API calls can
  change device configuration.

# Network Operator MCP

`network-operator-mcp` 使得 Agent 可以访问网络设备的管理界面（例如 STelenet 等），
同时把凭据留在 MCP 服务端。设备通过 `type` 选择交互模型，目前实现两种后端：

- `ssh-terminal`：持续存在的 SSH PTY，适合华为 VRP 等交互式网络 CLI。
- `ssh-exec`：标准 SSH exec channel，一次调用执行一个命令，返回 stdout、stderr、
  exit status 和 exit signal，适合 OpenWrt、MikroTik 等设备。

MCP 仅提供在 Agent 和设备之间传递消息的能力，不识别提示符，不解释设备输出，也不判断终端命令是否成功。

该 MCP 支持 stdio、Streamable HTTP 和 SSE 三种 transport。

已测试设备：

| 厂商及型号 | 系统版本 | `ssh-terminal` | `ssh-exec` |
|---|---|---|---|
| Huawei S1730S-S48T4X-A1 | VRP 5.170（V200R022C00SPC500） | ✓ | — |
| Huawei S5720-28P-LI-AC | VRP 5.170（V200R011C10SPC600） | ✓ | — |
| Huawei S5720S-52P-LI-AC | VRP 5.170（V200R011C10SPC600） | ✓ | — |
| Huawei FutureMatrix S6720S-S24S28X-A | VRP 5.170（V200R022C00SPC500） | ✓ | — |
| MikroTik CCR2004-1G-12S+2XS（r3） | RouterOS 7.23.1（stable） | — | ✓ |
| - | OpenWRT, ImmortalWrt | — | ✓ |

## 配置

配置分为后端参数、账户和设备三部分：

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

accounts:
  password-operator:
    username: netadmin
    password: plaintext-password

  key-operator:
    username: root
    private_key: |
      -----BEGIN OPENSSH PRIVATE KEY-----
      ...
      -----END OPENSSH PRIVATE KEY-----

devices:
  example-huawei:
    type: ssh-terminal
    host: 192.0.2.10
    port: 22
    account: password-operator

  example-openwrt:
    type: ssh-exec
    host: 192.0.2.20
    port: 22
    account: key-operator
```

账户可以被多个设备引用。账户支持 `password`、内联 `private_key` 和可选的
`private_key_passphrase`。

AsyncSSH 不校验服务器 Host Key。

## MCP 工具

| 工具 | 设备类型 | 功能 |
|---|---|---|
| `list_devices` | 全部 | 返回可用设备的名称和类型。 |
| `ssh_execute` | `ssh-exec` | 执行一个标准 SSH exec 请求。 |
| `open_session` | `ssh-terminal` | 打开持久终端并返回初始输出。 |
| `list_sessions` | `ssh-terminal` | 列出当前 MCP 客户端拥有的会话。 |
| `exchange` | `ssh-terminal` | 幂等发送一次终端输入并读取输出。 |
| `read_session` | `ssh-terminal` | 从 byte cursor 非破坏性读取输出。 |
| `close_session` | `ssh-terminal` | 关闭终端会话。 |

### SSH exec

`ssh_execute` 接收 `device`、`command`、可选的 `stdin` 和
`timeout_seconds`，返回：

```json
{
  "device": "example-openwrt",
  "server_version": "SSH-2.0-dropbear",
  "stdout": "Linux OpenWrt ...\n",
  "stderr": "",
  "exit_status": 0,
  "exit_signal": null,
  "elapsed_ms": 80
}
```

每次调用建立一个 SSH connection、发出一个 exec request、等待 EOF 和退出状态，
然后关闭连接。这里的完成边界来自 SSH 协议，不使用 quiet timeout 或提示符判断。

### SSH terminal

每个终端会话都有后台 Reader，把设备输出持续追加到临时 transcript。MCP 调用超时
只结束本次等待，不停止 SSH 数据流。

终止读取的原因：

- `quiet`：收到输出后，连续一段时间没有新字节；不表示命令完成。
- `deadline`：本次等待到期，设备可能仍在运行或输出。
- `response_limit`：达到响应大小限制，用返回的 cursor 继续读取。
- `eof`：SSH terminal channel 已结束。

`deadline` 和 `response_limit` 会把会话标记为 `unsettled`。调用方应继续读取、发送
`CTRL_C`，或者明确使用 `force_write=true`。

`exchange` 的 `input_type`：

- `line`：编码文本并追加 CR。
- `text`：只发送文本本身。
- `key`：支持 `ENTER`、`SPACE`、`CTRL_C`、`CTRL_Z` 和 `Q`。

每次写入使用新的 `request_id`；使用相同 ID 重试会返回原结果，不会重复写入。
`expected_outbound_seq` 用来确认调用方掌握最新写入顺序。

调用方必须把 `initial_output.next_cursor` 传给第一次 `exchange`，然后持续传递每次
返回的 `next_cursor`。服务端在写入前记录 `write_cursor`，并从调用方 cursor 返回
输出，因此 quiet 后迟到的字节不会被下一次写入跳过。

```text
opened = open_session(device)
cursor = opened.initial_output.next_cursor
seq = opened.session.outbound_seq

r1 = exchange(session_id, request_id="1", data="screen-length 0 temporary",
              cursor=cursor, expected_outbound_seq=seq)
cursor = r1.next_cursor
seq = r1.outbound_seq
```

一个终端会话只属于创建它的 MCP client session。同一服务进程中，一台
`ssh-terminal` 设备最多只有一个会话。其他客户端不能查看或接管该会话，尝试连接
时会得到明确的“被另一 MCP 客户端占用，请等待”错误。占用一直保留到 SSH 关闭
完成。

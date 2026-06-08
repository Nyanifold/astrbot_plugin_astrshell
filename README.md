# astrshell

An [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin that lets you interact with AstrBot directly from your zsh terminal.

## Requirements

- AstrBot v4.x
- zsh

## Installation

astrshell has **two independent setup steps** — one on the AstrBot side, one on your local machine. Both are required.

### Step 1 — AstrBot side

Install the plugin through AstrBot's plugin manager, then add an **astrshell** platform adapter in the AstrBot dashboard and enable it. This starts the socket server that your shell will connect to.

### Step 2 — Local shell side

Install the `astrshell` CLI on your local machine. This provides the `astrshell` and `astrshell-setup` commands that wire up your zsh.

With **uv** (recommended):

```zsh
uv tool install "astrbot-plugin-astrshell @ git+https://github.com/Nyanifold/astrbot_plugin_astrshell"
```

With **pip**:

```zsh
pip install git+https://github.com/Nyanifold/astrbot_plugin_astrshell
```

Then connect your shell:

**Auto-connect on every shell start** — run once to register astrshell in `~/.zshrc`:

```zsh
astrshell-setup
```

This appends a `source` line to your `~/.zshrc`. Restart your shell or run `source ~/.zshrc` to activate.

**Manual launch** — if you prefer to start astrshell only when needed, skip `astrshell-setup` and run this command whenever you want a session:

```zsh
astrshell
```

This opens a new zsh with astrshell active. Your normal `~/.zshrc` is untouched and no connection is made in other terminals.

### Environment variables

These variables can be set before sourcing or launching astrshell:

| Variable | Default | Description |
|----------|---------|-------------|
| `ASTRSHELL_SOCK` | `~/.astrshell/daemon.sock` | Connection target. Accepts a UDS path, `host:port` for direct TCP, or `user@host:port` to connect via an auto-created SSH tunnel |
| `ASTRSHELL_DIR` | `~/.astrshell` | Base data directory for config, socket, and logs |
| `ASTRSHELL_DEBUG` | _(unset)_ | Set to `1` to enable verbose debug output on stderr |

## Usage

All astrshell input starts with `]`. Commands are sent to AstrBot as messages.

| Input | Behavior |
|-------|----------|
| `]<text>` | Send text to AstrBot |
| `]/<cmd>` | Forward a slash command to AstrBot (e.g. `]/reset`) |
| `]//<cmd>` | AstrShell internal command |
| `]!<cmd>` | Run a shell command; send its stdout, stderr, and exit code to AstrBot |
| `]$<cmd>` | Run a shell command; send its stdout to AstrBot |
| `]#<note>` | Append a comment to the recording buffer (silent) |
| `]#!<cmd>` | Run a command and record it silently |
| `]##[note]` | Flush the recording buffer and send it to AstrBot |

Prefix any of the above with `]&` to send asynchronously — the prompt returns immediately while AstrBot processes in the background.

### Internal commands

| Command | Description |
|---------|-------------|
| `//help` | Show command reference |
| `//help -a [cmd]` | Show AstrBot built-in commands |
| `//status` | Show adapter status (PID, uptime, connections) |
| `//start` / `//end` | Start / stop a recording session |
| `//clear-buffer` | Discard the recording buffer |

Press `Ctrl-C` or `ESC` while waiting for a response to cancel the current request.

## Configuration

The following options can be set in the AstrBot platform adapter config:

| Option | Default | Description |
|--------|---------|-------------|
| `socket_path` | `~/.astrshell/daemon.sock` | UDS socket path, or `host:port` for TCP, or `user@host:port` for SSH tunnel |
| `pipeline_register_timeout` | `5.0` | Seconds to wait for pipeline registration |
| `pipeline_poll_interval` | `0.05` | Polling interval in seconds |
| `max_head_lines` | `100` | Max lines kept from the start of `!` command output |
| `max_tail_lines` | `100` | Max lines kept from the end of `!` command output |

Output truncation applies when line count reaches `1.5 × (max_head_lines + max_tail_lines)`. The omitted middle section is replaced with a `<...N lines omitted...>` marker.

## Protocol & Formatting

### Transport

The zsh client and the AstrBot adapter communicate over a **Unix Domain Socket** (default: `~/.astrshell/daemon.sock`). Two alternative transports are available via `ASTRSHELL_SOCK`:

- **Direct TCP** — `host:port` (e.g. `127.0.0.1:7890`): uses zsh's `zsh/net/tcp` module.
- **SSH tunnel** — `user@host:port`: astrshell automatically runs `ssh -f -N -L` to forward the remote port to localhost, then connects over TCP. The tunnel is torn down when the shell exits.

### Wire protocol

All messages in both directions use the same binary framing with three ASCII control characters as delimiters:

| Character | Hex | Role |
|-----------|-----|------|
| RS (Record Separator) | `\x1e` | Separates successive key-value pairs |
| US (Unit Separator) | `\x1f` | Separates a key from its value |
| GS (Group Separator) | `\x1d` | Terminates a complete message |

A single message on the wire looks like:

```
RS key1 US value1  RS key2 US value2  …  GS
```

Values may contain newlines and any byte except `GS`, so multi-line command output passes through unmodified without escaping.

### Handshake

```
zsh                              AstrBot
 │── type=connect, session_id ──▶│
 │◀── type=ready ────────────────│
```

After the socket is connected, zsh sends a `connect` frame carrying the session ID (the current Unix username). The adapter registers the connection and replies with `ready`. Only then does the shell begin sending user input.

### Message types

**zsh → AstrBot:**

| `type` field | Additional fields | Sent when |
|---|---|---|
| `connect` | `session_id` | On connection |
| `input` | `raw`, `cmd`, `stdout`, `stderr`, `exit_code`, `cwd`, `id`, `async` | User sends a `]`-prefixed line |
| `record_cmd` | `cmd`, `exit_code`, `id` | Every command that runs while recording is active |
| `stop` | `id` | User presses Ctrl-C or ESC |
| `disconnect` | — | Shell exits |

**AstrBot → zsh:**

| `type` field | Additional fields | Meaning |
|---|---|---|
| `ready` | — | Handshake complete |
| `reply` | `text`, `id`, `async` | AI response text |
| `ack` | `recording`, `id` | Non-pipeline command acknowledged |
| `end` | `id`, `async` | Request fully processed |
| `error` | `message`, `id`, `async` | Error from the adapter or pipeline |

### Request tracking and async mode

Every `input` frame carries a monotonically increasing sequence number in the `id` field. The adapter echoes the same `id` back on every `reply`, `ack`, and `end` frame, so the shell can match responses to requests and discard stale frames from interrupted sessions.

When a request is sent with `async=true` (the `]&` prefix), reply frames carry `async=true` and the shell does not block waiting for them. Instead, a ZLE file-descriptor callback fires whenever data arrives on the socket at the prompt, and async frames are rendered inline without interrupting the current input line.

### Message content assembly

For `]!` commands the message sent to AstrBot is assembled as:

```
$ <command>
<stdout>
<stderr>          (omitted if empty)
exit: <code>
current working directory: <cwd>   (appended only when the directory changed)
```

If stdout or stderr exceeds the truncation threshold (`1.5 × (max_head_lines + max_tail_lines)` lines), the middle section is replaced with a `<...N lines omitted...>` marker before the message is assembled.

### Reply formatting

AstrBot replies are rendered with [Rich](https://github.com/Textualize/rich) before being written back to the socket:

- A horizontal rule is printed above and below each reply.
- The agent name (`• AstrShell`) is printed as a bold header between the two rules. The header is suppressed for subsequent streaming chunks in the same request.
- Reply text is rendered as **Markdown** by default. For `]!` and `]$` commands, Markdown rendering is disabled and the text is printed verbatim.
- In async mode the top rule is bold; in sync mode it is dimmed.

The formatted ANSI output is captured into a string and sent as the `text` field of a `reply` frame.

## License

MIT

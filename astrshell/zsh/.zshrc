# ── Bootstrap: source user's .zshrc unless --skip-zshrc was passed ────────
# When launched via the `astrshell` CLI command, $1 is empty and we source
# ~/.zshrc first to preserve the user's full environment.
# When sourced from inside ~/.zshrc itself (via `astrshell-setup`), $1 is
# "--skip-zshrc" to prevent recursive sourcing.
if [[ "$1" != "--skip-zshrc" ]]; then
    [[ -f ~/.zshrc ]] && source ~/.zshrc
fi

# ── Idempotency guard ──────────────────────────────────────────────────────
# The inner source (from ~/.zshrc) already ran the full init; the outer
# execution (CLI launcher) must not run it a second time.
(( ${+_astrshell_initialized} )) && return 0
typeset -g _astrshell_initialized=1

# AstrShell — source this file from .zshrc
# Usage:
#   source /path/to/astrshell/astrshell.zsh [OPTIONS]
#
# Options:
#   --project DIR   path to astrshell project directory (for development / uv)
#   --debug         enable debug logging to stderr
#
# Environment variables:
#   ASTRSHELL_DIR     — base data directory (default: ~/.astrshell)
#                       config file, socket, logs, and AstrBot data live here
#   ASTRSHELL_PROJECT — path to astrshell project directory (overridden by --project)
#   ASTRSHELL_DEBUG   — set to 1 to enable debug output
#   ASTRSHELL_SOCK    — override connection target (default: ~/.astrshell/daemon.sock)
#                       UDS:  /path/to/daemon.sock  or  ~/.astrshell/daemon.sock
#                       TCP:  127.0.0.1:7890  or  :7890  (direct local TCP)
#                       SSH:  [user@]host:port  (auto SSH tunnel via ssh -f; supports password/2FA;
#                             host must not be localhost/127.0.0.1/0.0.0.0/::1)
#
# All other settings (paths, timeouts, polling intervals, log format …) are
# read from  $ASTRSHELL_DIR/astrshell_config.toml.  Run `astrshell init` to
# create a commented default config at that path.

# ── Argument parsing (from source invocation) ──────────────────────────────
typeset -g _astrshell_opt_project=""
typeset -g _astrshell_debug=${ASTRSHELL_DEBUG:-0}

() {
    local _i=1
    while (( _i <= $# )); do
        case "${@[_i]}" in
            --project|--dir) _i=$(( _i + 1 )); _astrshell_opt_project="${@[_i]}" ;;
            --debug)         _astrshell_debug=1 ;;
        esac
        _i=$(( _i + 1 ))
    done
} "$@"

# ── Debug logging ──────────────────────────────────────────────────────────
_astrshell_debug() {
    (( _astrshell_debug )) || return
    print -u2 -- "[astrshell:debug] $*"
}

# ── State ──────────────────────────────────────────────────────────────────
typeset -g _astrshell_recording=0
typeset -g _astrshell_last_cmd=""
typeset -g _astrshell_seq=0
typeset -g _ASTR_FD=-1  # socket FD (bidirectional); -1 = not connected
typeset -g _ASTR_W=-1   # alias → _ASTR_FD
typeset -g _ASTR_R=-1   # alias → _ASTR_FD
typeset -g _ASTR_SSH_CTL=""   # control socket path for SSH tunnel (empty = no tunnel)
typeset -g _ASTR_SSH_HOST=""  # SSH host saved for tunnel teardown
typeset -gA _astrshell_async_seqs=()   # seq IDs of in-flight async requests (write-only except end cleanup)
typeset -ga _astrshell_async_queue=()  # async frames buffered during sync waits
typeset -g _astrshell_connected=0      # 1 after daemon sends "ready", 0 otherwise

# Suppress execution of bare ] commands (astrshell intercepts them)
']'() { return 0 }

# ── Wire protocol ───────────────────────────────────────────────────────────
# Messages between the zsh coproc and the Python daemon use three ASCII
# control characters as delimiters, avoiding any need to escape field values
# (multi-line stdout/stderr pass through unmodified):
#
#   US  \x1f  Unit Separator   — separates a key from its value
#   RS  \x1e  Record Separator — separates successive key-value fields
#   GS  \x1d  Group Separator  — terminates a complete message
#
# Wire format (one message):
#
#   RS key1 US val1  RS key2 US val2  …  RS keyN US valN  GS
#
# Values may contain newlines and any byte except GS (\x1d), which is
# vanishingly rare in real shell output.
#
# Sending:   _astrshell_send  key1 val1  [key2 val2 …]
# Receiving: IFS= read -r -d "$_ASTR_GS" -u "$_ASTR_R" _raw
#            _astrshell_parse_raw "$_raw"  → populates _astrshell_msg[]
#            Access fields as: $_astrshell_msg[key]

(( ${+_ASTR_RS} )) || {
    typeset -gr _ASTR_RS=$'\x1e'   # Record Separator
    typeset -gr _ASTR_US=$'\x1f'   # Unit Separator
    typeset -gr _ASTR_GS=$'\x1d'   # Group Separator
}
typeset -gA _astrshell_msg=()  # Last parsed message (populated by _astrshell_parse_raw)

_astrshell_send() {
    # Send a message to the daemon.  Arguments: key val [key val …]
    local _out=""
    local _debug_out=""
    while (( $# >= 2 )); do
        _out="${_out}${_ASTR_RS}${1}${_ASTR_US}${2}"
        _debug_out="${_debug_out} ${1}=${2}"
        shift 2
    done
    # Always attach current working directory
    local _cwd="$(pwd)"
    _out="${_out}${_ASTR_RS}cwd${_ASTR_US}${_cwd}"
    _debug_out="${_debug_out} cwd=${_cwd}"
    _astrshell_debug "SEND ->${_debug_out}"
    printf '%s%s' "$_out" "$_ASTR_GS" >&$_ASTR_W
}

_astrshell_parse_raw() {
    # Parse a raw message (trailing GS already stripped by read -d) into
    # the global _astrshell_msg[] associative array.
    #
    # NOTE: the s:...: flag does NOT expand ${var} or $'\xNN' escapes on
    # zsh 5.5 and is therefore unusable with control-character separators.
    # Use %% / # parameter expansion instead — variables expand normally
    # there and the loop correctly handles any byte value including newlines.
    _astrshell_msg=()
    local _raw="$1" _rest _pair _k _v
    local -a _pairs=()
    _rest="$_raw"
    while [[ "$_rest" == *"${_ASTR_RS}"* ]]; do
        _pairs+=("${_rest%%${_ASTR_RS}*}")
        _rest="${_rest#*${_ASTR_RS}}"
    done
    _pairs+=("$_rest")
    for _pair in "${_pairs[@]}"; do
        [[ -z "$_pair" ]] && continue
        _k="${_pair%%${_ASTR_US}*}"
        _v="${_pair#*${_ASTR_US}}"
        [[ -n "$_k" ]] && _astrshell_msg[$_k]="$_v"
    done
}

# ── Command execution helpers ──────────────────────────────────────────────

_astrshell_exec_capture() {
    # Sets: _astrshell_stdout, _astrshell_stderr, _astrshell_exit
    local _tmpout _tmperr
    _tmpout=$(mktemp)
    _tmperr=$(mktemp)
    eval "$1" >"$_tmpout" 2>"$_tmperr"
    _astrshell_exit=$?
    _astrshell_stdout=$(cat "$_tmpout")
    _astrshell_stderr=$(cat "$_tmperr")
    rm -f "$_tmpout" "$_tmperr"
}

_astrshell_exec_stdout() {
    # Only capture stdout; discard stderr
    local _tmpout
    _tmpout=$(mktemp)
    eval "$1" >"$_tmpout" 2>/dev/null
    _astrshell_exit=$?
    _astrshell_stdout=$(cat "$_tmpout")
    rm -f "$_tmpout"
}

# ── Daemon I/O ─────────────────────────────────────────────────────────────

# Dispatch one received message.  _astrshell_msg[] must already be populated
# by _astrshell_parse_raw.  Returns 1 if the read loop should stop.
_astrshell_dispatch_msg() {
    local _id="${1:-}"
    local _mid _type

    _astrshell_debug "dispatch: type=$_astrshell_msg[type] id=$_astrshell_msg[id] async=$_astrshell_msg[async] (expected_id=$_id)"

    # Discard replies belonging to a different (interrupted) request
    if [[ -n "$_id" ]]; then
        _mid="$_astrshell_msg[id]"
        if [[ -n "$_mid" && "$_mid" != "$_id" ]]; then
            _astrshell_debug "dispatch: DISCARD (id mismatch $_mid != $_id)"
            return 0
        fi
    fi

    _type="$_astrshell_msg[type]"
    case "$_type" in
        end|ready)
            _astrshell_debug "dispatch: BREAK (type=$_type)"
            return 1
            ;;
        init_info)
            _astrshell_debug "dispatch: init_info PID=$_astrshell_msg[daemon_pid]"
            print -u2 -- ""
            print -u2 -- "Daemon Info"
            print -u2 -- "  PID: $_astrshell_msg[daemon_pid]"
            print -u2 -- "  Started: $_astrshell_msg[daemon_start_time]"
            print -u2 -- "  Connections: $_astrshell_msg[connection_count]"
            print -u2 -- "  Log: $_astrshell_msg[log_file]"
            ;;
        reply)
            _astrshell_debug "dispatch: reply text_len=${#_astrshell_msg[text]}"
            print -- "$_astrshell_msg[text]"
            ;;
        error)
            _astrshell_debug "dispatch: error message='$_astrshell_msg[message]'"
            print -u2 -- "astrshell error: $_astrshell_msg[message]"
            ;;
        ack)
            local _rec="$_astrshell_msg[recording]"
            _astrshell_debug "dispatch: ack recording=$_rec"
            if [[ "$_rec" == "true" ]]; then
                _astrshell_recording=1
            elif [[ "$_rec" == "false" ]]; then
                _astrshell_recording=0
            fi
            return 1
            ;;
        *)
            _astrshell_debug "dispatch: UNKNOWN type='$_type'"
            ;;
    esac
    return 0
}

_astrshell_read_reply() {
    local _id="${1:-}"
    local _raw=""
    local _spin='-\|/' _si=0 _showed_spin=0
    local _stop_sent=0   # have we sent a stop message to the daemon?
    local _got_int=0     # set by INT trap when Ctrl-C fires

    _astrshell_debug "read_reply: START (expected_id=$_id)"

    # Disable ZLE callback to prevent it from stealing sync messages
    zle -F $_ASTR_R 2>/dev/null
    _astrshell_debug "read_reply: ZLE callback disabled"

    # Catch SIGINT: set flag instead of aborting so we can notify the daemon.
    # The trap is scoped to this function and restored on return.
    trap '_got_int=1' INT

    while true; do
        if IFS= read -t 0.1 -r -d "$_ASTR_GS" -u "$_ASTR_R" _raw 2>/dev/null; then
            # ── Got a message from daemon ───────────────────────────────────
            _astrshell_debug "read_reply: RECEIVED raw_len=${#_raw}"
            if (( _showed_spin )); then
                printf '\r \r' >&2
                _showed_spin=0
            fi
            _astrshell_parse_raw "$_raw"
            if [[ "$_astrshell_msg[async]" == "true" ]]; then
                _astrshell_debug "read_reply: QUEUED (async message) queue_len=${#_astrshell_async_queue[@]}"
                _astrshell_async_queue+=("$_raw")
                continue
            fi
            _astrshell_dispatch_msg "$_id" || break
        else
            # ── Timeout — check for interrupt signals ───────────────────────
            local _do_stop=0

            # Ctrl-C: SIGINT fired while read was blocking → flag is set
            if (( _got_int )); then
                _got_int=0
                _do_stop=1
                _astrshell_debug "read_reply: SIGINT detected"
            fi

            # Bare ESC: short-timeout read from terminal (only while spinner shown).
            # Use a small positive timeout so zsh enters raw mode and flushes the
            # kernel input buffer — t=0 returns before the ESC char is available.
            # Omit -u so -k reads directly from the terminal device.
            if (( _showed_spin && ! _do_stop && ! _stop_sent )); then
                local _ch=""
                if read -k 1 -t 0.02 _ch 2>/dev/null && [[ "$_ch" == $'\e' ]]; then
                    # Drain any bytes that follow — arrow keys etc. arrive within ~1ms
                    local _drain=""
                    read -k 10 -t 0.05 _drain 2>/dev/null || true
                    # Only bare ESC (nothing following within 50ms) triggers stop
                    if [[ -z "$_drain" ]]; then
                        _do_stop=1
                        _astrshell_debug "read_reply: ESC detected"
                    fi
                fi
            fi

            if (( _do_stop && ! _stop_sent )); then
                _stop_sent=1
                if (( _showed_spin )); then
                    printf '\r \r' >&2
                    _showed_spin=0
                fi
                print -u2 -- "^C"
                _astrshell_debug "read_reply: sending stop"
                _astrshell_send type stop id "${_id:-0}"
                # Second Ctrl-C forces exit from the wait loop
                trap 'break' INT
            fi

            # Advance spinner (continues even after stop is sent)
            printf '\r%s' "${_spin:$(( _si % 4 )):1}" >&2
            _si=$(( _si + 1 ))
            _showed_spin=1
            # Debug: every 10 iterations, check if data is available
            if (( _astrshell_debug && _si % 40 == 0 )); then
                _astrshell_debug "read_reply: still waiting (fd=$_ASTR_R, iterations=$_si)"
            fi
        fi
    done

    trap - INT

    # Re-enable ZLE callback
    zle -F $_ASTR_R _astrshell_async_handler 2>/dev/null
    _astrshell_debug "read_reply: END (queue_len=${#_astrshell_async_queue[@]}), ZLE callback restored"
}

# ── Async display helpers ──────────────────────────────────────────────────

# Render one async message in ZLE context. _astrshell_msg[] must be populated.
_astrshell_display_async_msg() {
    local _type="$_astrshell_msg[type]"
    local _mid="$_astrshell_msg[id]"
    _astrshell_debug "display_async: type=$_type id=$_mid"
    case "$_type" in
        reply|push)
            zle -I
            print -- "$_astrshell_msg[text]"
            zle reset-prompt
            ;;
        error)
            zle -I
            print -u2 -- "astrshell async error: $_astrshell_msg[message]"
            zle reset-prompt
            ;;
        end)
            _astrshell_debug "display_async: END received, removing $_mid from async_seqs"
            [[ -n "$_mid" ]] && unset "_astrshell_async_seqs[$_mid]"
            ;;
    esac
}

# Drain async queue in non-ZLE context (precmd). Skipped when ZLE is active.
_astrshell_drain_async_queue_shell() {
    [[ -o zle ]] && return
    (( ${#_astrshell_async_queue[@]} == 0 )) && return
    _astrshell_debug "drain_shell: processing ${#_astrshell_async_queue[@]} queued messages"
    local _q
    for _q in "${_astrshell_async_queue[@]}"; do
        _astrshell_parse_raw "$_q"
        local _type="$_astrshell_msg[type]" _mid="$_astrshell_msg[id]"
        _astrshell_debug "drain_shell: type=$_type id=$_mid"
        case "$_type" in
            reply|push) print -- "$_astrshell_msg[text]" ;;
            error)      print -u2 -- "astrshell async error: $_astrshell_msg[message]" ;;
            end)        [[ -n "$_mid" ]] && unset "_astrshell_async_seqs[$_mid]" ;;
        esac
    done
    _astrshell_debug "drain_shell: queue drained"
    _astrshell_async_queue=()
}

# ZLE file-descriptor callback — fires when data arrives on $_ASTR_R at the prompt.
_astrshell_async_handler() {
    local _fd="$1" _raw _q
    _astrshell_debug "async_handler: triggered (queue_len=${#_astrshell_async_queue[@]})"

    # 1. Drain frames buffered during prior sync waits (in arrival order)
    if (( ${#_astrshell_async_queue[@]} > 0 )); then
        _astrshell_debug "async_handler: draining ${#_astrshell_async_queue[@]} buffered frames"
    fi
    for _q in "${_astrshell_async_queue[@]}"; do
        _astrshell_parse_raw "$_q"
        _astrshell_display_async_msg
    done
    _astrshell_async_queue=()

    # 2. Read all immediately available frames from FD (non-blocking).
    #    By the single-outstanding-sync invariant any frame here must be async;
    #    the guard is defensive.
    local _read_count=0
    while IFS= read -t 0 -r -d "$_ASTR_GS" -u "$_fd" _raw 2>/dev/null; do
        _astrshell_debug "async_handler: READ raw_len=${#_raw}"
        _astrshell_parse_raw "$_raw"
        [[ "$_astrshell_msg[async]" == "true" ]] || {
            _astrshell_debug "async_handler: SKIP (not async) type=$_astrshell_msg[type]"
            continue
        }
        _astrshell_display_async_msg
        _read_count=$(( _read_count + 1 ))
    done
    _astrshell_debug "async_handler: done (read $_read_count frames)"
}
zle -N _astrshell_async_handler

# Socket path: env var overrides the default
_ASTR_SOCK="${ASTRSHELL_SOCK:-${HOME}/.astrshell/daemon.sock}"

# ── Socket startup (UDS or TCP) ────────────────────────────────────────────
_astrshell_start() {
    # Compute stable session_id = username (shared across all terminals)
    local _user="${USER:-$(id -un 2>/dev/null)}"
    local _session_id="${_user}"

    if [[ "$_ASTR_SOCK" == *:* ]]; then
        # ── TCP mode ──────────────────────────────────────────────────────
        zmodload zsh/net/tcp 2>/dev/null || {
            print -u2 "astrshell: zsh/net/tcp module not available"
            return 1
        }
        local _tcp_host="${_ASTR_SOCK%:*}"
        local _tcp_port="${_ASTR_SOCK#*:}"
        # Strip user@ prefix to get bare hostname for locality check
        local _bare_host="${_tcp_host##*@}"

        if [[ -z "$_bare_host" || "$_bare_host" == "localhost" || "$_bare_host" == "127.0.0.1" || "$_bare_host" == "::1" || "$_bare_host" == "0.0.0.0" ]]; then
            # ── Direct local TCP ──────────────────────────────────────────
            ztcp "${_bare_host:-127.0.0.1}" "$_tcp_port" 2>/dev/null || {
                print -u2 "astrshell: failed to connect to ${_bare_host:-127.0.0.1}:$_tcp_port"
                return 1
            }
        else
            # ── Remote host: auto SSH tunnel ──────────────────────────────
            # -f  : authenticate interactively (handles password/2FA),
            #       then daemonize once the tunnel is up
            # -N  : no remote command
            # -M -S <ctl> : master mode with control socket for clean teardown
            # ExitOnForwardFailure : exit non-zero if port forwarding fails
            typeset -g _ASTR_SSH_CTL="${TMPDIR:-/tmp}/astrshell_ssh_$$"
            typeset -g _ASTR_SSH_HOST="$_tcp_host"
            print -u2 "astrshell: establishing SSH tunnel to ${_tcp_host}:${_tcp_port} ..."
            # -4 : force IPv4 to avoid bind [::1] errors on IPv6-disabled nodes
            ssh -4 -f -N -M -S "$_ASTR_SSH_CTL" \
                -o ExitOnForwardFailure=yes \
                -L "127.0.0.1:${_tcp_port}:localhost:${_tcp_port}" "$_tcp_host" || {
                print -u2 "astrshell: SSH tunnel failed"
                _ASTR_SSH_CTL=""
                _ASTR_SSH_HOST=""
                return 1
            }
            print -u2 "astrshell: SSH tunnel ready, connecting ..."
            ztcp 127.0.0.1 "$_tcp_port" 2>/dev/null || {
                print -u2 "astrshell: failed to connect via SSH tunnel"
                ssh -S "$_ASTR_SSH_CTL" -O exit "$_ASTR_SSH_HOST" 2>/dev/null
                _ASTR_SSH_CTL=""
                _ASTR_SSH_HOST=""
                return 1
            }
        fi
    else
        # ── UDS mode ──────────────────────────────────────────────────────
        mkdir -p "${_ASTR_SOCK:h}"
        zmodload zsh/net/socket 2>/dev/null || {
            print -u2 "astrshell: zsh/net/socket module not available"
            return 1
        }
        if ! [[ -S "$_ASTR_SOCK" ]]; then
            print -u2 "astrshell: AstrBot is not running or socket not found at $_ASTR_SOCK"
            print -u2 "Start AstrBot with the astrshell plugin enabled, then open a new terminal."
            return 1
        fi
        zsocket "$_ASTR_SOCK" 2>/dev/null || {
            print -u2 "astrshell: failed to connect to socket at $_ASTR_SOCK"
            return 1
        }
    fi

    # $REPLY now holds the connected socket FD
    typeset -g _ASTR_FD=$REPLY
    typeset -g _ASTR_R=$REPLY
    typeset -g _ASTR_W=$REPLY

    # Send connect handshake
    _astrshell_send type connect session_id "$_session_id"

    print -u2 -- "astrshell: connected (session $_session_id), waiting for ready..."

    # Wait for type=ready; display type=init progress messages along the way
    local _raw _n=0
    while IFS= read -r -d "$_ASTR_GS" -u "$_ASTR_R" _raw; do
        _n=$(( _n + 1 ))
        _astrshell_parse_raw "$_raw"
        if [[ "$_astrshell_msg[type]" == "ready" ]]; then
            print -u2 -- "astrshell: daemon ready"
            _astrshell_connected=1
            break
        elif [[ "$_astrshell_msg[type]" == "init" ]]; then
            print -u2 -- "astrshell: [init] $_astrshell_msg[msg]"
        elif [[ "$_astrshell_msg[type]" == "init_info" ]]; then
            print -u2 -- ""
            print -u2 -- "Daemon Info"
            print -u2 -- "  PID: $_astrshell_msg[daemon_pid]"
            print -u2 -- "  Started: $_astrshell_msg[daemon_start_time]"
            print -u2 -- "  Connections: $_astrshell_msg[connection_count]"
            print -u2 -- "  Log: $_astrshell_msg[log_file]"
        else
            print -u2 -- "astrshell: [unexpected] (type=$_astrshell_msg[type])"
        fi
    done
    if (( _n == 0 )); then
        print -u2 -- "astrshell: WARNING — socket closed without ready signal"
    fi
}

_astrshell_start
(( _astrshell_connected )) && zle -F $_ASTR_R _astrshell_async_handler

# ── Shutdown on exit ───────────────────────────────────────────────────────
zshexit() {
    (( _astrshell_connected )) || return
    _astrshell_send type disconnect id 0
    if [[ "$_ASTR_SOCK" == *:* ]]; then
        ztcp -c $_ASTR_FD 2>/dev/null
        [[ -n "$_ASTR_SSH_CTL" ]] && \
            ssh -S "$_ASTR_SSH_CTL" -O exit "$_ASTR_SSH_HOST" 2>/dev/null
    else
        zsocket -c $_ASTR_FD 2>/dev/null
    fi
}

# ── preexec / precmd hooks ─────────────────────────────────────────────────
preexec() {
    _astrshell_last_cmd=$1
}

precmd() {
    local _last_exit
    _last_exit=$?
    # Drain any async frames buffered during a prior sync wait (non-ZLE path)
    _astrshell_drain_async_queue_shell
    # Reset prompt style before each new prompt is drawn
    PROMPT="$_astrshell_base_prompt"
    _astrshell_bracket_state=0
    if [[ $_astrshell_recording == 1 ]]; then
        _astrshell_seq=$(( _astrshell_seq + 1 ))
        _astrshell_send type record_cmd id "$_astrshell_seq" cmd "$_astrshell_last_cmd" exit_code "$_last_exit"
        _astrshell_read_reply "$_astrshell_seq"
    fi
}

# ── Main accept-line widget ────────────────────────────────────────────────
_astrshell_accept_line() {
    local _buf="$BUFFER"

    # ] prefix
    if [[ "$_buf" == \]* ]]; then
        # Strip ] and determine token (space-insensitive)
        local _stripped="${_buf#]}"
        local _token=""
        local _rest="$_stripped"
        while [[ "$_rest" == [\ $'\t']* ]]; do _rest="${_rest#?}"; done
        # ]& async modifier — must be detected before token collection
        local _async=0
        if [[ "$_rest" == '&'* ]]; then
            _async=1
            _rest="${_rest#&}"
            while [[ "$_rest" == [\ $'\t']* ]]; do _rest="${_rest#?}"; done
        fi
        while [[ "$_rest" == [#!\$]* ]]; do
            _token="${_token}${_rest[1]}"
            _rest="${_rest#?}"
            while [[ "$_rest" == [\ $'\t']* ]]; do _rest="${_rest#?}"; done
        done
        local _body="${_rest}"

        # ] with only whitespace — stay in ] mode, no history, no daemon call
        if [[ -z "$_token" && -z "$_body" ]]; then
            print ""
            BUFFER="]"
            CURSOR=1
            _astrshell_apply_style
            zle reset-prompt
            return
        fi

        if (( ! _astrshell_connected )); then
            print ""
            print -u2 -- "astrshell: not connected (start AstrBot and open a new terminal)"
            BUFFER="]"
            CURSOR=1
            _astrshell_apply_style
            zle reset-prompt
            return
        fi

        print -s -- "$_buf"
        HISTNO=$(( $#history + 1 ))
        print ""
        BUFFER=""
        _astrshell_apply_style
        zle reset-prompt

        _astrshell_seq=$(( _astrshell_seq + 1 ))
        local _seq="$_astrshell_seq"
        local -a _async_kv=()
        (( _async )) && _async_kv=(async true)
        case "$_token" in
            "!")
                _astrshell_exec_capture "$_body"
                [[ -n "$_astrshell_stdout" ]] && print -r -- "$_astrshell_stdout"
                [[ -n "$_astrshell_stderr" ]] && print -r -u2 -- "$_astrshell_stderr"
                _astrshell_send type input id "$_seq" raw "$_buf" cmd "$_body" \
                    stdout "$_astrshell_stdout" stderr "$_astrshell_stderr" \
                    exit_code "$_astrshell_exit" "${_async_kv[@]}"
                ;;
            '$')
                _astrshell_exec_stdout "$_body"
                if [[ -z "$_astrshell_stdout" ]]; then
                    print -u2 -- "astrshell: \$interpolation produced empty output (exit $_astrshell_exit): $_body"
                    BUFFER="]"
                    CURSOR=1
                    _astrshell_apply_style
                    zle reset-prompt
                    return
                fi
                print -r -- "$_astrshell_stdout"
                _astrshell_send type input id "$_seq" raw "$_buf" cmd "$_body" \
                    stdout "$_astrshell_stdout" exit_code "$_astrshell_exit" "${_async_kv[@]}"
                ;;
            "#!")
                _astrshell_exec_capture "$_body"
                [[ -n "$_astrshell_stdout" ]] && print -r -- "$_astrshell_stdout"
                [[ -n "$_astrshell_stderr" ]] && print -r -u2 -- "$_astrshell_stderr"
                _astrshell_send type input id "$_seq" raw "$_buf" cmd "$_body" \
                    stdout "$_astrshell_stdout" stderr "$_astrshell_stderr" \
                    exit_code "$_astrshell_exit" "${_async_kv[@]}"
                ;;
            '#$')
                _astrshell_exec_stdout "$_body"
                if [[ -z "$_astrshell_stdout" ]]; then
                    print -u2 -- "astrshell: \$interpolation produced empty output (exit $_astrshell_exit): $_body"
                    BUFFER="]"
                    CURSOR=1
                    _astrshell_apply_style
                    zle reset-prompt
                    return
                fi
                print -r -- "$_astrshell_stdout"
                _astrshell_send type input id "$_seq" raw "$_buf" cmd "$_body" \
                    stdout "$_astrshell_stdout" exit_code "$_astrshell_exit" "${_async_kv[@]}"
                ;;
            *)
                _astrshell_send type input id "$_seq" raw "$_buf" "${_async_kv[@]}"
                ;;
        esac

        if (( _async )); then
            _astrshell_async_seqs[$_seq]=1
            _astrshell_debug "accept_line: async mode, seq=$_seq registered"
        else
            _astrshell_debug "accept_line: waiting for reply seq=$_seq"
            _astrshell_read_reply "$_seq"
            # Drain async queue (ZLE context) before redrawing ] prompt
            if (( ${#_astrshell_async_queue[@]} > 0 )); then
                _astrshell_debug "accept_line: draining ${#_astrshell_async_queue[@]} async messages post-reply"
            fi
            local _q
            for _q in "${_astrshell_async_queue[@]}"; do
                _astrshell_parse_raw "$_q"
                _astrshell_display_async_msg
            done
            _astrshell_async_queue=()
        fi
        BUFFER="]"
        CURSOR=1
        _astrshell_apply_style
        zle reset-prompt
        return
    fi

    # Normal shell command
    zle .accept-line
}

zle -N accept-line _astrshell_accept_line

# ── Dynamic prompt style (] mode) ─────────────────────────────────────────
typeset -g _astrshell_bracket_state=-1  # -1=unknown, 0=normal, 1=bracket
typeset -g _astrshell_resetting=0

_astrshell_apply_style() {
    if [[ "${BUFFER}" == \]* ]]; then
        PROMPT="$_astrshell_red_prompt"
        region_highlight=("0 1 bold,fg=203")
    else
        PROMPT="$_astrshell_base_prompt"
        region_highlight=()
    fi
}

# zle-line-init: sync state after ] is pre-filled into BUFFER
_astrshell_zle_line_init() {
    _astrshell_bracket_state=0
    [[ "${BUFFER}" == \]* ]] && _astrshell_bracket_state=1
}
zle -N zle-line-init _astrshell_zle_line_init

# zle-line-pre-redraw: fires before every redraw (typing, history, paste…)
# Updates region_highlight unconditionally; calls reset-prompt only when the
# bracket state crosses the boundary (with a recursion guard).
_astrshell_pre_redraw() {
    (( _astrshell_resetting )) && return
    local _new=0; [[ "${BUFFER}" == \]* ]] && _new=1
    _astrshell_apply_style
    if (( _new != _astrshell_bracket_state )); then
        _astrshell_bracket_state=$_new
        _astrshell_resetting=1
        zle reset-prompt
        _astrshell_resetting=0
    fi
}
zle -N zle-line-pre-redraw _astrshell_pre_redraw


# ── Prompt variants ────────────────────────────────────────────────────────
typeset -g _astrshell_base_prompt="${PROMPT}"
typeset -g _astrshell_red_prompt="%B%F{203}${PROMPT}%f%b"
PROMPT="$_astrshell_base_prompt"

# ── Load user config if present ────────────────────────────────────────────
[[ -f "${ASTRSHELL_DIR:-${HOME}/.astrshell}/userconfig.zsh" ]] && \
    source "${ASTRSHELL_DIR:-${HOME}/.astrshell}/userconfig.zsh"

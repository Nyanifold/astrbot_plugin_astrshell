import asyncio

from .preprocessor import encode_msg
from .recording import RecordingBuffer, RecordingError


async def _write(writer, obj: dict) -> None:
    """Write a message using the wire protocol (RS/US/GS delimiters)."""
    writer.write(encode_msg(obj))
    await writer.drain()


# ---------------------------------------------------------------------------
# Hardcoded English reference for AstrBot built-in slash commands.
# Sourced from AstrBot/astrbot/builtin_stars/builtin_commands/main.py.
# Hidden commands (set, unset, websearch) are intentionally omitted.
# ---------------------------------------------------------------------------

_ASTRBOT_COMMANDS: dict[str, str] = {
    "help":             "Show this help listing.",
    "reset":            "Reset the current LLM conversation.",
    "stop":             "Stop the agent that is currently running in this session.",
    "history":          "View conversation history.  Usage: /history [page]",
    "ls":               "List all conversations.  Usage: /ls [page]",
    "new":              "Create a new conversation.",
    "switch":           "Switch conversation by index from /ls.  Usage: /switch [index]",
    "rename":           "Rename the current conversation.  Usage: /rename <new_name>",
    "del":              "Delete the current conversation.",
    "t2i":              "Toggle text-to-image rendering for this session.",
    "tts":              "Toggle text-to-speech for this session.",
    "sid":              "Show the current session ID and admin ID.",
    "llm":              "[admin] Enable or disable LLM processing.",
    "model":            "[admin] View or switch the active model.  Usage: /model [index|name]",
    "provider":         "[admin] View or switch the active LLM provider.  Usage: /provider [index]",
    "key":              "[admin] View or switch the active API key.  Usage: /key [index]",
    "persona":          "[admin] View or switch the active persona.",
    "op":               "[admin] Grant admin rights.  Usage: /op <admin_id>",
    "deop":             "[admin] Revoke admin rights.  Usage: /deop <admin_id>",
    "wl":               "[admin] Add a session to the whitelist.  Usage: /wl <sid>",
    "dwl":              "[admin] Remove a session from the whitelist.  Usage: /dwl <sid>",
    "groupnew":         "[admin] Create a new group conversation.  Usage: /groupnew <sid>",
    "alter_cmd":        "[admin] Modify command permissions.  Alias: /alter",
    "dashboard_update": "[admin] Update the web dashboard.",
    "plugin":           "Plugin management sub-commands (see //help -a plugin).",
    "plugin ls":        "List all installed plugins.",
    "plugin on":        "Enable a plugin.  Usage: /plugin on <name>",
    "plugin off":       "Disable a plugin.  Usage: /plugin off <name>",
    "plugin get":       "[admin] Install a plugin from a repository URL.  Usage: /plugin get <url>",
    "plugin help":      "Show help and command list for a plugin.  Usage: /plugin help <name>",
}

_ASTRBOT_HELP_OVERVIEW = """\
AstrBot Built-in Commands
==========================
Trigger commands with the default prefix /.
[admin] = requires admin permission.

Conversation
  /reset            Reset the current LLM conversation.
  /stop             Stop the running agent in this session.
  /history [page]   View conversation history.
  /ls [page]        List all conversations.
  /new              Create a new conversation.
  /switch [index]   Switch conversation by /ls index.
  /rename <name>    Rename the current conversation.
  /del              Delete the current conversation.

Display
  /t2i              Toggle text-to-image rendering.
  /tts              Toggle text-to-speech (session level).

Info
  /help             Show this listing.
  /sid              Show session ID and admin ID.

LLM / Provider  [admin]
  /llm              Enable or disable LLM.
  /model [idx|name] View or switch model.
  /provider [idx]   View or switch LLM provider.
  /key [idx]        View or switch API key.
  /persona          View or switch persona.

Admin
  /op <id>          Grant admin rights.
  /deop <id>        Revoke admin rights.
  /wl <sid>         Add session to whitelist.
  /dwl <sid>        Remove session from whitelist.
  /groupnew <sid>   Create a new group conversation.
  /alter_cmd        Modify command permissions.  (alias: /alter)
  /dashboard_update Update the web dashboard.

Plugins
  /plugin ls                    List installed plugins.
  /plugin on/off <name>         Enable/disable a plugin.
  /plugin get <url>  [admin]    Install a plugin.
  /plugin help <name>           Show plugin commands and info.

Use //help -a <cmd> for details on a specific command."""


def _astrbot_help(cmd: str) -> str:
    """Return English help text for an AstrBot command, or the overview if cmd is empty."""
    if not cmd:
        return _ASTRBOT_HELP_OVERVIEW
    key = cmd.lower().strip()
    if key in _ASTRBOT_COMMANDS:
        return f"/{key}  —  {_ASTRBOT_COMMANDS[key]}"
    # try prefix match (e.g. "plugin" shows all plugin sub-commands)
    matches = {k: v for k, v in _ASTRBOT_COMMANDS.items() if k == key or k.startswith(key + " ")}
    if matches:
        lines = [f"Commands matching '{key}':"]
        for k, v in sorted(matches.items()):
            lines.append(f"  /{k}  —  {v}")
        return "\n".join(lines)
    return f"Unknown AstrBot command: '{cmd}'. Use //help -a to see all commands."


async def handle_slash(command: str, args: str,
                       buffer: RecordingBuffer,
                       writer: asyncio.StreamWriter) -> None:
    cmd = command.lower()

    if cmd == "start":
        try:
            buffer.start()
        except RecordingError as e:
            await _write(writer, {"type": "error", "message": str(e)})
            await _write(writer, {"type": "end"})
        return

    if cmd == "end":
        try:
            buffer.end()
        except RecordingError as e:
            await _write(writer, {"type": "error", "message": str(e)})
            await _write(writer, {"type": "end"})
        return

    if cmd == "clear-buffer":
        try:
            buffer.clear()
        except RecordingError as e:
            await _write(writer, {"type": "error", "message": str(e)})
            await _write(writer, {"type": "end"})
        return

    if cmd == "history":
        await _write(writer, {"type": "reply", "agent": "AstrShell",
                               "text": "(history not yet implemented)", "template": "• AstrShell"})
        await _write(writer, {"type": "end"})
        return

    if cmd == "messages":
        await _write(writer, {"type": "reply", "agent": "AstrShell",
                               "text": "(messages not yet implemented)", "template": "• AstrShell"})
        await _write(writer, {"type": "end"})
        return

    if cmd == "help":
        # Parse flags: //help [-a [<cmd>]]
        a_flag = False
        abot_cmd = ""
        remaining = args.strip()
        if remaining.startswith("-a"):
            a_flag = True
            abot_cmd = remaining[2:].strip()

        if a_flag:
            text = _astrbot_help(abot_cmd)
        else:
            text = """\
AstrShell Command Reference
============================

Input prefixes (all start with ] ):
  ]<text>         Send text to AstrBot as a user message.
  ]/<cmd> [args]  Forward /cmd to the AstrBot pipeline (bot slash-command).
  ]//<cmd> [args] AstrShell internal command (see below).

  ]!              Run the next command; send its stdout/stderr/exit-code to AstrBot.
  ]$              Run the next command; send its stdout to AstrBot.
  ]#<note>        Append a comment/note to the recording buffer (silent).
  ]#!             Run the next command; record it in the buffer (silent, not sent to AstrBot).
  ]#$             Run the next command; record stdout in the buffer (silent).
  ]##[note]       Flush the recording buffer and send its contents to AstrBot.

Async modifier:
  ]&<prefix>      Any of the above with & makes the request asynchronous — the shell
                  prompt returns immediately while AstrBot processes in the background.
                  Example: ]&! make build

AstrShell internal commands (]//<cmd>):
  //help          Show this help message.
  //help -a       Show AstrBot built-in commands.
  //help -a <cmd> Show help for a specific AstrBot command.
  //status        Show daemon status (PID, uptime, connections, log file).
  //start         Start a recording session (begin buffering interactions).
  //end           End the recording session (stop buffering; buffer is preserved).
  //clear-buffer  Clear the recording buffer without sending it.
  //restart       Request an AstrBot daemon restart.
  //history       (not yet implemented)
  //messages      (not yet implemented)"""
        await _write(writer, {"type": "reply", "agent": "AstrShell",
                               "text": text, "template": "• AstrShell"})
        await _write(writer, {"type": "end"})
        return

    if cmd == "restart":
        # In plugin mode AstrBot manages its own lifecycle; daemon restart is not available.
        await _write(writer, {"type": "error", "message": "Restart not available in plugin mode"})
        await _write(writer, {"type": "end"})
        return

    await _write(writer, {"type": "error", "message": f"Unknown command: /{command}"})
    await _write(writer, {"type": "end"})

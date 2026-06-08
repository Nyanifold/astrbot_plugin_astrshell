"""
Wire protocol codec and input token parser for AstrShell.

Consolidates:
  - astrshell/protocol.py  (encode_msg, read_msg)  → AstrshellProtocolParser + encode_msg()
  - astrshell/parser.py    (parse_input)            → parse_input()
"""
import asyncio
import re
from typing import Any

# ── Wire protocol constants ────────────────────────────────────────────────
_RS = b"\x1e"   # Record Separator — separates key-value fields
_US = b"\x1f"   # Unit Separator   — separates key from value
_GS = b"\x1d"   # Group Separator  — terminates a complete message

_INT_FIELDS: frozenset[str] = frozenset({"exit_code"})


class AstrshellProtocolParser:
    """Stateless wire codec. All methods are static; no instance needed."""

    # ── Inbound ───────────────────────────────────────────────────────────

    @staticmethod
    async def read_frame(reader: asyncio.StreamReader) -> dict:
        """Read one RS/US/GS frame from the stream.

        Returns a decoded dict. Raises EOFError if the connection closed.
        FROM: astrshell/protocol.py read_msg()
        """
        try:
            raw = await reader.readuntil(_GS)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            raise EOFError
        result: dict = {}
        for pair in raw[:-1].split(_RS):
            if not pair:
                continue
            if _US in pair:
                k_b, v_b = pair.split(_US, 1)
                k = k_b.decode()
                result[k] = int(v_b) if k in _INT_FIELDS else v_b.decode()
        return result

    # ── Outbound ──────────────────────────────────────────────────────────

    @staticmethod
    def encode_ready() -> bytes:
        return AstrshellProtocolParser._encode_frame({"type": "ready"})

    @staticmethod
    def encode_reply(text: str, req_id: str = "", async_mode: bool = False) -> bytes:
        d: dict = {"type": "reply", "text": text}
        if req_id:
            d["id"] = req_id
        if async_mode:
            d["async"] = True
        return AstrshellProtocolParser._encode_frame(d)

    @staticmethod
    def encode_end(req_id: str = "", async_mode: bool = False) -> bytes:
        d: dict = {"type": "end"}
        if req_id:
            d["id"] = req_id
        if async_mode:
            d["async"] = True
        return AstrshellProtocolParser._encode_frame(d)

    @staticmethod
    def encode_error(message: str, req_id: str = "", async_mode: bool = False) -> bytes:
        d: dict = {"type": "error", "message": message}
        if req_id:
            d["id"] = req_id
        if async_mode:
            d["async"] = True
        return AstrshellProtocolParser._encode_frame(d)

    @staticmethod
    def encode_push(text: str) -> bytes:
        return AstrshellProtocolParser._encode_frame({"type": "push", "text": text})

    # ── Internal ──────────────────────────────────────────────────────────

    @staticmethod
    def _encode_frame(fields: dict) -> bytes:
        """Encode a dict as RS/US/GS wire frame.

        FROM: astrshell/protocol.py encode_msg()
        """
        parts: list[bytes] = []
        for k, v in fields.items():
            if isinstance(v, bool):
                v_enc = b"true" if v else b"false"
            else:
                v_enc = str(v).encode()
            parts.append(_RS + k.encode() + _US + v_enc)
        return b"".join(parts) + _GS


# ── Module-level alias ─────────────────────────────────────────────────────
# Used by events.py and slash.py which call encode_msg() directly.

def encode_msg(fields: dict) -> bytes:
    """Alias for AstrshellProtocolParser._encode_frame(). FROM: protocol.py."""
    return AstrshellProtocolParser._encode_frame(fields)


# ── Input token parser ─────────────────────────────────────────────────────
# FROM: astrshell/parser.py (moved verbatim, no logic changes)

_TOKEN_RE = re.compile(r"^\]&?((?:\s*[#!$/])*)(.*)", re.DOTALL)


def _extract_token(raw: str) -> tuple[str, str]:
    m = _TOKEN_RE.match(raw)
    if not m:
        return "", raw[1:].lstrip()
    token_raw, rest = m.group(1), m.group(2)
    token = re.sub(r"\s", "", token_raw)
    body = rest.lstrip()
    return token, body


def parse_input(msg: dict[str, Any]) -> dict[str, Any]:
    """Parse a decoded wire message into a typed dict with a 'kind' field.

    FROM: astrshell/parser.py parse_input() — moved verbatim.
    """
    match msg.get("type"):
        case "record_cmd":
            return {"kind": "record_cmd", "cmd": msg["cmd"], "exit_code": msg.get("exit_code", 0)}
        case "stop":
            return {"kind": "stop"}
        case "input":
            pass
        case _:
            return {"kind": "unknown", "raw": msg}

    raw: str = msg.get("raw", "")
    token, body = _extract_token(raw)

    match token:
        case "##":
            return {"kind": "double_hash", "note": body}
        case "#!":
            return {
                "kind": "hash_bang",
                "cmd": msg.get("cmd", ""),
                "stdout": msg.get("stdout", ""),
                "stderr": msg.get("stderr", ""),
                "exit_code": msg.get("exit_code", 0),
            }
        case "#$":
            return {"kind": "hash_dollar", "cmd": msg.get("cmd", ""), "body": msg.get("stdout", "")}
        case "#":
            return {"kind": "hash", "body": body}
        case "!":
            return {
                "kind": "bang",
                "cmd": msg.get("cmd", ""),
                "stdout": msg.get("stdout", ""),
                "stderr": msg.get("stderr", ""),
                "exit_code": msg.get("exit_code", 0),
            }
        case "$":
            return {"kind": "dollar", "cmd": msg.get("cmd", ""), "body": msg.get("stdout", "")}
        case "/" | "//" as slash_token:
            parts = body.split(None, 1)
            return {
                "kind": "slash" if slash_token == "/" else "astr_slash",
                "command": parts[0] if parts else "",
                "args": parts[1] if len(parts) > 1 else "",
            }
        case _:
            return {"kind": "text", "body": body}


def truncate_output(text: str, max_head: int, max_tail: int) -> str:
    lines = text.splitlines()
    threshold = int(1.5 * (max_head + max_tail))
    if len(lines) < threshold:
        return text
    omitted = len(lines) - max_head - max_tail
    return "\n".join(lines[:max_head]
                     + [f"<...{omitted} lines omitted...>"]
                     + lines[-max_tail:])

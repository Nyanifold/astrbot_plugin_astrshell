"""AstrShell platform adapter for AstrBot.

FROM: astrshell/platform/adapter.py — restructured.
Removed: AstrBotCoreLifecycle calls, queue-based run() loop,
         direct protocol.py/parser.py imports, init/init_info emission,
         shutdown kind handling.
Added: _handle_connection() per-connection async loop,
       run() via ConnectionManager.start() + serve_forever().
"""
import asyncio
import os
import uuid

from astrbot.api import logger
from astrbot.core.platform import Platform, PlatformMetadata
from astrbot.core.platform.astrbot_message import AstrBotMessage
from astrbot.core.platform.message_session import MessageSesion
from astrbot.core.platform.register import register_platform_adapter
from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot.core.message.message_event_result import MessageChain

from .preprocessor import AstrshellProtocolParser, encode_msg, parse_input, truncate_output
from .connection import ConnectionManager, _is_tcp
from .recording import RecordingBuffer, RecordingError
from .events import (
    ShellMessageEvent, ShellCommandMessageEvent, RecordingFileMessageEvent,
    _make_msg_obj,
)



@register_platform_adapter(
    "astrshell",
    "AstrShell terminal adapter",
    default_config_tmpl={
        "socket_path": "~/.astrshell/daemon.sock",
        "pipeline_register_timeout": 5.0,
        "pipeline_poll_interval": 0.05,
        "max_head_lines": 100,
        "max_tail_lines": 100,
    },
)
class ShellPlatformAdapter(Platform):

    def __init__(self, platform_config: dict, platform_settings: dict,
                 event_queue: asyncio.Queue) -> None:
        super().__init__(platform_config, event_queue)
        self._socket_path: str = platform_config.get("socket_path", "~/.astrshell/daemon.sock")
        self._pipeline_register_timeout: float = float(
            platform_config.get("pipeline_register_timeout", 5.0))
        self._pipeline_poll_interval: float = float(
            platform_config.get("pipeline_poll_interval", 0.05))
        self._max_head_lines: int = int(platform_config.get("max_head_lines", 100))
        self._max_tail_lines: int = int(platform_config.get("max_tail_lines", 100))
        self._conn_mgr = ConnectionManager()
        self._buffers: dict[str, RecordingBuffer] = {}
        self._cwds: dict[str, str] = {}
        self._session_umo: str = ""
        self._bg_tasks: set[asyncio.Task] = set()

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            name="astrshell",
            description="AstrShell terminal adapter",
            id="astrshell",
            support_proactive_message=True,
        )

    # ── UDS server lifecycle ───────────────────────────────────────────────

    async def run(self) -> None:
        """Start server (UDS or TCP) and serve connections until cancelled."""
        if _is_tcp(self._socket_path):
            socket_path = self._socket_path
            logger.debug(f"run: starting TCP server at {socket_path}")
        else:
            socket_path = os.path.expanduser(self._socket_path)
            os.makedirs(os.path.dirname(socket_path), exist_ok=True)
            logger.debug(f"run: starting UDS server at {socket_path}")
        await self._conn_mgr.start(socket_path, self._handle_connection)
        try:
            await self._conn_mgr.serve_forever()
        except asyncio.CancelledError:
            logger.debug("run: cancelled, closing")
            await self._conn_mgr.close()
            raise

    async def _handle_connection(self, reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter) -> None:
        """Handle one UDS client: handshake → ready → dispatch loop."""
        conn_id = str(uuid.uuid4())

        # Step 1: Read connect handshake frame
        try:
            raw = await AstrshellProtocolParser.read_frame(reader)
        except EOFError:
            writer.close()
            return

        if raw.get("type") != "connect":
            writer.close()
            return

        session_id = raw.get("session_id", "unknown")
        logger.debug(f"_handle_connection: connected conn_id={conn_id[:8]} session={session_id}")

        # Step 2: Register and immediately send ready
        self._conn_mgr.register(conn_id, session_id, writer)
        writer.write(AstrshellProtocolParser.encode_ready())
        await writer.drain()

        # Step 3: Message dispatch loop
        try:
            while True:
                try:
                    raw = await AstrshellProtocolParser.read_frame(reader)
                except EOFError:
                    break

                if raw.get("type") == "disconnect":
                    break

                req_id = raw.get("id", "")
                async_mode = raw.get("async") == "true"
                cwd = raw.get("cwd", "")
                cwd_changed = bool(cwd and cwd != self._cwds.get(conn_id, ""))
                if cwd_changed:
                    self._cwds[conn_id] = cwd

                parsed = parse_input(raw)
                logger.debug(f"_handle_connection: dispatching kind={parsed.get('kind')} conn_id={conn_id[:8]}")
                await self._dispatch(parsed, conn_id=conn_id, session_id=session_id,
                                     req_id=req_id, async_mode=async_mode,
                                     cwd_changed=cwd_changed)
        finally:
            logger.debug(f"_handle_connection: disconnected conn_id={conn_id[:8]}")
            self._conn_mgr.unregister(conn_id)
            if conn_id in self._buffers:
                del self._buffers[conn_id]
            self._cwds.pop(conn_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    # ── Writer helpers ─────────────────────────────────────────────────────

    class _ConnWriter:
        """Buffers bytes and flushes via asyncio.StreamWriter."""
        def __init__(self, writer: asyncio.StreamWriter):
            self._writer = writer
            self._buf = bytearray()

        def write(self, data: bytes) -> None:
            self._buf.extend(data)

        async def drain(self) -> None:
            if self._buf:
                self._writer.write(bytes(self._buf))
                await self._writer.drain()
                self._buf.clear()

    class _BroadcastWriter:
        """Broadcasts to all connections via ConnectionManager."""
        def __init__(self, conn_mgr: ConnectionManager):
            self._conn_mgr = conn_mgr
            self._buf = bytearray()

        def write(self, data: bytes) -> None:
            self._buf.extend(data)

        async def drain(self) -> None:
            if self._buf:
                await self._conn_mgr.broadcast(bytes(self._buf))
                self._buf.clear()

    # ── send_by_session (proactive messages from AstrBot) ─────────────────

    async def send_by_session(self, session: "MessageSesion",
                               message_chain: "MessageChain") -> None:
        """Broadcast a proactive message to all shells."""
        logger.debug("send_by_session: broadcasting proactive message")
        writer = self._BroadcastWriter(self._conn_mgr)
        await ShellMessageEvent.send_message_chain(
            message_chain, writer, req_id="", async_mode=True, render_markdown=True,
        )

    # ── Internal helpers ───────────────────────────────────────────────────

    def _get_buffer(self, conn_id: str) -> RecordingBuffer:
        if conn_id not in self._buffers:
            self._buffers[conn_id] = RecordingBuffer()
        return self._buffers[conn_id]

    async def _ack(self, conn_id_or_writer, recording: bool | None = None,
                   req_id: str = "") -> None:
        obj: dict = {"type": "ack"}
        if recording is not None:
            obj["recording"] = recording
        if req_id:
            obj["id"] = req_id
        writer = conn_id_or_writer
        writer.write(encode_msg(obj))
        await writer.drain()

    async def _error(self, message: str, writer, req_id: str = "",
                     async_mode: bool = False) -> None:
        err_obj: dict = {"type": "error", "message": message}
        end_obj: dict = {"type": "end"}
        if req_id:
            err_obj["id"] = req_id
            end_obj["id"] = req_id
        if async_mode:
            err_obj["async"] = True
            end_obj["async"] = True
        writer.write(encode_msg(err_obj))
        writer.write(encode_msg(end_obj))
        await writer.drain()

    def convert_message(self, parsed: dict, conn_id: str,
                        cwd_changed: bool = False) -> AstrBotMessage | None:
        kind = parsed["kind"]
        buffer = self._get_buffer(conn_id)

        if kind == "text":
            body = parsed["body"]
            if buffer.recording:
                buffer.append_user_message(body)
            msg = body
            cwd = self._cwds.get(conn_id, "")
            if cwd_changed and cwd:
                msg += f"\n\ncurrent working directory: {cwd}"
            return _make_msg_obj(msg, "astrshell")

        elif kind == "dollar":
            body = parsed["body"]
            if buffer.recording:
                buffer.append_eval_message(parsed["cmd"], body)
            msg = body
            cwd = self._cwds.get(conn_id, "")
            if cwd_changed and cwd:
                msg += f"\n\ncurrent working directory: {cwd}"
            return _make_msg_obj(msg, "astrshell")

        elif kind == "bang":
            if buffer.recording:
                buffer.append_command_message(
                    parsed["cmd"], parsed["stdout"], parsed["stderr"], parsed["exit_code"])
            parts = [f"$ {parsed['cmd']}", parsed["stdout"]]
            if parsed["stderr"]:
                parts.append(parsed["stderr"])
            parts.append(f"exit: {parsed['exit_code']}")
            msg = "\n".join(p for p in parts if p)
            cwd = self._cwds.get(conn_id, "")
            if cwd_changed and cwd:
                msg += f"\n\ncurrent working directory: {cwd}"
            return _make_msg_obj(msg, "astrshell")

        elif kind == "double_hash":
            entries = buffer.flush(note=parsed["note"])
            from .recording import RecordingBuffer as _RB
            msg = _RB().serialize(entries)
            abm = _make_msg_obj(msg, "astrshell")
            abm._entries = entries
            return abm

        elif kind == "astr_cmd":
            return _make_msg_obj(parsed["message_str"], "astrshell")

        return None

    async def handle_msg(self, message: AstrBotMessage, writer,
                         conn_id: str, session_id: str,
                         req_id: str = "", kind: str = "",
                         parsed: dict | None = None,
                         async_mode: bool = False,
                         cwd_changed: bool = False) -> None:
        render = kind != "astr_cmd"

        if kind == "bang":
            assert parsed is not None
            event: ShellMessageEvent = ShellCommandMessageEvent(
                command=parsed["cmd"], stdout=parsed["stdout"],
                stderr=parsed["stderr"], exit_code=parsed["exit_code"],
                session_id=session_id, writer=writer,
                render_markdown=render,
                cwd=self._cwds.get(conn_id, "") if cwd_changed else "",
            )
        elif kind == "double_hash":
            entries = getattr(message, "_entries", [])
            event = RecordingFileMessageEvent(
                entries=entries, session_id=session_id, writer=writer,
                render_markdown=render, cwd=self._cwds.get(conn_id, "") if cwd_changed else "",
            )
        else:
            event = ShellMessageEvent(
                message_str=message.message_str, session_id=session_id,
                writer=writer, render_markdown=render,
            )

        if req_id:
            event.set_extra("req_id", req_id)
        event.set_extra("async_mode", async_mode)
        self.commit_event(event)
        if not self._session_umo:
            self._session_umo = event.unified_msg_origin
        task = asyncio.create_task(
            self._send_end_on_done(event, req_id, writer, async_mode=async_mode))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _send_end_on_done(self, event: ShellMessageEvent, req_id: str,
                                 writer, async_mode: bool = False) -> None:
        if not req_id:
            return
        await asyncio.sleep(0)
        _reg_iters = max(1, int(self._pipeline_register_timeout / self._pipeline_poll_interval))
        for _ in range(_reg_iters):
            if event in active_event_registry._events.get(
                    event.unified_msg_origin, set()):
                break
            await asyncio.sleep(self._pipeline_poll_interval)
        while event in active_event_registry._events.get(
                event.unified_msg_origin, set()):
            await asyncio.sleep(self._pipeline_poll_interval)

        end_obj: dict = {"type": "end", "id": req_id}
        if async_mode:
            end_obj["async"] = True
        writer.write(encode_msg(end_obj))
        await writer.drain()

    async def _dispatch(self, parsed: dict, conn_id: str, session_id: str,
                        req_id: str = "", async_mode: bool = False,
                        cwd_changed: bool = False) -> None:
        kind = parsed["kind"]
        buffer = self._get_buffer(conn_id)

        # Get a writer for this connection
        raw_writer = self._conn_mgr.get_writer(conn_id)
        if raw_writer is None:
            return
        writer = self._ConnWriter(raw_writer)

        if kind in ("bang", "hash_bang"):
            parsed = {**parsed,
                      "stdout": truncate_output(
                          parsed.get("stdout", ""), self._max_head_lines, self._max_tail_lines),
                      "stderr": truncate_output(
                          parsed.get("stderr", ""), self._max_head_lines, self._max_tail_lines)}

        if kind == "record_cmd":
            if buffer.recording:
                buffer.append_cmd(parsed["cmd"], parsed["exit_code"])
            await self._ack(writer, recording=buffer.recording, req_id=req_id)
            return

        if kind == "hash":
            buffer.append_comment(parsed["body"])
            await self._ack(writer, recording=buffer.recording, req_id=req_id)
            return

        if kind == "hash_bang":
            buffer.append_comment_cmd(
                parsed["cmd"], parsed["stdout"], parsed["stderr"], parsed["exit_code"])
            await self._ack(writer, recording=buffer.recording, req_id=req_id)
            return

        if kind == "hash_dollar":
            buffer.append_comment_dollar(parsed["cmd"], parsed["body"])
            await self._ack(writer, recording=buffer.recording, req_id=req_id)
            return

        if kind == "slash":
            cmd, args = parsed["command"], parsed["args"]
            msg_str = "/" + cmd + (" " + args if args else "")
            abm = self.convert_message({"kind": "astr_cmd", "message_str": msg_str}, conn_id)
            if abm:
                await self.handle_msg(abm, writer=writer, conn_id=conn_id,
                                      session_id=session_id, req_id=req_id,
                                      kind="astr_cmd", async_mode=async_mode)
            return

        if kind == "astr_slash":
            await self._handle_slash(parsed["command"], parsed["args"],
                                     writer=writer, req_id=req_id, conn_id=conn_id)
            return

        if kind == "stop":
            await self._handle_stop(writer=writer, req_id=req_id)
            return

        # Pipeline-triggering kinds: text, bang, dollar, double_hash
        try:
            abm = self.convert_message(parsed, conn_id, cwd_changed=cwd_changed)
        except RecordingError as e:
            await self._error(str(e), writer=writer, req_id=req_id, async_mode=async_mode)
            return
        if abm:
            await self.handle_msg(abm, writer=writer, conn_id=conn_id,
                                  session_id=session_id, req_id=req_id,
                                  kind=kind, parsed=parsed,
                                  async_mode=async_mode, cwd_changed=cwd_changed)

    async def _handle_slash(self, command: str, args: str,
                             writer, req_id: str = "", conn_id: str = "") -> None:
        cmd = command.lower()
        if cmd == "status":
            status = self._conn_mgr.get_status()
            text = (f"AstrShell Status\n================\n"
                    f"PID: {status['pid']}\n"
                    f"Uptime: {status['uptime_seconds']} seconds\n"
                    f"Active connections: {status['connection_count']}")
            writer.write(encode_msg({"type": "reply", "text": text}))
            await writer.drain()
            await self._ack(writer, req_id=req_id)
            return

        from .slash import handle_slash
        buffer = self._get_buffer(conn_id)
        await handle_slash(command, args, buffer, writer)
        await self._ack(writer, recording=buffer.recording, req_id=req_id)

    async def _handle_stop(self, writer, req_id: str = "") -> None:
        umo = self._session_umo or "astrshell:FriendMessage:astrshell"
        count = active_event_registry.request_agent_stop_all(umo)
        text = "Stopped." if count > 0 else "No active task."
        reply_obj: dict = {"type": "reply", "text": text}
        end_obj: dict = {"type": "end"}
        if req_id:
            reply_obj["id"] = req_id
            end_obj["id"] = req_id
        writer.write(encode_msg(reply_obj))
        writer.write(encode_msg(end_obj))
        await writer.drain()

    async def terminate(self) -> None:
        await self._conn_mgr.close()

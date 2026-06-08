import time
import uuid

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

from .formatter import format_reply
from .preprocessor import encode_msg
from .recording import RecordingBuffer, RecordingEntry


_PLATFORM_META = PlatformMetadata(
    name="astrshell",
    description="AstrShell terminal adapter",
    id="astrshell",
    support_proactive_message=True,
)


def _make_msg_obj(message_str: str, session_id: str) -> AstrBotMessage:
    obj = AstrBotMessage()
    obj.type = MessageType.FRIEND_MESSAGE
    obj.self_id = "astrshell"
    obj.session_id = session_id
    obj.message_id = uuid.uuid4().hex
    obj.sender = MessageMember(user_id=session_id, nickname="user")
    obj.message = [Plain(message_str)]
    obj.message_str = message_str
    obj.raw_message = message_str
    obj.timestamp = int(time.time())
    return obj


class ShellMessageEvent(AstrMessageEvent):
    """Plain text or ]$ stdout message."""

    def __init__(self, *, message_str: str, session_id: str, writer=None,
                 render_markdown: bool = True):
        msg_obj = _make_msg_obj(message_str, session_id)
        super().__init__(message_str, msg_obj, _PLATFORM_META, session_id)
        self._writer = writer
        self._render_markdown = render_markdown
        self._sent_once = False

    async def send(self, message: MessageChain) -> None:
        req_id = self.get_extra("req_id", "")
        async_mode = self.get_extra("async_mode", False)
        logger.debug(f"ShellMessageEvent.send called: req_id={req_id}, async={async_mode}, writer={self._writer is not None}")
        if message is None:
            logger.debug("ShellMessageEvent.send: message is None, returning")
            return
        if self._writer is not None:
            formatted = list(format_reply(message, req_id=req_id,
                                    render_markdown=self._render_markdown,
                                    show_header=async_mode or not self._sent_once,
                                    async_mode=async_mode))
            logger.debug(f"ShellMessageEvent.send: formatted {len(formatted)} messages")
            for msg in formatted:
                logger.debug(f"ShellMessageEvent.send: writing msg type={msg.get('type')} id={msg.get('id')} len={len(encode_msg(msg))}")
                self._writer.write(encode_msg(msg))
            logger.debug("ShellMessageEvent.send: calling writer.drain()")
            await self._writer.drain()
            logger.debug("ShellMessageEvent.send: writer.drain() done")
        else:
            logger.debug("ShellMessageEvent.send: NO WRITER!")
        self._sent_once = True
        await super().send(message)

    @staticmethod
    async def send_message_chain(
        message: MessageChain,
        writer,
        req_id: str = "",
        async_mode: bool = False,
        render_markdown: bool = True,
    ) -> None:
        if message is None or writer is None:
            return
        for msg in format_reply(message, req_id=req_id,
                                render_markdown=render_markdown,
                                show_header=True,
                                async_mode=async_mode):
            writer.write(encode_msg(msg))
        await writer.drain()


class ShellCommandMessageEvent(ShellMessageEvent):
    """Carries ]! command + captured output."""

    def __init__(self, *, command: str, stdout: str, stderr: str,
                 exit_code: int, session_id: str, writer=None,
                 render_markdown: bool = True, cwd: str = ""):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        parts = [f"$ {command}", stdout]
        if stderr:
            parts.append(stderr)
        parts.append(f"exit: {exit_code}")
        message_str = "\n".join(p for p in parts if p)
        if cwd:
            message_str += f"\n\ncurrent working directory: {cwd}"
        super().__init__(message_str=message_str, session_id=session_id,
                         writer=writer, render_markdown=render_markdown)


class RecordingFileMessageEvent(ShellMessageEvent):
    """Carries a full recording buffer sent via ]##."""

    def __init__(self, *, entries: list[RecordingEntry], session_id: str, writer=None,
                 render_markdown: bool = True, cwd: str = ""):
        self.entries = entries
        buf = RecordingBuffer()
        message_str = buf.serialize(entries)
        if cwd:
            message_str += f"\n\ncurrent working directory: {cwd}"
        super().__init__(message_str=message_str, session_id=session_id,
                         writer=writer, render_markdown=render_markdown)

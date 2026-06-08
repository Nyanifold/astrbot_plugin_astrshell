from dataclasses import dataclass
from typing import Literal

EntryType = Literal[
    "recording-start", "recording-end", "cmd", "note",
    "user-message", "command-message", "command-result",
    "eval-message", "evaluated-message", "agent-message",
    "comment-message",
    "comment-command-message", "comment-command-result",
    "comment-eval-message", "comment-evaluated-message",
]


@dataclass
class RecordingEntry:
    type: EntryType
    content: str = ""
    stderr: str | None = None
    exit_code: int | None = None


class RecordingError(Exception):
    pass


class RecordingBuffer:
    def __init__(self):
        self.entries: list[RecordingEntry] = []
        self.recording: bool = False

    def start(self):
        if self.recording:
            raise RecordingError("already in recording mode — use /end first")
        self.entries.append(RecordingEntry(type="recording-start"))
        self.recording = True

    def end(self):
        if not self.recording:
            raise RecordingError("not in recording mode — use /start first")
        self.entries.append(RecordingEntry(type="recording-end"))
        self.recording = False

    def append_cmd(self, cmd: str, exit_code: int):
        self.entries.append(RecordingEntry(type="cmd", content=cmd, exit_code=exit_code))

    def append_user_message(self, text: str):
        self.entries.append(RecordingEntry(type="user-message", content=text))

    def append_agent_message(self, text: str):
        self.entries.append(RecordingEntry(type="agent-message", content=text))

    def append_comment(self, text: str):
        self.entries.append(RecordingEntry(type="comment-message", content=text))

    def append_comment_cmd(self, cmd: str, stdout: str, stderr: str, exit_code: int):
        self.entries.append(RecordingEntry(type="comment-command-message", content=cmd))
        self.entries.append(RecordingEntry(type="comment-command-result",
                                           content=stdout, stderr=stderr or None,
                                           exit_code=exit_code))

    def append_comment_dollar(self, cmd: str, stdout: str):
        self.entries.append(RecordingEntry(type="comment-eval-message", content=cmd))
        self.entries.append(RecordingEntry(type="comment-evaluated-message", content=stdout))

    def append_command_message(self, cmd: str, stdout: str, stderr: str, exit_code: int):
        self.entries.append(RecordingEntry(type="command-message", content=cmd))
        self.entries.append(RecordingEntry(type="command-result",
                                           content=stdout, stderr=stderr or None,
                                           exit_code=exit_code))

    def append_eval_message(self, cmd: str, stdout: str):
        self.entries.append(RecordingEntry(type="eval-message", content=cmd))
        self.entries.append(RecordingEntry(type="evaluated-message", content=stdout))

    def flush(self, note: str) -> list[RecordingEntry]:
        if self.recording:
            raise RecordingError("must /end recording before sending with ]##")
        if not self.entries:
            raise RecordingError("buffer is empty — nothing to send")
        if note:
            self.entries.append(RecordingEntry(type="note", content=note))
        result = list(self.entries)
        self.entries.clear()
        return result

    def clear(self):
        if self.recording:
            raise RecordingError("must /end recording before /clear-buffer")
        self.entries.clear()

    def serialize(self, entries: list[RecordingEntry]) -> str:
        """Serialize entries into a human-readable string for message_str."""
        lines = []
        for e in entries:
            if e.type in ("recording-start", "recording-end"):
                lines.append(f"[{e.type}]")
            elif e.type == "cmd":
                lines.append(f"[cmd] {e.content}  exit:{e.exit_code}")
            elif e.type == "note":
                lines.append(f"[note] {e.content}")
            elif e.type == "command-result":
                body = e.content
                if e.stderr:
                    body += f"\n{e.stderr}"
                lines.append(f"[command-result] {body}  exit:{e.exit_code}")
            else:
                lines.append(f"[{e.type}] {e.content}")
        return "\n".join(lines)

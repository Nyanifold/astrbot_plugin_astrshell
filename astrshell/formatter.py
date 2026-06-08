from io import StringIO

from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.text import Text

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain


def format_reply(message: MessageChain, agent_name: str = "AstrShell",
                 req_id: str = "", render_markdown: bool = True,
                 show_header: bool = True,
                 async_mode: bool = False) -> list[dict]:
    """Convert a MessageChain into a list of reply dicts for the wire protocol."""
    text_parts = []
    for comp in message.chain:
        if isinstance(comp, Plain):
            text_parts.append(comp.text)

    full_text = "".join(text_parts)

    rule_style = "bold" if async_mode else "dim"

    sio = StringIO()
    console = Console(file=sio, force_terminal=True, highlight=False)
    if show_header:
        console.print(Rule(style=rule_style))
        console.print(Text(f"• {agent_name}", style="bold"))
        console.print(Rule(style="dim"))
    if full_text:
        if render_markdown:
            console.print(Markdown(full_text))
        else:
            console.print(full_text, markup=False, highlight=False)
    console.print(Rule(style=rule_style))

    reply: dict = {
        "type": "reply",
        "agent": agent_name,
        "text": sio.getvalue(),
    }
    if req_id:
        reply["id"] = req_id
    if async_mode:
        reply["async"] = True
    return [reply]

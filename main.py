from astrbot.api.star import Context, Star, register


@register("astrshell", "Nyanifold", "AstrShell Terminal Adapter", "0.2.0")
class AstrShellPlugin(Star):
    def __init__(self, context: Context):
        from .astrshell.adapter import ShellPlatformAdapter  # noqa
        # import triggers @register_platform_adapter registration

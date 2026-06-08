from astrbot.api.star import Context, Star, register


@register("astrshell", "Nyanifold", "AstrShell Terminal Adapter", "0.2.1")
class AstrShellPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        from .astrshell.adapter import ShellPlatformAdapter  # noqa
        # import triggers @register_platform_adapter registration

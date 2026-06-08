"""Console scripts for astrshell.

Registered in pyproject.toml [project.scripts]:
  astrshell       → launch_shell()
  astrshell-setup → setup()
"""
import importlib.resources
import os
import pathlib


def launch_shell() -> None:
    """Launch a new zsh terminal with astrshell pre-sourced.

    Sets ZDOTDIR to the package's zsh/ directory so that zsh reads
    astrshell/zsh/.zshrc instead of ~/.zshrc.
    astrshell/zsh/.zshrc sources ~/.zshrc first (no --skip-zshrc
    flag in this path), preserving the user's full environment.
    """
    zsh_dir = str(importlib.resources.files("astrshell") / "zsh")
    env = os.environ.copy()
    env["ZDOTDIR"] = zsh_dir
    os.execvpe("zsh", ["zsh"], env)


def setup() -> None:
    """Add astrshell source line to ~/.zshrc (one-time setup).

    Appends:
        source "/path/to/astrshell/zsh/.zshrc" --skip-zshrc

    The --skip-zshrc flag prevents recursive sourcing since ~/.zshrc
    is already being executed when this line runs.
    """
    zsh_dir = str(importlib.resources.files("astrshell") / "zsh")
    zsh_script = f"{zsh_dir}/.zshrc"
    zshrc = pathlib.Path.home() / ".zshrc"

    existing = zshrc.read_text(errors="ignore") if zshrc.exists() else ""
    if "# astrshell" in existing:
        print("astrshell already configured in ~/.zshrc")
        return

    line = f'\nsource "{zsh_script}" --skip-zshrc  # astrshell\n'
    try:
        with zshrc.open("a") as f:
            f.write(line)
    except OSError as e:
        print(f"Could not write to {zshrc}: {e}")
        print(f"Add the following line manually:\n  {line.strip()}")
        return
    print(f"Added to {zshrc}. Restart your shell or run: source ~/.zshrc")

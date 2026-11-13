import argparse
import re

from .language import language
from .text_layout import terminal_width, text_width, wrap_line, wrap_rows


class TerminalHelpFormatter(argparse.HelpFormatter):
    def __init__(self, prog):
        width = terminal_width()
        help_position = 4 if width < 48 else min(24, width // 3)
        super().__init__(prog, width=width, max_help_position=help_position)

    def _format_usage(self, usage, actions, groups, prefix=None):
        if prefix is None:
            prefix = "使い方: " if language.get() == "ja" else "usage: "
        if prefix == "":
            return super()._format_usage(usage, actions, groups, prefix)
        # Ask argparse for the syntax, then pack complete argument groups into rows.
        width = self._width
        try:
            self._width = max(width, 4096)
            syntax = super()._format_usage(usage, actions, groups, "").strip()
        finally:
            self._width = width
        parts = re.findall(r"\[[^\[\]]*\]|\{[^{}]*\}|\([^()]*\)|\S+", syntax)
        lines = []
        line = prefix.rstrip()
        for part in parts:
            candidate = f"{line} {part}"
            if text_width(candidate) <= width:
                line = candidate
                continue
            lines.extend(wrap_line(line, width))
            wrapped = list(wrap_line(part, width, "  "))
            lines.extend(wrapped[:-1])
            line = wrapped[-1] if wrapped else ""
        lines.extend(wrap_line(line, width))
        return "\n".join(lines) + "\n\n"

    def _split_lines(self, text, width):
        return list(wrap_line(" ".join(text.split()), max(1, width)))

    def _fill_text(self, text, width, indent):
        return "\n".join(wrap_line(" ".join(text.split()), max(1, width) + text_width(indent), indent))

    def format_help(self):
        return "\n".join(wrap_rows(super().format_help(), self._width)) + "\n"


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        japanese = language.get() == "ja"
        kwargs.setdefault("formatter_class", TerminalHelpFormatter)
        super().__init__(*args, **kwargs)
        if japanese:
            self._positionals.title = "位置引数"
            self._optionals.title = "オプション"
            if self.add_help:
                self._option_string_actions["--help"].help = "このヘルプを表示して終了"


def parser_candidates(parser):
    commands = {}
    options = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            descriptions = {action.choices[entry.dest]: entry.help for entry in action._choices_actions}
            commands.update(
                (name, (child, descriptions.get(child)))
                for name, child in action.choices.items() if not name.startswith("_")
            )
        elif action.help != argparse.SUPPRESS:
            options.update((name, action) for name in action.option_strings)
    return commands, options

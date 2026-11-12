import argparse

from .language import language


class JapaneseHelpFormatter(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix=None):
        if prefix is None:
            prefix = "使い方: "
        return super()._format_usage(usage, actions, groups, prefix)


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        japanese = language.get() == "ja"
        if japanese:
            kwargs.setdefault("formatter_class", JapaneseHelpFormatter)
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

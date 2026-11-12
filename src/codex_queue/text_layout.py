import shutil
import unicodedata


def terminal_width():
    # Leave the final column free so writing a line cannot scroll the screen.
    return max(1, shutil.get_terminal_size().columns - 1)


def cell_width(char):
    if unicodedata.category(char) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def text_width(text):
    return sum(map(cell_width, text))


def fit(text, width, tail=False):
    result = []
    for char in reversed(text) if tail else text:
        size = cell_width(char)
        if size > width:
            break
        width -= size
        result.append(char)
    return "".join(reversed(result) if tail else result)


def wrap_line(text, width, indent=""):
    indent = fit(indent, max(0, width - 2))
    available = width - text_width(indent)
    while text:
        fragment = fit(text, available)
        if len(fragment) == len(text):
            yield indent + fragment.rstrip()
            return
        # Prefer a word boundary or a comma in a long list of choices.
        cut = max(fragment.rfind(" "), fragment.rfind(",") + 1)
        if cut > 0 and not text[len(fragment):].startswith(" "):
            fragment = fragment[:cut]
        if not fragment:
            # A double-width glyph cannot be drawn in a one-column viewport.
            text = text[1:]
            continue
        yield indent + fragment.rstrip()
        text = text[len(fragment):].lstrip()


def wrap_rows(text, width):
    for row in text.expandtabs(4).splitlines():
        row = "".join(char for char in row if unicodedata.category(char) != "Cc")
        if not row.strip():
            yield ""
            continue
        content = row.lstrip()
        yield from wrap_line(content, width, row[:len(row) - len(content)])


def print_wrapped(text):
    print("\n".join(wrap_rows(text, terminal_width())))

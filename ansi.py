# ansi.py is for ansi related function and variable to generate better terminal experience inside Discord.

import re


class Color:
    # Foreground
    BLACK   = 30
    RED     = 31
    GREEN   = 32
    YELLOW  = 33
    BLUE    = 34
    MAGENTA = 35
    CYAN    = 36
    WHITE   = 37


    # Background
    BG_BLACK   = 40
    BG_RED     = 41
    BG_GREEN   = 42
    BG_YELLOW  = 43
    BG_BLUE    = 44
    BG_MAGENTA = 45
    BG_CYAN    = 46
    BG_WHITE   = 47



    # Styles
    RESET = 0
    BOLD = 1
    UNDERLINE = 4


def c(*args):
    *codes, text = args
    return f"\033[{';'.join(map(str, codes))}m{text}\033[0m"


# ── Terminal-style helpers ──────────────────────────────────────────────────
# Every command prints the same handful of shapes: a prompt, a label/value pair,
# a success or failure line. Spelling those out with `c()` at every call site
# makes the cogs unreadable, so the colour decisions live here instead.

# Latency bands, matching the "excellent / good / poor" wording used by the
# latency and ping commands.
_GOOD_MS = 80
_FAIR_MS = 150


def clean(text) -> str:
    """Strip stray escapes so user-supplied text cannot spoof colours."""
    return str(text).replace("\033", "")


def plain(text) -> str:
    """Text as-is, just sanitised — for values that need no colour."""
    return clean(text)


def prompt(command: str) -> str:
    """The command line the user typed, e.g. `$ alpha ping discord.com`."""
    return f"{c(Color.BOLD, Color.WHITE, '$')} {c(Color.BOLD, Color.CYAN, clean(command))}"


def label(text) -> str:
    """A field name: `ws:`, `MEM`, `load avg:`."""
    return c(Color.CYAN, clean(text))


def value(text) -> str:
    """A field's value, bright enough to read first."""
    return c(Color.BOLD, Color.WHITE, clean(text))


def ok(text) -> str:
    return c(Color.GREEN, clean(text))


def warn(text) -> str:
    return c(Color.YELLOW, clean(text))


def err(text) -> str:
    return c(Color.BOLD, Color.RED, clean(text))


def note(text) -> str:
    """Low-emphasis context: units, hints, separators."""
    return c(Color.WHITE, clean(text))


def latency(ms) -> str:
    """A millisecond reading, coloured by how good it is."""
    ms = int(ms)
    if ms < _GOOD_MS:
        return c(Color.BOLD, Color.GREEN, f"{ms} ms")
    if ms < _FAIR_MS:
        return c(Color.BOLD, Color.YELLOW, f"{ms} ms")
    return c(Color.BOLD, Color.RED, f"{ms} ms")


def quality(word: str) -> str:
    """Colour a quality verdict to match the latency bands."""
    word = clean(word)
    if word == "excellent":
        return ok(word)
    if word == "good":
        return warn(word)
    return err(word)


def level(pct) -> str:
    """Colour codes for a 0-100 usage percentage: green, yellow or red."""
    pct = float(pct)
    if pct < 60:
        return (Color.BOLD, Color.GREEN)
    if pct < 85:
        return (Color.BOLD, Color.YELLOW)
    return (Color.BOLD, Color.RED)


def field(text, width: int, *codes) -> str:
    """A fixed-width column, padded before colouring.

    Padding a coloured string counts the escape codes, so the plain text is
    widened first and the result is wrapped afterwards.
    """
    padded = clean(text).ljust(width)
    return c(*codes, padded) if codes else padded


def term(*lines) -> str:
    """Wrap already-coloured lines in the code fence Discord needs."""
    return "```ansi\n" + "\n".join(lines) + "\n```"


_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
_TOKEN_RE = re.compile(r"\x1b\[[0-9;]*m|[^\x1b]+")


def visible_len(text: str) -> int:
    """How wide `text` looks on screen, ignoring colour codes."""
    return len(_ESCAPE_RE.sub("", text))


def clip(text: str, width: int) -> str:
    """Truncate to `width` visible characters without cutting a colour span.

    Slicing a coloured string at an arbitrary offset can drop the closing
    reset, which bleeds that colour across everything printed after it.
    """
    if visible_len(text) <= width:
        return text
    out: list[str] = []
    shown = 0
    for token in _TOKEN_RE.findall(text):
        if token.startswith("\x1b"):
            out.append(token)
            continue
        if shown + len(token) <= width:
            out.append(token)
            shown += len(token)
            continue
        out.append(token[: width - shown])
        break
    return "".join(out) + f"\033[{Color.RESET}m"

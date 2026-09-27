# ansi.py is for ansi related function and variable to generate better terminal experience inside Discord.

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

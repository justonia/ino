import sys
import os

try:
    import ipdb
    def set_trace():
        ipdb.set_trace()
    def pm(t):
        ipdb.post_mortem(t)

except ImportError,e:
    import pdb
    def set_trace():
        pdb.Pdb(skip=['ino.ipdb']).set_trace()

    def pm(t):
        pdb.post_mortem(t)

__PASSTHROUGH_EXCEPTIONS = (SyntaxError, SystemExit, KeyboardInterrupt)

def should_use_debugger():
    is_interactive_terminal = sys.stdout.isatty() and sys.stdin.isatty() and sys.stderr.isatty()
    ino_debugger = "INO_DEBUGGER" in os.environ
    return is_interactive_terminal and ino_debugger

def auto_debug_break(type, value, tb):
    import traceback

    if type in __PASSTHROUGH_EXCEPTIONS:
        traceback.print_exception(type, value, tb)
        sys.exit(1)

    if not should_use_debugger():
        sys.__excepthook__(type, value, tb)
        return

    traceback.print_exception(type, value, tb)

    pm(tb)



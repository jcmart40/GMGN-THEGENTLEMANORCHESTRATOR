"""Cross-PROCESS file locking -- built 2026-09-09 after finding a real gap
in the same-night threading.Lock fix (bot/executor.py, bot/paper_trader.py):
tui.py runs as a completely separate `python tui.py` OS process from
bot/screener.py's own process, and imports the exact same executor.py/
paper_trader.py modules directly. Each process gets its OWN, independent
threading.Lock instance in its own memory -- a lock in one process is
invisible to the other, so pressing a dashboard hotkey (tui.py's own
process) while the bot's own exit-check loop is running (bot.screener's
process) can still race on the same live_positions.json/paper_portfolio.json
file, exactly the same silent-lost-write bug the threading.Lock fix closed
for the intra-process (background-thread-vs-main-thread) case only.

msvcrt.locking() (stdlib, Windows-only -- same convention tui.py's own
keyboard handling already uses, see that module's docstring) locks a byte
range of an open file at the OS level, visible to and enforced across
EVERY process on the machine, not just this one's own threads. LK_LOCK
mode blocks and internally retries for up to ~10 seconds before raising --
no manual retry loop needed for the normal case.

Usage: `with cross_process_lock(path):` around the same read-modify-write
critical section the threading.Lock already wraps -- the two are
complementary (the fast in-process lock avoids unnecessary OS lock
contention between threads in the SAME process; the OS lock is what
actually stops two SEPARATE processes from racing), not a replacement for
each other.
"""
from __future__ import annotations

import contextlib
import os

import msvcrt


@contextlib.contextmanager
def cross_process_lock(path: str):
    """Locks a 1-byte region of a dedicated lock file at `path` (created if
    missing, never truncated -- its content doesn't matter, only the OS-level
    lock on it does). `path` should be a stable, dedicated lock file, not the
    actual data file itself (locking the data file directly would conflict
    with the atomic tempfile-then-os.replace pattern every other write in
    this project already uses, since os.replace() needs the destination
    path free of an open handle on it)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # "a+b": create if missing, never truncate, allows both read and write
    # of the 1 byte msvcrt.locking() actually touches.
    with open(path, "a+b") as f:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

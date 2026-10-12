"""Standalone CLI for messaging the AI directly, without going through tui.py
or asking Claude to do it -- 2026-10, user's explicit request for a faster
way to use ai_review.write_user_note() (see that function's own docstring
for how/when the AI picks these up). Run from the GMGN-bot project root:

    python note.py "your message here"

Or with no argument, it prompts interactively:

    python note.py

The note is appended to logs/ai_memory.json (user_notes), the same file and
the same cross-process-locked write path bot/ai_review.py's own
write_user_note() already uses -- safe to run while the bot is running.
"""
from __future__ import annotations

import sys

import bot.ai_review as ai_review


def main() -> None:
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:]).strip()
    else:
        try:
            text = input("Message to AI: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled -- nothing sent.")
            return

    if not text:
        print("Empty message -- nothing sent.")
        return

    ai_review.write_user_note(text)
    print(f"Sent: {text}")


if __name__ == "__main__":
    main()

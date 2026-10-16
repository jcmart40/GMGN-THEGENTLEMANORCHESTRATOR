"""Persistent user/AI conversation log shared by the dashboard and chat UI."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Literal

import bot.ai_review as ai_review
import bot.file_lock as file_lock
from config import config


def _read_unlocked() -> list[dict]:
    if not os.path.exists(config.gentleman_chat_log_file):
        return []
    events = []
    with open(config.gentleman_chat_log_file, "r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid conversation log entry at line {line_number}") from exc
            if not isinstance(event, dict) or event.get("role") not in ("user", "assistant"):
                raise ValueError(f"Invalid conversation log entry at line {line_number}")
            events.append(event)
    return events


def load_events() -> list[dict]:
    with file_lock.cross_process_lock(config.gentleman_chat_log_lock_file):
        return _read_unlocked()


def _new_event(role: Literal["user", "assistant"], text: str, note_id: str) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "role": role,
        "text": text,
        "note_id": note_id,
    }


def _append_unlocked(event: dict) -> None:
    directory = os.path.dirname(config.gentleman_chat_log_file) or "."
    os.makedirs(directory, exist_ok=True)
    with open(config.gentleman_chat_log_file, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_event(role: Literal["user", "assistant"], text: str, note_id: str) -> dict:
    event = _new_event(role, text, note_id)
    with file_lock.cross_process_lock(config.gentleman_chat_log_lock_file):
        _append_unlocked(event)
    return event


def sync_replies() -> list[dict]:
    """Append newly answered chat notes once, safe across UI processes."""
    notes = {
        note.get("id"): note
        for note in ai_review.load_memory().get("user_notes", [])
        if note.get("id") and note.get("answered") and note.get("answer")
    }
    with file_lock.cross_process_lock(config.gentleman_chat_log_lock_file):
        events = _read_unlocked()
        answered_ids = {
            event.get("note_id") for event in events
            if event.get("role") == "assistant" and event.get("note_id")
        }
        user_ids = []
        seen_ids = set()
        for event in events:
            note_id = event.get("note_id")
            if event.get("role") == "user" and note_id and note_id not in seen_ids:
                user_ids.append(note_id)
                seen_ids.add(note_id)
        new_events = [
            _new_event("assistant", str(notes[note_id]["answer"]), note_id)
            for note_id in user_ids
            if note_id not in answered_ids
            if note_id in notes
        ]
        for event in new_events:
            _append_unlocked(event)
    return new_events

"""Windows chat window for sending instructions and questions to the Gentleman."""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

import bot.ai_review as ai_review
import bot.chat_log as chat_log
from config import config

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=LOG_DIR / "gentleman_chat.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("gentleman-chat")
POLL_MS = 1500
MAX_DISPLAY_EVENTS = 200


class GentlemanChat:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Talk to the Gentleman")
        self.root.geometry("620x600")
        self.root.minsize(460, 380)
        self.root.configure(bg="#0d1220")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.polling = False
        self.last_signature: tuple = ()

        self.chat = tk.Text(
            root, bg="#111726", fg="#e6edf3", font=("Consolas", 10),
            wrap="word", state="disabled", relief="flat", padx=12, pady=10,
        )
        scrollbar = tk.Scrollbar(root, command=self.chat.yview)
        self.chat.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", padx=(0, 8), pady=8)
        self.chat.pack(fill="both", expand=True, padx=(8, 0), pady=8)
        self.chat.tag_configure("user", foreground="#79c0ff",
                                font=("Consolas", 9, "bold"))
        self.chat.tag_configure("assistant", foreground="#7ee787",
                                font=("Consolas", 9, "bold"))
        self.chat.tag_configure("pending", foreground="#e4cb00",
                                font=("Consolas", 9, "italic"))

        self.status = tk.Label(
            root,
            text="Messages are answered during the next AI review pass. "
                 "This chat does not place or close trades.",
            bg="#0d1220", fg="#a0aab8", anchor="w", justify="left",
            wraplength=580, font=("Segoe UI", 9),
        )
        self.status.pack(fill="x", padx=12, pady=(0, 4))

        row = tk.Frame(root, bg="#0d1220")
        self.entry = tk.Entry(
            row, bg="#161b22", fg="#e6edf3", font=("Segoe UI", 11),
            relief="flat", insertbackground="#e6edf3",
        )
        self.entry.pack(side="left", fill="x", expand=True,
                        padx=(8, 4), pady=6, ipady=7)
        self.send_button = tk.Button(
            row, text="Send", command=self.send,
            bg="#238636", fg="#ffffff", relief="flat",
            font=("Segoe UI", 10, "bold"), padx=18, pady=7,
        )
        self.send_button.pack(side="right", padx=(4, 8), pady=6)
        row.pack(fill="x")
        self.entry.bind("<Return>", self.send)
        self.root.bind("<Return>", self.send)
        self.root.after(150, self.load_history)
        self.root.after(POLL_MS, self.poll_replies)
        self.root.after(150, self.drain_events)
        self.entry.focus_set()

    def show_event(self, event: dict) -> None:
        role = event.get("role")
        label = "You" if role == "user" else "Gentleman"
        stamp = event.get("ts", "")
        try:
            from datetime import datetime
            stamp = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M")
        except (TypeError, ValueError):
            stamp = ""
        self.chat.configure(state="normal")
        self.chat.insert("end", f"\n[{stamp}] {label}:\n", role)
        self.chat.insert("end", f"{event.get('text', '')}\n")
        if role == "user" and not event.get("answered"):
            self.chat.insert("end", "Waiting for AI review reply...\n", "pending")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def load_history(self) -> None:
        try:
            chat_log.sync_replies()
            events = chat_log.load_events()
            answered_ids = {
                event.get("note_id") for event in events
                if event.get("role") == "assistant"
            }
            for event in events[-MAX_DISPLAY_EVENTS:]:
                if event.get("role") == "user":
                    event = {**event, "answered": event.get("note_id") in answered_ids}
                self.show_event(event)
            self.last_signature = self._signature(events)
        except (OSError, ValueError) as exc:
            log.exception("Could not load the Gentleman conversation history.")
            messagebox.showerror(
                "Gentleman Chat", f"Could not load conversation history:\n{exc}",
                parent=self.root,
            )

    @staticmethod
    def _signature(events: list[dict]) -> tuple:
        return tuple(
            (event.get("ts"), event.get("role"), event.get("note_id"), event.get("text"))
            for event in events[-MAX_DISPLAY_EVENTS:]
        )

    def send(self, event=None) -> str:
        text = self.entry.get().strip()
        if not text:
            return "break"
        self.entry.delete(0, "end")
        self.send_button.configure(state="disabled")
        self.status.configure(text="Saving and sending your message...")
        threading.Thread(target=self._queue_message, args=(text,), daemon=True).start()
        return "break"

    def _queue_message(self, text: str) -> None:
        try:
            note_id = ai_review.write_user_note(text)
        except Exception as exc:
            log.exception("Failed to queue chat message.")
            self.events.put(("error", f"Message was not queued:\n{exc}"))
            return
        try:
            record = chat_log.append_event("user", text, note_id)
        except Exception as exc:
            log.exception("Message queued but conversation log write failed.")
            self.events.put(("warning", f"Message queued, but its conversation log could not be saved:\n{exc}"))
        else:
            self.events.put(("sent", record))

    def poll_replies(self) -> None:
        if not self.polling:
            self.polling = True
            threading.Thread(target=self._read_conversation, daemon=True).start()
        self.root.after(POLL_MS, self.poll_replies)

    def _read_conversation(self) -> None:
        try:
            chat_log.sync_replies()
            self.events.put(("history", chat_log.load_events()))
        except Exception as exc:
            log.exception("Could not refresh the Gentleman conversation.")
            self.events.put(("error", f"Could not refresh conversation:\n{exc}"))
        finally:
            self.polling = False

    def drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "sent":
                    self.show_event(value)
                    self.status.configure(
                        text="Message saved. Waiting for his next AI review reply."
                    )
                    self.send_button.configure(state="normal")
                    self.entry.focus_set()
                elif kind == "history":
                    signature = self._signature(value)
                    if signature != self.last_signature:
                        self.chat.configure(state="normal")
                        self.chat.delete("1.0", "end")
                        answered_ids = {
                            event.get("note_id") for event in value
                            if event.get("role") == "assistant"
                        }
                        for event in value[-MAX_DISPLAY_EVENTS:]:
                            if event.get("role") == "user":
                                event = {**event, "answered": event.get("note_id") in answered_ids}
                            self.show_event(event)
                        self.last_signature = signature
                    if not config.openai_api_key:
                        self.status.configure(
                            text="OPENAI_API_KEY is missing; messages are logged but cannot receive replies."
                        )
                elif kind == "warning":
                    self.send_button.configure(state="normal")
                    self.status.configure(text=str(value))
                    messagebox.showwarning("Gentleman Chat", str(value), parent=self.root)
                elif kind == "error":
                    self.send_button.configure(state="normal")
                    self.status.configure(text=str(value))
                    messagebox.showerror("Gentleman Chat", str(value), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(200, self.drain_events)


def main() -> None:
    root = tk.Tk()
    GentlemanChat(root)
    root.mainloop()


if __name__ == "__main__":
    main()

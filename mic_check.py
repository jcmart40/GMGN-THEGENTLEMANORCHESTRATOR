"""mic_check.py -- standalone end-to-end voice diagnostic.
Run: python mic_check.py        (from the project folder)

Drives the REAL VoiceIO class (the exact code path tui.py uses: mic
auto-discovery -> VAD capture -> faster-whisper -> grammar classify) so
what it verifies is the production pipeline, not a copy. Nothing touches
trading: commands are only PRINTED here, never executed.

Added 2026-10-15 during live setup, after the whole saga: stale PortAudio
indices after Windows enables a device, WDM-KS blocking-API rejection,
48k-only native sample rates, a disabled Microphone Array feeding pure
silence, and an always-listening Voice Access hogging the capture path.

Exit codes: 0 = at least one utterance classified (voice ready),
            1 = zero commands recognized (read the printed hints).
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)-7s %(name)s: %(message)s")
for noisy in ("httpx", "urllib3", "faster_whisper", "huggingface_hub"):
    logging.getLogger(noisy).setLevel(logging.CRITICAL)

from bot.voice_io import VoiceIO          # real production class
from config import config

TEST_SECS = 20.0
SUGGESTED = ["help", "status", "sell all", "close position three",
             "go paper", "voice off"]


def main() -> int:
    print("=" * 72)
    print("GENTLEMAN VOICE — end-to-end check (same code tui.py runs)")
    print("VOICE_ENABLED=%s  VOICE_INPUT_NAME=%r  STT model=%r"
          % (config.voice_enabled, config.voice_input_name, config.voice_stt_model))
    print("=" * 72)
    v = VoiceIO()
    v.start()
    t0 = time.time()
    while v.stt_warming and time.time() - t0 < 120:
        time.sleep(0.25)
    if v.stt_error:
        print("\nSTT FAILED to start (see log lines above) -- faster-whisper/"
              "sounddevice/mic problem. Trading dashboard unaffected.")
        v.stop()
        return 1
    print("\nWhisper ready in %.1fs. Soft clicks you may hear = mic"
          " auto-probing." % (time.time() - t0))

    msg, ok = v.arm_voice()
    print("ARM:", msg, "| ok:", ok)
    if not ok:
        v.stop()
        return 1
    v.keep_armed()

    print("\n--- You have %.0f seconds. Say things like: %s ---"
          % (TEST_SECS, ", ".join(f'"{s}"' for s in SUGGESTED[:4])))
    print("    (2.4s of silence after each sentence closes it. Any\n"
          "     UNMATCHED sentence is dropped safely -- watch the log.)\n")
    got = []
    t_end = time.time() + TEST_SECS
    while time.time() < t_end:
        for cmd in v.drain_commands():
            tag = cmd.intent or "(unmatched -> dropped)"
            print("    HEARD: %-45r -> %s %s" % (cmd.text[:45], tag,
                                                 cmd.payload or ""))
            got.append(cmd)
        v.keep_armed()
        time.sleep(0.2)

    v.disable_listening()
    v.stop()
    time.sleep(0.3)

    acts = [c for c in got if c.intent]
    print("\n" + "=" * 72)
    if any(c.intent in ("help", "status", "sell_all", "close_one", "paper",
                        "cancel", "confirm", "voice_off") for c in acts):
        print("VERDICT: VOICE PIPELINE WORKS -- %d utterance(s) recognized."
              % len(acts))
        print("Start `python tui.py`, press v. Nothing was executed here\n"
              "(this script only PRINTS; tui.py is what acts, with its\n"
              "spoken-confirm gate on money commands).")
        return 0
    if got:
        print("VERDICT: mic + Whisper work but NOTHING matched the grammar\n"
              "(transcripts above). Speak clearly, try the exact phrases.")
        return 0
    print("VERDICT: zero utterances recognized.\n"
          "  - never spoke during window? rerun and talk more.\n"
          "  - Windows default INPUT must be the real Microphone Array\n"
          "    (Settings -> System -> Sound -> Input) -- NOT Stereo Mix;\n"
          "    `mmsys.cpl` -> Recording tab: enabled + green check.\n"
          "  - close anything holding the mic (Voice access: Accessibility\n"
          "    settings -> OFF).")
    return 1


if __name__ == "__main__":
    sys.exit(main())

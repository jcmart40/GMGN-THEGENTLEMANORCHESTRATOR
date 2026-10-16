"""Real-time voice layer for tui.py -- spoken commands in, spoken alerts out.

Built 2026-10-15 at the operator's explicit request ("issue voice commands
and receive audio/text feedback hands-free while manual trading").

Architecture, and why each piece is this shape:

- THREE daemon threads, plain queues, NOT asyncio. tui.py's main loop is
  already a poll loop (msvcrt.kbhit() + time.sleep(0.02)); draining a
  queue next to it is the smallest possible diff, and Rich Live is not
  safe to update from a background thread anyway -- every render stays on
  the main thread. daemon=True everywhere, so a dead mic or voice engine
  can never take the dashboard down -- same "a bug here can't affect
  trading" contract tui.py's own docstring makes.
  * STT thread: opens the mic per utterance (sounddevice), runs a
    self-calibrating energy VAD, transcribes with local faster-whisper,
    pushes VoiceCommand onto a queue the dashboard drains.
  * TTS thread: OWNS THE pyttsx3/SAPI5 ENGINE EXCLUSIVELY. It is not
    thread-safe and runs its own internal message loop; calling it from
    the main thread is exactly how this kind of feature freezes a UI.
    Only ever reached through speak(), which just enqueues -- drop-oldest,
    max 4 deep: an alert heard late is worse than one never heard. The
    echo gate (capture won't start while this thread is mid-sentence, plus
    a short settling gap) is why spoken "confirm?" prompts can never be
    transcribed back as a confirm from our own voice.
  * Alert thread: read-only watcher of the same JSON files tui.py
    already renders (trade history closes + cleared screening events),
    speaking them unprompted. Runs WITHOUT the mic armed -- hearing the
    bot act is the point; pushing-to-talk is only for issuing commands.

- Push-to-toggle (`v`) instead of always-on hotword: msvcrt gives clean
  single-keystroke semantics but no reliable keyup, and an always-listening
  mic in a room with speakers is a feedback-loop nightmare (the TTS alert
  gets transcribed back as a command). Armed state is loud in the header
  badge; a session keeps re-arming capture after every utterance/silence
  until disarmed, and an utterance auto-closes on ~2.4s of trailing
  silence so a missed second `v` can't leave the mic hot for hours.

- Why the parse is a tiny closed grammar: the 2026-09-16 "l"-key incident
  (see tui.py) proved a single ambiguous trigger with zero confirmation
  can move real money. A mis-heard word is a strictly worse trigger than
  a mis-pressed key, so: read-only voice commands (status/help/note) fire
  instantly, action commands (sell all / close N / shutdown) must be
  RE-SPOKEN inside a short confirmation window -- any other utterance
  cancels instead of acting. "Live" is refused outright, exactly like the
  keyboard's `l` now refuses it.

- Local faster-whisper only. No API key, no network. whisper's segment
  "confidence" turned out to be a language-model score, not a correctness
  score -- it confidently hallucinates fluent sentences from room noise --
  so gating happens on GRAMMAR MATCH, not on confidence: an utterance that
  isn't recognizably one of the commands below is silently dropped (only
  logged), never "executed with a warning".

Run with: python tui.py, press v. All knobs in .env (VOICE_*);
config.voice_enabled=False removes the layer entirely.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

try:
    import numpy as np
except ImportError:  # a missing numpy disables VOICE ONLY -- tui.py imports
    np = None        # this module lazily-guarded and keeps rendering fine.

from config import config

log = logging.getLogger("gmgn-agent")

SAMPLE_RATE = 16_000
_FRAME_MS = 30                       # whisper's internal frame size; VAD matches it
_PREROLL_CHUNKS = 20                 # ~0.6s of audio kept before speech onset so the
                                     # first consonant of a command isn't clipped
_SILENCE_CHUNKS_AFTER_SPEECH = 80   # 80 x 30ms = 2.4s trailing silence ends an utterance
_MIN_SPEECH_CHUNKS = 15             # 0.45s -- shorter than this is a cough, not a command
_MAX_UTTERANCE_SECS = 8.0           # longer utterances are force-cut and transcribed
_TTS_MAX_QUEUE = 4
_ALERT_SWEEP_SECS = 1.5
_ALERT_FRESHNESS_SECS = 120.0       # never replay events older than this on startup
_ECHO_SETTLE_SECS = 0.9             # quiet gap after the Gentleman stops talking
                                    # before the mic opens -- 0.3s clipped room
                                    # tails; 0.9s covers SAPI + sound-card lag

_INITIAL_NOISE_GATE = 0.015          # rms floor before the gate self-calibrates

# Capture devices that can never be a spoken command's source (loopbacks,
# mapper pseudo-devices) -- skipped during voice mic discovery.
_NON_INPUT_HINTS = ("stereo mix", "loopback", "what u hear", "what you hear",
                    "pc speaker", "sound mapper", "primary sound capture")
_PROBE_CLICK_SECS = 0.3
_PROBE_CLICK_AMP = 0.6        # matched to the LENGTH the live beep-sweep validated:
                              # a thicker 0.3s/0.6 click reads ~0.02 peak at
                              # real-mic levels; the earlier 0.10s/0.55 only
                              # produced ~0.001 (below a 0.0025 floor and
                              # indistinguishable from electrical noise), so
                              # real mics failed the probe. Length, not just
                              # loudness, clears the floor.
_PROBE_DRAIN_SECS = 0.25
_PROBE_LIVE_FLOOR = 0.0025    # peak that counts as "this mic hears sound"

# Whisper tiny/base confusions that actually bite this exact command set
# (all observed patterns of "heard word -> intended word"; applied
# longest-first on the normalized text, word-bounded, never inventing an
# intent from nothing, only steering a mis-heard string toward the
# intended phrase).
_HOMOPHONE_FIXES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bgentlemans\b"), "gentleman"),
    (re.compile(r"\bgm gn\b"), "gmgn"),
    (re.compile(r"^\s*(?:hey|ok|okay|yo|the)\s+"), ""),
    (re.compile(r"\bsell all of\b"), "sell all"),
    (re.compile(r"\bclose all of\b"), "close all"),
    (re.compile(r"\bcell\b"), "sell"),      # top Whisper tiny mishear of "sell"
    (re.compile(r"\bshut down\b"), "shutdown"),
    (re.compile(r"\bposition number\b"), "position"),
    (re.compile(r"\bmessage two\b"), "message to"),
    (re.compile(r"\bnote two\b"), "note to"),
)

# NOTE: "two"->"to" ONLY appears inside fixed phrases above, never
# globally -- "close two" must keep its digit meaning. These run BEFORE
# digit extraction, so "close position number two" arrives at the grammar
# already reduced to "close position two".

_DIGIT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9,
    "first": 1, "second": 2, "third": 3,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
}

_CONFIRMS = {
    "confirm", "yes", "do it", "go ahead", "yes do it", "confirm it",
    "that is correct", "correct",
}

_CLOSE_TARGET = (r"(?:" + "|".join(_DIGIT_WORDS) + r")")

_INTENTS: tuple[tuple[str, re.Pattern], ...] = (
    # ordered; FIRST MATCH WINS -- order is the safety property, not
    # cosmetics:
    #  - cancel/confirm/voice_off/help/status stay before everything,
    #  - `note` sits ABOVE the action intents on purpose: "note to the
    #    gentleman, switch to paper" mentions "paper" but must never fire
    #    a real mode flip -- the note path is inert by design (writes to
    #    the AI chat log, touches nothing else), actions are not.
    ("cancel",   re.compile(r"^(?:cancel|never ?mind|disregard|forget it)\.?$")),
    ("confirm",  re.compile(r"^(?:" + "|".join(re.escape(w) for w in _CONFIRMS) + r")\.?$")),
    ("voice_off", re.compile(r"^(?:(?:hey |the )?gentleman[, ]+)?(?:voice off|stop listening|disarm voice)\.?$")),
    ("help",     re.compile(r"\b(?:help|what can you do|voice commands?)\b")),
    ("status",   re.compile(r"\b(?:status|position report|how are we|balance|p and l|profit)\b(?:\s+report)?")),
    ("note",     re.compile(r"\b(?:note|message)\s+to\s+(?:the\s+)?(?:gentleman|g man|gmgn|ai)\b[.:,]?\s*(?:is\s+|that\s+|says?\s+)?(.*)")),
    ("sell_all", re.compile(r"\b(?:sell|dump|close)\s+(?:it\s+)?(?:all|everything)\b")),
    ("shutdown", re.compile(r"\b(?:shutdown|shut (?:it|everything) down|stop trading|kill switch|emergency stop)\b")),
    ("paper",    re.compile(r"\bpaper\b")),
    ("live_refused", re.compile(r"\b(?:live|go live|unlock live|real money)\b")),
    ("close_one", re.compile(
        r"\b(?:"
        r"(?:close|dump)\s+(?:position|num(?:ber)?|#)?\s*(" + _CLOSE_TARGET + r")\b"
        r"|"
        r"sell\s+(?:position|num(?:ber)?|#)\s*(" + _CLOSE_TARGET + r")\b"
        r")")),
)


@dataclass
class VoiceCommand:
    """One recognized utterance. `text` is the RAW transcript (what the
    operator actually said, shown on the dashboard so a mis-hear is
    visible); `intent` is the closed-grammar classification, or None if
    it was just a sentence we happened to hear (dropped, logged only --
    see docstring on why we never act on unmatched audio)."""
    text: str
    intent: str | None = None
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


def normalize(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"[^\w\s.#'-]", " ", t)
    for pattern, repl in _HOMOPHONE_FIXES:
        t = pattern.sub(repl, t)
    return re.sub(r"\s+", " ", t).strip()


def classify(text: str) -> VoiceCommand:
    """Pure function: raw transcript -> VoiceCommand. The single place
    the grammar lives; tui.py only maps intent -> existing action."""
    norm = normalize(text)
    for name, pattern in _INTENTS:
        m = pattern.search(norm)
        if not m:
            continue
        payload: dict = {}
        if name == "close_one":
            # two-branch pattern: digit in group 1 (close/dump N) or
            # group 2 (sell position N) -- exactly one fires per match.
            digit = m.group(1) or m.group(2)
            payload["index"] = _DIGIT_WORDS[digit]
        elif name == "note":
            body = m.group(1).strip(" .,")
            if not body:               # "note to the gentleman" with nothing
                continue               # after it: not a note, drop through
            payload["text"] = body
        elif name == "confirm":
            payload["norm"] = norm
        return VoiceCommand(text=text.strip(), intent=name, payload=payload)
    log.info("voice_io: dropped unmatched utterance: %r", text.strip())
    return VoiceCommand(text=text.strip(), intent=None)


class VoiceIO:
    """Public surface used by tui.py:
         start/stop, arm_voice()/disable_listening()/keep_armed(),
         listening/stt_ready/stt_error/stt_warming properties,
         drain_commands(), speak().
       Everything else runs on the three worker threads.
    """

    def __init__(self) -> None:
        self.sample_rate = SAMPLE_RATE
        self._commands: queue.Queue[VoiceCommand] = queue.Queue()
        self._tts_q: queue.Queue[str] = queue.Queue()

        self._stop = threading.Event()
        self._listening = threading.Event()
        self._stt_ready = threading.Event()
        self._stt_failed = False
        self._tts_up = threading.Event()
        self._tts_failed = False
        # Mic gate: capture must not open while the TTS thread is speaking
        # or shortly after -- otherwise my own spoken "say confirm..."
        # prompt feeds the STT mic and a Whisper segment could echo back a
        # "confirm" I asked for myself. Belt AND braces: the gate, plus
        # confirm intent being anchored ^...$ on the full utterance.
        self._speaking = threading.Event()
        self._last_speech_end = 0.0
        self._in_capture = False           # keep_armed() watchdog guard
        self._mic_choice: tuple[int, int, tuple] | None = None  # (idx, rate, list sig)

        self._stt_thread = threading.Thread(target=self._stt_worker, name="voice-stt", daemon=True)
        self._tts_thread = threading.Thread(target=self._tts_worker, name="voice-tts", daemon=True)
        self._alert_thread = threading.Thread(target=self._alert_worker, name="voice-alerts", daemon=True)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Returns immediately. The first Whisper construction also
        DOWNLOADS the model (~75MB for tiny) -- far too slow for the
        dashboard's startup path, so readiness is surfaced via stt_warming
        / stt_ready / stt_error, exactly like _balance_cache hides latency
        elsewhere in tui.py."""
        if np is None:
            self._stt_failed = True
            log.warning("voice_io: numpy missing -- voice disabled entirely.")
            return
        self._stt_thread.start()
        self._tts_thread.start()
        self._alert_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._listening.set()          # wake a blocked mic stream so it can exit

    # -- mic arming ------------------------------------------------------

    def arm_voice(self) -> tuple[str, bool]:
        """The `v` hotkey's enable half. Returns (message, ready_to_set
        session flag). Arming is a SESSION, not one capture: the mic
        re-arms continuously while the dashboard's `_voice_on` flag is
        set (main loop calls keep_armed() each tick, worker closes the
        stream between utterances). Deliberately does not open the stream
        HERE -- see speak()/keep_armed() echo-gate notes."""
        if self._stt_failed:
            return "Voice input unavailable -- check the log.", False
        if not self._stt_ready.is_set():
            return "Voice engine still loading (first run also downloads the model) -- press v again soon.", False
        return "Voice armed. Say 'help' for commands, press v to disarm.", True

    def disable_listening(self) -> str:
        """The `v` disable half AND the 'voice off' spoken intent -- one
        operation, two spellings."""
        self._listening.clear()
        return "Voice disarmed."

    @property
    def speaking_quiet(self) -> bool:
        """False while the Gentleman is mid-sentence or within the echo
        settling window -- the tui watchdog holds off re-arming until this
        flips True, so a spoken "say confirm" prompt can't be captured as
        its own answer."""
        return (not self._speaking.is_set()
                and (time.time() - self._last_speech_end) >= _ECHO_SETTLE_SECS)

    def keep_armed(self) -> None:
        """Idempotent tick-level watchdog for an active session: if the
        session says armed but no capture is actually running, start one.
        Covers the arm -> silence only -> NO command -> nothing fires any
        re-arm case that a purely command-driven re-arm would deadlock on.
        The worker's echo gate (see _record_utterance) is what keeps this
        from opening the mic into the Gentleman's own half-finished
        sentences."""
        if (self._stt_ready.is_set() and not self._stt_failed and not self._stop.is_set()
                and not self._in_capture and not self._listening.is_set()
                and self.speaking_quiet):
            self._listening.set()

    @property
    def listening(self) -> bool:
        return self._listening.is_set()

    @property
    def stt_ready(self) -> bool:
        return self._stt_ready.is_set()

    @property
    def stt_warming(self) -> bool:
        return not self._stt_ready.is_set() and not self._stt_failed

    @property
    def stt_error(self) -> bool:
        return self._stt_failed

    # -- main-thread API ---------------------------------------------------

    def drain_commands(self) -> list[VoiceCommand]:
        out: list[VoiceCommand] = []
        while True:
            try:
                out.append(self._commands.get_nowait())
            except queue.Empty:
                return out

    def speak(self, text: str, on_done=None) -> None:
        """Never blocks -- only enqueues. Drop-oldest, shallow on
        purpose: a TTS backlog is how this feature ends up reading last
        minute's stale prices out loud.

        on_done fires on the TTS thread right after the phrase finishes
        playing (or is dropped). It exists for one call site: re-arming
        the mic only AFTER the 'say confirm' prompt has been spoken --
        arming before/during it would let Whisper hear my own voice ask
        for the confirm and answer myself."""
        if not text:
            if on_done:
                try:
                    on_done()
                except Exception:  # noqa: BLE001
                    log.exception("voice_io: speak on_done callback failed")
            return
        while self._tts_q.qsize() >= _TTS_MAX_QUEUE:
            try:
                item = self._tts_q.get_nowait()
                self._tts_q.task_done()
                if item[1]:
                    item[1]()
            except queue.Empty:
                break
        self._tts_q.put((text, on_done))

    # -- STT worker --------------------------------------------------------

    def _stt_worker(self) -> None:
        try:
            import faster_whisper
            import sounddevice as sd
        except Exception:  # noqa: BLE001 -- missing dep = voice off, dashboard fine
            self._stt_failed = True
            log.exception("voice_io: faster-whisper/sounddevice import failed -- voice disabled.")
            return

        # Device diagnostic (2026-10-15, operator's explicit request): print
        # EVERY input device and which index PortAudio picked as default, so
        # a "it hears the wrong mic" complaint (e.g. a virtual Stereo Mix /
        # AMD wave-mic grabbing the default slot) is immediately visible in
        # logs/bot.log instead of guessable. A wrong default is fixable
        # without code: Windows Settings -> System -> Sound -> Input, or
        # set the desired device as Default Communications device.
        try:
            log.info("voice_io: default input device index = %r", sd.default.device[0]
                     if sd.default.device else None)
            for idx, dev in enumerate(sd.query_devices()):
                if dev.get("max_input_channels", 0) > 0:
                    mark = " <== DEFAULT" if idx == (sd.default.device[0] if sd.default.device else -1) else ""
                    log.info("voice_io: input[%d] %r ch=%d%s",
                             idx, dev["name"], dev["max_input_channels"], mark)
        except Exception:  # noqa: BLE001 -- diagnostics must never gate startup
            log.exception("voice_io: device enumeration failed")

        model_size = config.voice_stt_model
        try:
            model = faster_whisper.WhisperModel(model_size, device="cpu",
                                                compute_type=config.voice_stt_compute)
        except Exception:  # noqa: BLE001 -- bad name / no disk / offline first-download
            self._stt_failed = True
            log.exception("voice_io: Whisper model %r failed to load -- voice disabled.",
                          model_size)
            return
        self._stt_ready.set()
        log.info("voice_io: Whisper %r loaded; voice armed only by `v`.", model_size)

        while not self._stop.is_set():
            self._listening.wait()
            if self._stop.is_set():
                return
            self._in_capture = True
            try:
                audio = self._record_utterance(sd)
            except Exception:  # noqa: BLE001 -- device unplugged mid-session:
                audio = None
                if not self._stop.is_set():
                    log.exception("voice_io: capture failed; retrying in 3s")
                    self._stop.wait(3.0)
            finally:
                self._in_capture = False
            if audio is None:
                continue
            try:
                segments, _info = model.transcribe(
                    audio, language="en", beam_size=1,
                    vad_filter=True, no_speech_threshold=0.6,
                    condition_on_previous_text=False,
                )
                text = " ".join(s.text.strip() for s in segments).strip()
            except Exception:  # noqa: BLE001
                log.exception("voice_io: transcribe failed (utterance dropped)")
                continue
            if not text or len(text) < 2:
                continue
            self._commands.put(classify(text))

    def _resolve_mic(self, sounddevice):
        """-> (device_index_or_None, samplerate). Auto-discovers a mic that
        actually delivers audio, cached against the device-list signature.

        Live findings on this operator's machine that forced all of this:
        - PortAudio indices shift EVERY time Windows enables/disables a
          device (18 -> silent virtual array; post-enable, same physical
          mic moved to #21), so any stored INDEX is a time-bomb;
        - several namesakes of one mic exist across host APIs (DirectSound
          / WASAPI / WDM-KS); some OPEN but stream pure zeros (disabled or
          virtual), others reject PortAudio outright ('Invalid device');
        - the enabled physical mic only opens at its NATIVE rate (16k
          rejected, 44.1/48k fine), and the old 'dead' #18-style WDM-KS
          entries reject even callback reads at foreign block sizes.
        Strategy: ordered candidates = name substring matches (config
        voice_input_name, default 'Microphone Array') then the rest of the
        sensible inputs; for each, quick-open at native rate and play two
        soft clicks through the speakers -- first device that MEASURES the
        click wins, since measuring is the only truth that survives driver
        weirdness. Never picks loopback-ish devices (they'd play our own
        TTS back at us in a feedback loop). Last-resort fallbacks keep
        voice usable (better quiet-silent than dead) and everything here
        is guarded -- worst case returns the OS default with a logged note.
        """
        try:
            devs = sounddevice.query_devices()
        except Exception:  # noqa: BLE001 -- audio stack wedged entirely
            return None, self.sample_rate
        sig = tuple((d["name"], d.get("default_samplerate")) for d in devs)
        if self._mic_choice and self._mic_choice[2] == sig:
            return self._mic_choice[0], self._mic_choice[1]

        default_in = sounddevice.default.device[0] if sounddevice.default.device else None
        wanted = (config.voice_input_name or "").strip().lower()

        def plausible(i, d):
            if d.get("max_input_channels", 0) <= 0:
                return False
            low = d["name"].lower()
            return not any(h in low for h in _NON_INPUT_HINTS)

        order: list[int] = []
        if wanted:
            order += [i for i, d in enumerate(devs) if plausible(i, d)
                      and wanted in d["name"].lower()]
        if config.voice_input_device is not None and config.voice_input_device >= 0:
            order.append(int(config.voice_input_device))
        if default_in is not None and plausible(default_in, devs[default_in]):
            order.append(int(default_in))
        order += [i for i, d in enumerate(devs) if plausible(i, d)
                  and ("microphone" in d["name"].lower()
                       or "mic" in d["name"].lower()
                       or "array" in d["name"].lower())]
        order += [i for i, d in enumerate(devs) if plausible(i, d)]
        seen, cands = set(), []
        for i in order:
            if 0 <= i < len(devs) and i not in seen:
                seen.add(i)
                cands.append(i)

        def sweep():
            for i in cands:
                # Between-candidate settle: several PortAudio entries here
                # are DIFFERENT VIEWS OF THE SAME PHYSICAL MIC (DirectSound
                # alias, AFD wave install, ...). Probing one and immediately
                # probing its sibling can leave the previous capture session
                # still tearing down, so the sibling's probe sees contention
                # instead of the click and falsely reports silence -- hit
                # live twice during setup with exclusive-mode allowed on the
                # device. 150ms is enough for the WASAPI teardown.
                time.sleep(0.15)
                native = float(devs[i].get("default_samplerate") or 0.0)
                rates = [r for r in (native, 48_000.0, 44_100.0, float(self.sample_rate))
                         if r > 0]
                tried: set[int] = set()
                for rate in rates:
                    rate = int(rate)
                    if rate in tried:
                        continue
                    tried.add(rate)
                    if self._probe_mic_hears(sounddevice, i, rate):
                        return (i, rate)
            return None

        picked = sweep()
        if picked is None:
            # A full sweep of all-mics-not-hearing-clicks is far more often
            # teardown contention or a busy audio engine than a truly dead
            # mic -- pause long enough for everything to settle and sweep
            # once more before declaring defeat.
            log.info("voice_io: click-probe sweep found nothing; retrying once "
                     "after a pause (likely capture-session teardown contention)")
            self._stop.wait(2.0)
            picked = sweep()

        if picked is None:
            log.warning("voice_io: no input device heard the self-test click -- "
                        "falling back to candidates blindly (utterances will log "
                        "empty if it's still silent).")
            if cands:
                i = cands[0]
                picked = (i, int(devs[i].get("default_samplerate") or self.sample_rate))
            else:
                picked = (None, self.sample_rate)

        self._mic_choice = (picked[0], picked[1], sig)
        name = (devs[picked[0]]["name"] if picked[0] is not None
                else "OS-default input")
        log.info("voice_io: mic selected -> #%r %r @ %d Hz", picked[0], name, picked[1])
        return picked

    def _probe_mic_hears(self, sounddevice, idx: int, rate: int) -> bool:
        """Open mic #idx at #rate, click the speakers 3x, check the mic
        measured one. Only returns True on a genuinely ABOVE-FLOOR peak,
        so silent virtual devices fail out. Timing lesson from the live
        sweeps: sounddevice.play() is ASYNCHRONOUS -- the 0.2s collection
        window started before the click even reached the speakers and a
        pure peak can be missed entirely; wait() through playback THEN
        drain for a bit is what detected reliably. Deliberately not
        louder than _PROBE_CLICK_AMP: a soft tick, not a jump-scare."""
        q: queue.Queue = queue.Queue(maxsize=64)

        def _cb(indata, _n, _t, _st):
            try:
                # channels=1 raw stream: indata is one contiguous float32
                # block; bytes() copies it (buffer is reused after return).
                q.put_nowait(np.frombuffer(bytes(indata), dtype=np.float32).copy())
            except Exception:  # noqa: BLE001 -- full queue: just miss a frame
                pass

        block = max(64, rate * _FRAME_MS // 1000)
        try:
            with sounddevice.RawInputStream(samplerate=rate, blocksize=block, channels=1,
                                            dtype="float32", device=idx, callback=_cb):
                click = (np.sin(2 * np.pi * 660 * np.arange(int(_PROBE_CLICK_SECS * rate))
                                / rate) * _PROBE_CLICK_AMP).astype(np.float32)
                best = 0.0
                for _ in range(3):
                    try:
                        sounddevice.play(click, rate)
                        sounddevice.wait()
                    except Exception:  # noqa: BLE001 -- speakers busy: one miss
                        continue
                    t_end = time.time() + _PROBE_DRAIN_SECS
                    while time.time() < t_end:
                        try:
                            fr = q.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        if len(fr):
                            best = max(best, float(np.max(np.abs(fr))))
                    if best >= _PROBE_LIVE_FLOOR:
                        break
                return best >= _PROBE_LIVE_FLOOR
        except Exception:  # noqa: BLE001 -- this (idx, rate) simply can't open
            return False

    def _record_utterance(self, sounddevice):
        """Blocking mic session for ONE utterance. None = disarmed with
        nothing caught. The stream closes while the STT thread transcribes
        (sub-half-second), so audio said DURING transcription spills into
        the next utterance instead -- an acceptable trade against having a
        second queue between capture and model, and why back-to-back
        shouting two commands can lose the second one (log shows it)."""
        # Echo gate: never open the mic while the Gentleman is mid-sentence
        # or just stopped -- otherwise a spoken "say confirm" prompt could
        # transcribe into the confirmation that answers itself. Capture
        # waits here (until speech ends + the settling gap, or until the
        # arm is cancelled), it does not silently drop the utterance.
        while self._speaking.is_set() and self._listening.is_set() \
                and not self._stop.is_set():
            time.sleep(0.1)
        if not self._listening.is_set() or self._stop.is_set():
            return None
        settle = _ECHO_SETTLE_SECS - (time.time() - self._last_speech_end)
        if settle > 0:
            time.sleep(settle)
            if not self._listening.is_set() or self._stop.is_set():
                return None
        # Resolve which physical mic to open (after the gate so the probe's
        # test-clicks never overlap the Gentleman's own speech). PortAudio
        # indices REORDER whenever Windows enables/disables a device (this
        # burned us live 2026-10-15: enabling "Microphone Array" shifted
        # every index, silently stranding VOICE_INPUT_DEVICE=18/19 on stale
        # devices), several namesakes exist across host APIs with only some
        # openable (WASAPI rejects PortAudio on this machine, DirectSound/
        # MME stream), and at least one enabled-but-silent virtual device
        # exists. So a config index alone can't be trusted: probe-open each
        # candidate, play two short clicks through the speakers, and keep
        # the first mic that MEASURES sound. Result cached until the device
        # list signature changes or the bound stream fails to open.
        device, stream_rate = self._resolve_mic(sounddevice)
        frames = max(1, int(stream_rate * _FRAME_MS // 1000))
        ring: deque[np.ndarray] = deque(maxlen=_PREROLL_CHUNKS)
        chunks: list[np.ndarray] = []
        total = 0.0
        noise_rms = _INITIAL_NOISE_GATE
        silence_run = 0
        heard_speech = False
        # CALLBACK capture, NOT blocking stream.read(): this machine's real
        # mic devices (WDM-KS: "AMDAfdInstall Wave Microphone") reject the
        # PortAudio blocking API outright ("Blocking API not supported yet",
        # PaErrorCode -9999 -- found by live device testing 2026-10-15) but
        # stream fine through a callback. The callback just hands frames to
        # a queue; the loop below consumes, so all VAD logic is unchanged.
        audio_q: queue.Queue = queue.Queue(maxsize=256)

        def _cb(indata, frame_count, _t, status):
            if status:
                log.debug("voice_io: capture status: %s", status)
            try:
                audio_q.put_nowait((np.frombuffer(indata, dtype=np.float32)
                                    [:frame_count].copy() if getattr(indata, "ndim", 1) > 1
                                    else np.frombuffer(indata, dtype=np.float32)[:frame_count].copy()))
            except queue.Full:
                pass  # transcription lag: drop oldest-coupled frames, never wedge the audio driver

        try:
            stream = sounddevice.RawInputStream(samplerate=stream_rate, blocksize=frames,
                                                channels=1, dtype="float32",
                                                device=device, callback=_cb)
        except Exception:  # noqa: BLE001 -- device vanished between resolve and
            # use (Windows renumbering mid-session): drop the cache, re-resolve
            # once, last-ditch blocksize=0 (driver's own pick) on the new pick.
            log.warning("voice_io: cached mic open failed; re-resolving device")
            self._mic_choice = None
            device, stream_rate = self._resolve_mic(sounddevice)
            frames = max(1, int(stream_rate * _FRAME_MS // 1000))
            try:
                stream = sounddevice.RawInputStream(samplerate=stream_rate, blocksize=frames,
                                                    channels=1, dtype="float32",
                                                    device=device, callback=_cb)
            except Exception:  # noqa: BLE001
                stream = sounddevice.RawInputStream(samplerate=stream_rate, blocksize=0,
                                                    channels=1, dtype="float32",
                                                    device=device, callback=_cb)
        with stream:
            while not self._stop.is_set():
                if not self._listening.is_set():
                    # disarmed mid-utterance: flush whatever was collected
                    break
                try:
                    mono = audio_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                rms = float(np.sqrt(np.mean(np.square(mono.astype(np.float64)))))
                gate = max(_INITIAL_NOISE_GATE, noise_rms * 3.0)
                if rms <= gate:
                    silence_run += 1 if heard_speech else 0
                    ring.append(mono.copy())
                    if heard_speech and silence_run >= _SILENCE_CHUNKS_AFTER_SPEECH:
                        break
                    continue
                # moving average keeps the gate learning under continuous speech
                noise_rms = 0.999 * noise_rms + 0.001 * rms
                if not heard_speech:
                    heard_speech = True
                    chunks.extend(ring)      # preroll so the first word isn't clipped
                    ring.clear()
                chunks.append(mono.copy())
                total += len(mono) / stream_rate
                silence_run = 0
                if total >= _MAX_UTTERANCE_SECS:
                    break
        self._listening.clear()              # one utterance per arm -- see
        if not heard_speech or not chunks:
            return None                       # docstring re: push-to-toggle cadence
        speech_only = sum(len(c) for c in chunks) / stream_rate
        audio = np.concatenate(chunks)
        if speech_only < _MIN_SPEECH_CHUNKS * frames / stream_rate:
            return None
        if stream_rate != self.sample_rate:
            # linear interpolation resample 48k->16k: crude but plenty for
            # short command speech, and dependency-free (no soxr/scipy).
            n_out = int(len(audio) * self.sample_rate / stream_rate)
            xp = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
            fp = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            audio = np.interp(fp, xp, audio).astype(np.float32)
        return audio

    # -- TTS worker ----------------------------------------------------------

    def _tts_worker(self) -> None:
        try:
            import pyttsx3
            engine = pyttsx3.init()          # created ONLY here (docstring:
        except Exception:  # noqa: BLE001    # SAPI5 is not thread-safe)
            self._tts_failed = True
            log.exception("voice_io: TTS engine failed to init -- alerts print to log only.")
            return
        try:
            engine.setProperty("rate", config.voice_tts_rate)
            wanted = (config.voice_tts_voice or "").strip()
            if wanted:
                for v in engine.getProperty("voices") or []:
                    if wanted.lower() in (v.name or "").lower():
                        engine.setProperty("voice", v.id)
                        break
        except Exception:  # noqa: BLE001 -- voice pick is cosmetic
            log.exception("voice_io: TTS voice selection failed; default voice used")
        self._tts_up.set()
        log.info("voice_io: TTS engine up.")
        while not self._stop.is_set():
            try:
                text, on_done = self._tts_q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                try:
                    self._speaking.set()   # mic gate sees this (see __init__
                    engine.say(text)       # comment: echo prevention)
                    engine.runAndWait()
                except Exception:  # noqa: BLE001 -- one bad utterance can't
                    log.exception("voice_io: failed to speak %r", text)
                finally:
                    self._last_speech_end = time.time()
                    self._speaking.clear()
            finally:
                self._tts_q.task_done()
                # Fired even when the phrase failed to play, so a mic
                # re-arm promised by this phrase can't silently die.
                if on_done:
                    try:
                        on_done()
                    except Exception:  # noqa: BLE001
                        log.exception("voice_io: speak on_done callback failed")

    # -- alert worker ---------------------------------------------------------

    def _alert_worker(self) -> None:
        """Read-only sweep of the shared JSON files -- the same
        files-as-message-bus tui.py renders from, this process never
        touches the engine. Two streams: closes from bot/trade_history's
        file, and autonomous-BUY candidates 'cleared' in the activity log
        (logged by screener AT buy time; whether the fill then failed is
        in the close stream, so this is 'bought' as the engine itself
        announces it). Freshness-windowed so booting the dashboard never
        narrates yesterday."""
        boot = time.time()
        last_close = self._max_ts(config.trade_history_file, "exit_ts") or boot
        last_event = self._max_ts(config.activity_log_file, "ts") or boot
        while not self._stop.is_set():
            if config.voice_speak_alerts and self._tts_up.is_set():
                try:
                    last_close = self._sweep_closes(last_close)
                    last_event = self._sweep_screen_events(last_event)
                except Exception:  # noqa: BLE001 -- sweep must never die
                    log.exception("voice_io: alert sweep failed; retrying next pass")
            self._stop.wait(_ALERT_SWEEP_SECS)

    @staticmethod
    def _max_ts(path: str, key: str) -> float:
        return max((_entry_ts(e, key) for e in VoiceIO._read_records(path)), default=0.0)

    def _sweep_closes(self, last_seen: float) -> float:
        for e in self._read_records(config.trade_history_file):
            ts = _entry_ts(e, "exit_ts")
            if ts <= last_seen:
                continue
            last_seen = max(last_seen, ts)
            if time.time() - ts > _ALERT_FRESHNESS_SECS:
                continue
            sym = e.get("symbol") or (e.get("address") or "?")[:6]
            pnl_sol = float(e.get("pnl_sol") or 0.0)
            pnl_pct = float(e.get("pnl_pct") or 0.0) * 100.0   # stored as fraction (see
            reason = str(e.get("reason") or "").replace("_", " ")  #   _fee_adjusted_pnl_pct)
            self.speak(f"Closed {sym}. {'Profit' if pnl_sol >= 0 else 'Loss'} "
                       f"{abs(pnl_pct):.0f} percent, {abs(pnl_sol):.3f} sol. {reason}.")
            log.info("voice alert: closed %s %+.2f%% %+0.4f SOL (%s)",
                     sym, pnl_pct, pnl_sol, reason)
        return last_seen

    def _sweep_screen_events(self, last_seen: float) -> float:
        for e in self._read_records(config.activity_log_file):
            ts = _entry_ts(e, "ts")
            if ts <= last_seen:
                continue
            last_seen = max(last_seen, ts)
            if time.time() - ts > _ALERT_FRESHNESS_SECS:
                continue                       # never narrate stale backlog on boot
            if e.get("verdict") != "cleared":
                continue                       # rejections stay quiet -- the log has them
            sym = e.get("symbol") or "?"
            self.speak(f"Buy fired on {sym}.")
        return last_seen

    @staticmethod
    def _read_records(path: str) -> list[dict]:
        """[] for missing/unreadable/mid-write files -- same fail-soft rule
        as tui.py's own _load_activity (the writer is a different process
        doing temp-file+os.replace, so torn reads are rare but genuinely
        possible on the wrong poll instant)."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []


def _entry_ts(entry: dict, key: str) -> float:
    try:
        return float(entry.get(key) or entry.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0

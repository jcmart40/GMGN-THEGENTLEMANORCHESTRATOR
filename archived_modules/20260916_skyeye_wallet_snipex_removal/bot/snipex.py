"""X/Twitter-following signal source (2026-09-15) -- fourth piece of the
CopyTrade toolkit integration, GMGN-bot ("the Gentleman"), built AHEAD of
Token Snipe a second time at the user's explicit request ("yes SnipeX
next").

ARCHITECTURE: same as every other piece -- this is NOT a separate trader
with its own rules, it's a SIGNAL SOURCE only, feeding candidates through
bot/screener.py's existing `_autonomous_buy()`, same risk gates, same
paper/live execution, same exit management. See bot/wallet_follow.py's own
module docstring for the full "one unified engine" reasoning. Unlike
wallet_follow.py/dev_snipe.py, there is no sell-side mirror here -- the
original SnipeX product had its own TP/SL tiers, but per this project's
locked architecture, exits are handled entirely by the SAME shared
exit_manager.py every other position uses. Nothing source-specific to
build for the sell side.

CREDENTIAL: xAI's (Grok's) `x_search` tool (docs.x.ai/developers/tools/
x-search), NOT a real X/Twitter Developer credential -- the user only had
xAI API keys on hand and chose to proceed with those rather than wait on
X's own developer approval process. See
[[project_gmgn_copytrade_toolkit_research_20260914]] for the full
real-mechanism writeup, including a real injection-risk discussion.

MECHANISM, verified live 2026-09-15 before writing this file:
  - `x_search`'s `allowed_x_handles` parameter only FILTERS results -- it
    does NOT tell Grok which account to search. The handle must be named
    explicitly in the natural-language prompt every single poll, or Grok
    has nothing to search for. Confirmed live: naming a handle only in
    `allowed_x_handles` produced "I don't have a specific account...";
    naming it in the prompt text itself worked correctly and returned
    real, verifiable posts with citation URLs.
  - `from_date`/`to_date` genuinely constrain results -- confirmed live
    against a real ~29-day-old historical window, which correctly
    returned only posts from that window (not today's). Used here for a
    rolling "since last poll" search window.
  - X status IDs (Twitter Snowflake IDs, parsed out of the returned post
    URLs) are reliably time-ordered -- confirmed by comparing IDs across
    two real, time-separated test windows (older window = lower IDs).
    Used as the actual "what's new" dedup anchor, the same role
    create_timestamp/timestamp play for dev-snipe/wallet-follow, since
    this tool has no clean structured timestamp field of its own to read.
  - Every candidate contract address is extracted from the exact quoted
    post text with a strict regex (never trusted purely to Grok's own
    paraphrase of what a post said), and still has to clear the exact
    same security.check_arbitrary_token_candidate() real on-chain check
    every other signal source uses before anything can buy. This bounds
    (does not eliminate) the real injection risk of watching
    human-authored social media text -- worse than the on-chain-metadata
    injection attempt gmgn-cli already caught and neutralized once this
    same day, since a shill post is deliberately adversarial text, not
    just a stray metadata field.
  - Scope, stated honestly: this covers new posts/replies by the watched
    account. Does NOT detect reposts of someone else's content or
    profile-bio changes specifically -- both were part of the original
    SnipeX product's spec but aren't verified working through this
    mechanism, and are not claimed as covered here.

Pilot handle: Cented (@Cented7) -- the SAME wallet already chosen for
wallet-follow, cross-confirmed via GMGN's own portfolio stats
(twitter_bind=True, is_blue_verified=True, followers_count=810,843), not a
cold guess. Multiple handles supported from day one via a watchlist file,
same pattern as Dev Snipe -- and since the underlying tool needs the
handle named in the prompt anyway, multiple handles can be checked in ONE
Grok call rather than one call per handle (cheaper than Dev Snipe's
per-dev polling).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import bot.file_lock as file_lock
import bot.security as security
from config import config

log = logging.getLogger("gmgn-agent")

_SOURCE = "snipex"

# Twitter/X status IDs are Snowflake IDs -- large integers, always present
# in both the tweet URL format (x.com/<handle>/status/<id>) and the
# citation URL format (x.com/i/status/<id>) seen in live testing.
_STATUS_ID_RE = re.compile(r"status/(\d+)")

# Solana: base58 alphabet (excludes 0, O, I, l), 32-44 chars -- same shape
# used everywhere else in this codebase for a mint address.
_SOLANA_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
# Ethereum-shaped address (0x + 40 hex) -- included since SnipeX's target
# accounts could plausibly post either chain's contracts; config.chain
# still gates which chain any resulting candidate actually gets bought on.
_ETH_ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


def _load_watchlist() -> list[str]:
    """JSON array of X handles (with or without a leading '@', normalized
    here), user-populated directly -- same no-free-text-parsing precedent
    as every other watchlist in this project. Missing/invalid file just
    means nothing is watched yet."""
    if not os.path.exists(config.snipex_watchlist_file):
        return []
    try:
        with open(config.snipex_watchlist_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log.warning("snipex: watchlist file is not a JSON array -- ignoring it this cycle.")
            return []
        seen: set[str] = set()
        handles = []
        for h in data:
            h = str(h).lstrip("@").strip() if h else ""
            if h and h not in seen:
                seen.add(h)
                handles.append(h)
        return handles
    except Exception:
        log.exception("snipex: watchlist file unreadable -- treating as empty this cycle.")
        return []


def _load_state() -> dict:
    """Keyed by handle -> {"last_seen_status_id", "last_poll_time_iso"}.
    Both None signals "never polled this handle before" -- same first-run
    baseline-seeding pattern as every other signal source here, avoids
    firing on a handle's entire recent history the first time it's added."""
    if not os.path.exists(config.snipex_state_file):
        return {}
    try:
        with open(config.snipex_state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.exception("snipex state file unreadable -- starting fresh rather than blocking the loop.")
        return {}


def _save_state(state: dict) -> None:
    directory = os.path.dirname(config.snipex_state_file) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, config.snipex_state_file)


def _extract_status_ids(text: str, citations: list[str]) -> dict[int, str]:
    """Maps status ID -> the post's own URL (for logging). Pulls from both
    the citation URLs (always present when the tool actually ran) and the
    prose content (defensive -- covers the model quoting a URL inline
    without it also appearing as a citation)."""
    ids: dict[int, str] = {}
    for url in list(citations) + [text]:
        for match in _STATUS_ID_RE.finditer(url):
            ids[int(match.group(1))] = url
    return ids


def _extract_candidate_addresses(text: str) -> set[str]:
    """Strict regex extraction from the exact post text Grok quoted --
    deliberately never trusts Grok's own paraphrase or summary of "what
    address was mentioned." A false negative (missing a real call) is
    acceptable; a false positive from a hallucinated/paraphrased address
    is not, since it would waste a real security-check API call and could
    theoretically be steered by injected text in the source post."""
    found = set(_SOLANA_ADDR_RE.findall(text)) | set(_ETH_ADDR_RE.findall(text))
    return found


def _extract_new_candidate_addresses(content: str, new_ids: set[int]) -> set[str]:
    """Scopes extraction to text actually tied to a NEW post, not the whole
    response blob -- real gap found in a self-audit 2026-09-15, before this
    ever ran on genuinely new activity: if Grok ever pads a response with
    old context alongside a real new post (quoting/referencing an
    already-processed post for continuity), extracting from the entire
    blob would re-surface an address from OLD, already-handled content as
    if it were new, every single cycle it kept appearing. In every
    observed real response, each post's own status-ID URL appears right
    after that post's text (e.g. "Exact text: ...\\nURL: .../status/<id>"),
    so splitting on each status-ID occurrence and keeping only the chunk
    IMMEDIATELY PRECEDING it correctly buckets that post's own text with
    its own ID. Any trailing text after the last ID (e.g. closing remarks)
    is scanned too, but only when at least one post this cycle is
    genuinely new -- conservative default, doesn't drop a real signal just
    because it wasn't clearly attributed to a specific post."""
    found: set[str] = set()
    last_end = 0
    for match in _STATUS_ID_RE.finditer(content):
        chunk = content[last_end:match.end()]
        if int(match.group(1)) in new_ids:
            found |= _extract_candidate_addresses(chunk)
        last_end = match.end()
    if new_ids:
        found |= _extract_candidate_addresses(content[last_end:])
    return found


def _query_x_search(handle: str, since: datetime, now: datetime) -> tuple[str, list[str]]:
    """Blocking call -- run via asyncio.to_thread by callers. Returns
    (content, citations). Raises RuntimeError on any failure (missing key,
    SDK error) so callers can log and skip this cycle rather than crash."""
    if not config.xai_api_key:
        raise RuntimeError("XAI_API_KEY not configured")
    from xai_sdk import Client
    from xai_sdk.chat import user
    from xai_sdk.tools import x_search

    client = Client(api_key=config.xai_api_key)
    chat = client.chat.create(
        model=config.snipex_model,
        tools=[x_search(allowed_x_handles=[handle], from_date=since, to_date=now)],
    )
    chat.append(user(
        f"Search X for every new post (including replies) from the account @{handle} "
        f"between {since.isoformat()} and {now.isoformat()}. For each post, give the "
        f"EXACT verbatim text (do not paraphrase or summarize) and the post URL. "
        f"If there are none, say NONE."
    ))
    response = chat.sample()
    return response.content or "", list(response.citations or [])


async def _handle_candidate(address: str, handle: str, post_url: str) -> None:
    try:
        # security.check_arbitrary_token_candidate() is async and does its
        # own internal thread-offloading + shared rate_limiter reporting
        # (2026-09-15 audit fix) -- no asyncio.to_thread() wrapper needed
        # here anymore.
        result = await security.check_arbitrary_token_candidate(address, config.chain)
    except Exception:
        log.exception("snipex: security check failed for %s (from @%s) -- skipping.", address, handle)
        return

    if not result.passed:
        log.info("snipex: %s (from @%s, %s) REJECTED -- %s", address, handle, post_url, "; ".join(result.reasons))
        return

    candidate = {
        "address": address,
        "symbol": address[:8],
        "name": address[:8],
        "market_cap": None,
        "liquidity": None,
    }
    empty_trenches = security.SecurityResult(passed=True, reasons=[])

    log.warning("[bold cyan]SNIPEX SIGNAL[/bold cyan] %s -- @%s posted %s, buying via the "
                "Gentleman's own buy path.", address, handle, post_url)

    import bot.screener as screener
    await screener._autonomous_buy(candidate, empty_trenches, result, source=_SOURCE)


async def _poll_handle_once(handle: str, handle_state: dict) -> dict:
    now = datetime.now(timezone.utc)
    last_seen_id = handle_state.get("last_seen_status_id")
    last_poll_iso = handle_state.get("last_poll_time_iso")

    if last_seen_id is None or last_poll_iso is None:
        # First poll ever for this handle -- a wide-but-bounded lookback
        # just to seed a real baseline status ID, WITHOUT acting on
        # anything found. Same first-run reasoning as every other signal
        # source here.
        since = now - timedelta(hours=1)
    else:
        try:
            since = datetime.fromisoformat(last_poll_iso)
        except ValueError:
            since = now - timedelta(hours=1)

    try:
        content, citations = await asyncio.to_thread(_query_x_search, handle, since, now)
    except Exception:
        log.exception("snipex: x_search query failed for @%s -- skipping this handle this cycle.", handle)
        return handle_state

    post_ids = _extract_status_ids(content, citations)

    if last_seen_id is None:
        newest_id = max(post_ids.keys(), default=0)
        log.info("snipex: first poll for @%s, baseline seeded (status_id=%s) -- "
                  "watching for new posts from here.", handle, newest_id)
        return {"last_seen_status_id": newest_id, "last_poll_time_iso": now.isoformat()}

    new_ids = {sid: url for sid, url in post_ids.items() if sid > last_seen_id}
    newest_id = max([last_seen_id] + list(new_ids.keys()))

    if new_ids:
        # Extract candidates from the FULL response content, not per-post --
        # the model's own formatting (numbered list, etc.) isn't reliable
        # enough to slice text precisely to one post. Any address found is
        # attributed to one of the new posts' URLs, which is only used for
        # logging context, never for the actual buy decision (that's the
        # address + the real security check).
        newest_url = next(iter(new_ids.values()))
        addresses = _extract_new_candidate_addresses(content, set(new_ids.keys()))
        for address in addresses:
            try:
                await _handle_candidate(address, handle, newest_url)
            except Exception:
                log.exception("snipex: candidate handling crashed for %s (@%s) -- continuing.", address, handle)

    return {"last_seen_status_id": newest_id, "last_poll_time_iso": now.isoformat()}


async def _poll_once() -> None:
    handles = _load_watchlist()
    if not handles:
        return  # nothing to watch yet

    state = _load_state()
    for handle in handles:
        handle_state = state.get(handle, {"last_seen_status_id": None, "last_poll_time_iso": None})
        try:
            state[handle] = await _poll_handle_once(handle, handle_state)
        except Exception:
            log.exception("snipex: poll crashed for @%s -- continuing with the rest of the watchlist.", handle)
    _save_state(state)


async def run_forever() -> None:
    if not config.snipex_enabled:
        log.info("snipex: disabled (SNIPEX_ENABLED=0), loop not starting.")
        return
    if not config.xai_api_key:
        log.warning("snipex: SNIPEX_ENABLED=1 but XAI_API_KEY is not set -- loop not starting.")
        return
    log.warning("snipex: watching %d X handle(s) every %.0fs.",
                len(_load_watchlist()), config.snipex_poll_interval_secs)
    while True:
        try:
            with file_lock.cross_process_lock(config.snipex_state_lock_file):
                await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- one bad cycle must never kill the whole loop
            log.exception("snipex: poll cycle crashed -- continuing on the next one.")
        await asyncio.sleep(config.snipex_poll_interval_secs)

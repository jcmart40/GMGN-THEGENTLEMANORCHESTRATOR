"""Thin wrapper around the gmgn-cli Node.js tool (npm install -g gmgn-cli).

GMGN's own AI-agent integration is a CLI, not a documented raw HTTP API --
see https://github.com/GMGNAI/gmgn-skills for the full skill reference. This
module shells out to that CLI and parses its JSON output, the same way any
other language would integrate with a CLI-first tool. Verified working
2026-09-05 against a real API key (~/.config/gmgn/.env, GMGN_API_KEY) with a
live `market trending` call.

Auth: the CLI itself reads credentials from ~/.config/gmgn/.env (global) or
a project-local .env (takes precedence) -- nothing in this module ever
touches the API key or private key directly; gmgn-cli handles that.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any

# Resolved once at import time, not per-call -- shutil.which() is the fix
# for a real Windows issue found live (2026-09-05): npm's global install
# creates gmgn-cli.cmd/.ps1 wrapper scripts, and subprocess.run(["gmgn-cli"])
# doesn't resolve that extension the way a shell does, failing with
# WinError 2 (file not found) even though `gmgn-cli ...` works fine typed
# directly into a terminal. shutil.which() correctly checks PATHEXT
# (.cmd/.bat/.exe) on Windows and returns the real resolvable path; on
# Linux/Mac this just finds the plain executable, so this stays portable.
_GMGN_CLI_PATH = shutil.which("gmgn-cli")


class GmgnCliError(Exception):
    """Raised when gmgn-cli exits non-zero or returns unparseable output."""


class GmgnRateLimitError(GmgnCliError):
    """Raised specifically when gmgn-cli's own failure output indicates GMGN's
    platform-side rate limit (HTTP 429), not a generic CLI/network failure --
    confirmed live 2026-09-06 (a real live-buy attempt hit "HTTP 429
    code=429 error=RATE_LIMIT_EXCEEDED"). A subclass of GmgnCliError, not a
    separate exception, so any existing `except GmgnCliError` catch site
    still catches this too -- but callers that specifically need to react
    differently to a rate limit (back off, don't just log-and-skip) can
    catch this narrower type first. See bot/screener.py's screen_once() for
    the backoff logic built on top of this.

    retry_after_secs (2026-09-06, real incident): GMGN's own error text
    tells you exactly how long the ban has left ("Rate limit resets at
    ...(~300s remaining)"). The first version of this class didn't expose
    that, so bot/screener.py's backoff used a generic fixed schedule
    (5s/10s/20s/45s cooldown) -- far shorter than a real 300s ban. On
    restart the bot immediately retried, got rate-limited again, and each
    retry EXTENDED the ban further (GMGN's own words: "repeated requests
    can extend the ban") -- confirmed live: a ban that should have cleared
    kept resetting to a fresh ~300s every time the bot restarted and
    immediately re-hit it. retry_after_secs is parsed from that exact
    message so callers can wait the REAL remaining time instead of
    guessing short and making it worse."""

    def __init__(self, message: str, retry_after_secs: float | None = None):
        super().__init__(message)
        self.retry_after_secs = retry_after_secs


def _parse_retry_after_secs(error_text: str) -> float | None:
    """Extracts the "(~Ns remaining)" figure GMGN's own rate-limit message
    reports, e.g. "Rate limit resets at 2026-09-06 09:57:14 GMT-07:00
    (~300s remaining)". Deliberately parsed from this figure, not the
    absolute reset timestamp next to it -- avoids any timezone-parsing
    risk, GMGN has already done that math for us."""
    m = re.search(r"\(~(\d+)s remaining\)", error_text)
    return float(m.group(1)) if m else None


def run(*args: str, timeout: float = 15.0) -> Any:
    """Run `gmgn-cli <args>` and return its parsed JSON response.

    Raises GmgnCliError on a non-zero exit or invalid JSON -- callers get a
    clear failure instead of a silent None, since every real command here is
    read-only market data (safe to raise) rather than a live trade action
    (which will get its own, more careful error handling once built).

    Response shape is NOT uniform across commands -- confirmed live
    2026-09-05: `market trending` wraps its response in
    {"code": 0, "data": {...}, "message": ..., "reason": ...}, but
    `token info` returns the raw data object directly with no envelope and
    no "code" key at all. Checking `parsed.get("code") != 0` unconditionally
    (the original version of this function) produced a false-positive error
    on every unwrapped command, since a missing key defaults to None != 0.
    Fixed by only treating "code" as a real error signal when the key is
    actually present -- an unwrapped response has no code key, so it's
    never mistaken for a failure. Callers use _unwrap() below to pull the
    right thing out of either shape.

    encoding="utf-8" explicit (2026-09-05, real bug found live): Windows'
    subprocess.run(text=True) decodes with the system's default codepage
    (cp1252 here), not UTF-8 -- `market trenches` real output includes
    non-ASCII characters (emoji/non-English token names/descriptions are
    common in this data) that cp1252 can't decode, crashing with
    UnicodeDecodeError deep in a background reader thread and silently
    leaving result.stdout as None. errors="replace" is a deliberate safety
    net on top, not a substitute -- this is a black-box external CLI, a
    single malformed byte sequence in one field must never crash every
    other call that happens to hit it.

    Rewritten from plain subprocess.run(timeout=...) to Popen +
    communicate() with an explicit process-tree kill on timeout (2026-09-06,
    real production hang found live): gmgn-cli resolves to gmgn-cli.CMD (a
    Windows batch wrapper spawning a real node.exe underneath it). A plain
    subprocess.run(timeout=X) only kills the IMMEDIATE child (the .CMD
    wrapper) when the timeout fires -- the node.exe process it spawned can
    survive as an orphan still holding the stdout/stderr pipes open, and
    subprocess.run() then hangs internally draining those pipes as part of
    its own cleanup, past the timeout having already "fired." Confirmed
    live: a real `swap` call froze the whole engine process for 5+ minutes
    past its 30s timeout, with no exception ever raised -- not a missing
    try/except, the underlying timeout mechanism itself never actually
    completed. Fixed by using `taskkill /F /T /PID <pid>` on timeout, which
    kills the whole process TREE (the wrapper AND every process it spawned),
    not just the one PID Python has a handle to. stdin=DEVNULL added too
    (a real gap, though not this bug's actual cause) -- prevents a
    different hang class where the CLI unexpectedly waits on interactive
    input despite --yes."""
    if _GMGN_CLI_PATH is None:
        raise GmgnCliError("gmgn-cli not found on PATH -- run 'npm install -g gmgn-cli'")
    try:
        proc = subprocess.Popen(
            [_GMGN_CLI_PATH, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except FileNotFoundError as exc:
        raise GmgnCliError("gmgn-cli not found on PATH -- run 'npm install -g gmgn-cli'") from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            # /T = kill the whole process tree, not just this PID -- see
            # this function's own docstring for why proc.kill() alone
            # isn't enough here.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True, timeout=10)
        else:
            proc.kill()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # tree kill already sent; don't hang a second time waiting on it
        raise GmgnCliError(f"gmgn-cli timed out after {timeout}s: {args}") from exc

    if proc.returncode != 0:
        combined = (stderr or "").strip() or (stdout or "").strip()
        # Detect GMGN's own platform-side rate limit specifically (2026-09-06,
        # confirmed live: "HTTP 429 code=429 error=RATE_LIMIT_EXCEEDED... IP
        # rate limit exceeded... repeated requests can extend the ban").
        # Checked as substrings on the raw combined output rather than trying
        # to parse a structured field -- gmgn-cli prints this as plain text
        # on a CLI-level failure, not as JSON (the JSON-parse path below
        # never runs when returncode != 0).
        if "429" in combined or "RATE_LIMIT_EXCEEDED" in combined or "RATE_LIMIT_BANNED" in combined:
            raise GmgnRateLimitError(
                f"gmgn-cli hit GMGN's rate limit for {args}: {combined}",
                retry_after_secs=_parse_retry_after_secs(combined),
            )
        raise GmgnCliError(f"gmgn-cli exited {proc.returncode} for {args}: {combined}")
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GmgnCliError(f"gmgn-cli returned non-JSON output for {args}: {(stdout or '')[:200]!r}") from exc

    if isinstance(parsed, dict) and "code" in parsed and parsed["code"] != 0:
        raise GmgnCliError(f"gmgn-cli reported an API error for {args}: {parsed.get('message') or parsed.get('reason')}")
    return parsed


def _unwrap(parsed: Any, default: Any) -> Any:
    """Pull the real payload out of whichever response shape run() returned.
    Confirmed live (2026-09-05) there are (at least) THREE distinct shapes
    across different gmgn-cli commands, not one consistent envelope:
      1. {"code": 0, "data": {...}, "message": ..., "reason": ...}
         -- e.g. `market trending`
      2. bare object, no wrapper at all -- e.g. `token info`, `token security`
      3. {"list": [...]}, no "code"/"data" at all -- e.g. `token holders`,
         `track smartmoney` (two independent confirmations before
         generalizing this, not a guess off one data point)
    `default` (typically {} or []) covers the case where none of the above
    match, matching every wrapper function's old `.get("data", default)`
    behavior for the enveloped case. If a FOURTH shape turns up, add it
    here with its own real, verified example -- don't guess ahead of what's
    actually been seen."""
    if isinstance(parsed, dict):
        if "data" in parsed:
            return parsed["data"]
        if "list" in parsed:
            return parsed["list"]
    return parsed if parsed is not None else default


def _build_args(*positional: str, **flags: Any) -> list[str]:
    """Convert Python kwargs into gmgn-cli --flag args. A None value means
    "not provided" and is skipped entirely (relies on the CLI's own
    default), matching every wrapper function's own None-default kwargs
    below. A list/tuple value repeats the flag once per item (matches
    repeatable flags like `--signal-type <n...>` / `--tag <tag>`).
    Underscore-to-hyphen is automatic (order_by -> --order-by) -- this
    covers every real flag seen in `gmgn-cli <cmd> --help` so far; a
    future command with a flag that DOESN'T follow this convention should
    build its args by hand instead of forcing it through here."""
    args = list(positional)
    for key, value in flags.items():
        if value is None:
            continue
        flag = "--" + key.replace("_", "-")
        if isinstance(value, (list, tuple)):
            for v in value:
                args.extend([flag, str(v)])
        elif isinstance(value, bool):
            if value:
                args.append(flag)  # store-true style flag (e.g. --hide-open)
        else:
            args.extend([flag, str(value)])
    return args


# -- market ------------------------------------------------------------------

def get_trending(chain: str = "sol", interval: str = "1h", limit: int = 10) -> list[dict]:
    """Live trending-token rankings. Real, verified fields per token include
    price/volume/liquidity/market_cap, and GMGN's own pre-computed risk
    signals: rug_ratio, sniper_count, smart_degen_count (smart-money
    holders), renowned_count (KOL holders), bundler_rate,
    rat_trader_amount_rate, is_honeypot, dev_team_hold_rate,
    top_10_holder_rate -- see a real captured response for the full field
    list before assuming any field not explicitly listed here."""
    parsed = run("market", "trending", *_build_args(chain=chain, interval=interval, limit=limit))
    return _unwrap(parsed, {}).get("rank", [])


def get_kline(
    address: str, chain: str = "sol", resolution: str = "5m",
    from_ts: int | None = None, to_ts: int | None = None,
) -> list[dict]:
    """OHLCV candlestick data. from_ts/to_ts are Unix seconds -- named with
    a _ts suffix (not the CLI's own bare `from`/`to`) since `from` is a
    Python reserved word and can't be a keyword argument name."""
    args = _build_args(chain=chain, address=address, resolution=resolution)
    if from_ts is not None:
        args.extend(["--from", str(from_ts)])
    if to_ts is not None:
        args.extend(["--to", str(to_ts)])
    parsed = run("market", "kline", *args)
    return _unwrap(parsed, [])


def get_trenches(
    chain: str = "sol", types: list[str] | None = None, limit: int = 80,
    filter_preset: str | None = None, sort_by: str | None = None,
    direction: str | None = None, **filters: Any,
) -> dict:
    """New/near-completion/completed launchpad tokens, with GMGN's own huge
    server-side filter set (rug_ratio, bundler_rate, insider_ratio,
    smart_degen_count, top_holder_rate, token age, and 40+ more -- see
    `gmgn-cli market trenches --help` for the full list). Pass any of those
    as snake_case kwargs (e.g. min_rug_ratio=0.0, max_rug_ratio=0.3) and
    they flow straight through via **filters/_build_args -- deliberately
    not hand-listing all 40+ here, that's the CLI's own job to validate.
    Returns the full `data` dict (keyed by category: new_creation/
    near_completion/completed), not a flattened list, since callers likely
    care which category a result came from.

    `limit` is enforced CLIENT-SIDE here (2026-09-05, real bug found and
    fully traced): GMGN's own /v1/trenches server endpoint does not honor
    the limit it's sent -- confirmed by reading gmgn-cli's actual source
    (dist/client/OpenApiClient.js's buildTrenchesBody()), which correctly
    builds {"limit": <value>, ...} into the POST body every time; a raw
    CLI call with --limit 1 vs --limit 80 returns the identical ~60 items
    per category regardless. Not a bug in this wrapper or the CLI -- no
    matching GitHub issue found either, so flagging it here rather than
    trusting the server to ever actually apply it. Truncating each
    category to `limit` after the fact is the only way to make this
    function's own `limit` parameter mean anything real to a caller."""
    args = _build_args(
        chain=chain, type=types, limit=limit,
        filter_preset=filter_preset, sort_by=sort_by, direction=direction,
        **filters,
    )
    parsed = run("market", "trenches", *args)
    data = _unwrap(parsed, {})
    if isinstance(data, dict):
        return {
            cat: (items[:limit] if isinstance(items, list) else items)
            for cat, items in data.items()
        }
    return data


def get_signals(
    chain: str = "sol", signal_type: list[int] | None = None,
    mc_min: float | None = None, mc_max: float | None = None, **filters: Any,
) -> dict:
    """Token signals (price spikes, smart-money buys, large buys, etc, 21
    signal types -- max 50 results per group). signal_type is a list of
    ints (1-21); omit for all types."""
    args = _build_args(chain=chain, signal_type=signal_type, mc_min=mc_min, mc_max=mc_max, **filters)
    parsed = run("market", "signal", *args)
    return _unwrap(parsed, {})


# -- token ---------------------------------------------------------------

def get_token_info(address: str, chain: str = "sol") -> dict:
    """Basic token info + realtime price."""
    parsed = run("token", "info", *_build_args(chain=chain, address=address))
    return _unwrap(parsed, {})


def get_token_security(address: str, chain: str = "sol") -> dict:
    """Security metrics: honeypot, open-source, renounced, buy/sell tax.

    Real field names verified live (2026-09-05) -- use these, NOT the
    is_honeypot/is_open_source/is_blacklist/is_renounced fields also
    present in the response: those all returned null on a real token,
    they're unused/deprecated aliases. The actual signal lives in:
      honeypot (0/1), open_source (0/1), blacklist (0/1),
      renounced_mint (bool), renounced_freeze_account (bool),
      buy_tax / sell_tax (string percentages)."""
    parsed = run("token", "security", *_build_args(chain=chain, address=address))
    return _unwrap(parsed, {})


def get_token_holders(
    address: str, chain: str = "sol", limit: int = 20,
    order_by: str | None = None, direction: str | None = None,
    tag: str | None = None,
) -> list[dict]:
    """Top holders, optionally filtered by wallet tag (smart_degen/
    renowned/fresh_wallet/dev/sniper/rat_trader/bundler/etc). Response is
    the {"list": [...]} shape -- see _unwrap()'s own docstring."""
    parsed = run("token", "holders", *_build_args(
        chain=chain, address=address, limit=limit,
        order_by=order_by, direction=direction, tag=tag,
    ))
    return _unwrap(parsed, [])


def get_token_traders(address: str, chain: str = "sol", limit: int = 20) -> list[dict]:
    """Top traders (by realized/unrealized PnL and volume) for a token."""
    parsed = run("token", "traders", *_build_args(chain=chain, address=address, limit=limit))
    return _unwrap(parsed, [])


# -- portfolio / wallets ---------------------------------------------------

def get_wallet_holdings(wallet: str, chain: str = "sol", limit: int = 20, **filters: Any) -> list[dict]:
    """Current token holdings for a wallet. filters covers order_by/
    direction/interval/hide_abnormal/hide_airdrop/hide_closed/hide_open/
    tx30d -- see `gmgn-cli portfolio holdings --help`."""
    parsed = run("portfolio", "holdings", *_build_args(chain=chain, wallet=wallet, limit=limit, **filters))
    return _unwrap(parsed, [])


def get_wallet_stats(wallets: list[str], chain: str = "sol", period: str = "7d") -> dict:
    """Trading stats (win rate, PnL) for one or more wallets at once."""
    parsed = run("portfolio", "stats", *_build_args(chain=chain, wallet=wallets, period=period))
    return _unwrap(parsed, {})


def get_wallet_activity(
    wallet: str, chain: str = "sol", types: list[str] | None = None,
    token: str | None = None, limit: int = 20, cursor: str | None = None,
) -> list[dict]:
    """Transaction history (buy/sell/transferIn/transferOut/add/remove) for
    ANY wallet -- built 2026-09-15 for Dev Snipe research. Unlike `track
    follow-wallet` (bot/wallet_follow.py), this does NOT require the target
    wallet be Tracked via GMGN's platform UI first -- confirmed live against
    two different, never-Tracked wallets, each correctly returning that
    specific wallet's own real activity (right `wallet` field, right token
    symbols matching known creations). This is because `portfolio activity`
    uses lighter "exist auth" (API key only, weight 3) rather than the
    signed/account-scoped "critical auth" that caused follow-wallet's
    Track-first requirement (skills/gmgn-portfolio/SKILL.md: "portfolio
    holdings uses critical auth... All other portfolio commands use exist
    auth"). Real response envelope is {"activities": [...], "next": ...}
    -- NOT "list" or "data", so _unwrap() doesn't apply here; reach into
    "activities" directly, same pattern as get_trending()'s own "rank" dig.
    `types` is repeatable (e.g. ["sell"]) per the CLI's own --type flag.
    Does NOT return the `next` cursor -- callers needing pagination should
    call run() directly instead of this convenience wrapper."""
    parsed = run("portfolio", "activity", *_build_args(
        chain=chain, wallet=wallet, type=types, token=token, limit=limit, cursor=cursor,
    ))
    if isinstance(parsed, dict) and "activities" in parsed:
        return parsed["activities"]
    return []


def get_wallet_created_tokens(
    wallet: str, chain: str = "sol", order_by: str | None = None,
    direction: str | None = None, migrate_state: str | None = None,
) -> dict:
    """Tokens created by a developer wallet -- built 2026-09-15 for Dev
    Snipe. Exist auth (API key only, weight 2), same as get_wallet_activity
    above -- no Track-first requirement. Real response is a bare object
    (not "list"/"data" wrapped): top-level inner_count/open_count/
    open_ratio/last_create_timestamp/creator_ath_info, plus a `tokens`
    array (per skills/gmgn-portfolio/SKILL.md). CRITICAL per that doc:
    "Total created = inner_count + open_count. Do NOT use len(tokens) as
    the total -- the tokens array is capped at 100 entries and may be
    truncated." Returns the full dict (not just `tokens`) so callers can
    see create_timestamp ordering context and the counts -- _unwrap()
    doesn't apply since this shape has neither "data" nor "list" at the
    top level."""
    parsed = run("portfolio", "created-tokens", *_build_args(
        chain=chain, wallet=wallet, order_by=order_by, direction=direction, migrate_state=migrate_state,
    ))
    return parsed if isinstance(parsed, dict) else {}


# -- track (KOL / smart money / followed wallets) -------------------------

def get_smartmoney_trades(chain: str = "sol", limit: int = 100, side: str | None = None) -> list[dict]:
    """Real-time Smart Money buy/sell trade feed."""
    parsed = run("track", "smartmoney", *_build_args(chain=chain, limit=limit, side=side))
    return _unwrap(parsed, [])


def get_kol_trades(chain: str = "sol", limit: int = 100, side: str | None = None) -> list[dict]:
    """Real-time KOL buy/sell trade feed."""
    parsed = run("track", "kol", *_build_args(chain=chain, limit=limit, side=side))
    return _unwrap(parsed, [])


def get_follow_wallet_trades(
    chain: str = "sol", wallet: str | None = None, limit: int = 10,
    side: str | None = None, min_amount_usd: float | None = None,
    max_amount_usd: float | None = None,
) -> list[dict]:
    """Trade feed for wallets you follow on GMGN (or one specific wallet if
    `wallet` is given) -- the direct equivalent of Axiom-bot's own
    copy-trade wallet monitor, but server-side instead of a custom Solana
    RPC websocket subscription."""
    parsed = run("track", "follow-wallet", *_build_args(
        chain=chain, wallet=wallet, limit=limit, side=side,
        min_amount_usd=min_amount_usd, max_amount_usd=max_amount_usd,
    ))
    return _unwrap(parsed, [])


# -- swap (live execution, bot/executor.py only) --------------------------

def submit_swap(
    chain: str, from_wallet: str, input_token: str, output_token: str,
    amount: int, slippage: float, priority_fee: float | None = None,
    condition_orders: list[dict] | None = None,
    sell_ratio_type: str | None = None,
    anti_mev: bool = False,
) -> dict:
    """Submit a real on-chain swap via `gmgn-cli swap --yes --raw`.

    `--yes` (skip interactive confirmation) itself requires the env var
    GMGN_ALLOW_AUTOMATED_TRADES=1 to be set (confirmed via `gmgn-cli swap
    --help`, 2026-09-05) -- gmgn-cli's OWN deliberate safety gate. This
    function never sets that variable itself; it's inherited from whatever
    the calling process's environment already has, same as GMGN_API_KEY/
    GMGN_PRIVATE_KEY. bot/executor.py checks for it explicitly before ever
    calling this, so a missing gate produces a clear ExecutionBlocked
    error here rather than a confusing CLI failure.

    `amount` is the RAW smallest-unit amount (lamports for SOL, or the
    token's own raw base units per its `decimals`) -- gmgn-cli's own
    `--amount` flag docs say "raw amount (smallest unit)"; converting from
    a human-readable/decimal amount is the caller's job (see
    bot/executor.py's Decimal-based conversion using a holding's real
    `token.decimals` field, verified live 2026-09-05 against
    `portfolio holdings` -- balance there is a decimal STRING, not raw
    units, a real distinction this project already got bitten by once with
    wrong field-name assumptions elsewhere).

    `condition_orders` (2026-09-26): optional native take-profit/stop-loss
    (and trailing variants) attached to this SAME buy transaction, per
    gmgn-skills' documented `--condition-orders` swap flag -- see
    bot/executor.py's execute_buy() for how the Gentleman builds this list
    from his own effective stop_loss_pct/take_profit_pct at buy time. This
    is a genuinely different mechanism than exit_manager.py's polling loop:
    it fires server-side, the instant price crosses the line, with no
    2-second poll gap to overshoot through -- see config.py's real
    emergency-drawdown overshoot data (every single one blew 10-80+ points
    past the line) for the exact problem this responds to. It does NOT
    replace the polling exit_manager -- gmgn-cli's condition orders only
    understand a flat price move, nothing like max-hold/liquidity-drain/
    profit-giveback/queue-pressure, which still need the existing loop.
    JSON-encoded here (not passed through _build_args' generic list
    handling, which repeats a flag once per list item -- wrong shape for
    this flag, which wants ONE JSON-array string)."""
    condition_orders_json = json.dumps(condition_orders) if condition_orders else None
    args = _build_args(
        chain=chain, **{"from": from_wallet}, input_token=input_token,
        output_token=output_token, amount=amount, slippage=slippage,
        priority_fee=priority_fee, yes=True, raw=True,
        condition_orders=condition_orders_json,
        sell_ratio_type=sell_ratio_type,
        anti_mev=anti_mev,
    )
    parsed = run("swap", *args, timeout=30.0)
    return _unwrap(parsed, {})


if __name__ == "__main__":
    # Quick manual smoke test: python bot/gmgn_client.py
    tokens = get_trending(limit=3)
    print(f"Fetched {len(tokens)} trending token(s):")
    for t in tokens:
        print(f"  {t['symbol']:12s} rank={t['rank']} mc=${t['market_cap']:.0f} "
              f"rug_ratio={t['rug_ratio']} smart_money={t['smart_degen_count']}")

    first_addr = tokens[0]["address"]
    print(f"\nToken info for {tokens[0]['symbol']}:")
    info = get_token_info(first_addr)
    print(f"  price={info.get('price', {}).get('price')} liquidity={info.get('liquidity')}")

    print(f"\nSecurity for {tokens[0]['symbol']}:")
    sec = get_token_security(first_addr)
    print(f"  honeypot={sec.get('honeypot')} open_source={sec.get('open_source')} "
          f"renounced_mint={sec.get('renounced_mint')}")

    print(f"\nTop 3 holders for {tokens[0]['symbol']}:")
    for h in get_token_holders(first_addr, limit=3):
        print(f"  {h.get('address', '')[:12]}... amount_percentage={h.get('amount_percentage')} "
              f"tag={h.get('wallet_tag_v2')}")

    print("\nRecent Smart Money trades (sol, limit 3):")
    for t in get_smartmoney_trades(limit=3):
        print(f"  {t.get('base_token', {}).get('symbol')} side={t.get('side')} amount_usd={t.get('amount_usd')}")

    print("\nTrenches new_creation (sol, limit 3):")
    trenches = get_trenches(types=["new_creation"], limit=3)
    for cat, items in trenches.items():
        print(f"  category={cat}: {len(items) if isinstance(items, list) else items}")

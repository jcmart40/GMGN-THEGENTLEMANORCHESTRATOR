"""Combined security audit for one candidate token, built on top of the
fields GMGN's own trenches/token-info endpoints already compute server-side
-- see bot/gmgn_client.py for the individual API wrappers this reads from.

Fails CLOSED throughout: a threshold field that's missing/None (common on
a brand-new token GMGN hasn't finished computing stats for yet) is treated
as a REJECT, never a silent pass. This is the same philosophy Axiom-bot's
own bot/axiom_monitor.py check_safety() proved out across months of real
live trading -- an unknown value is not the same as a safe value.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import bot.ai_review as ai_review
import bot.gmgn_client as gmgn
import bot.rate_limiter as rate_limiter
from config import config


@dataclass
class SecurityResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)  # non-empty only when passed=False
    # Only ever set by check_arbitrary_token_candidate() (2026-09-15 speed
    # fix) -- the real get_token_info() response it already fetched, handed
    # back so callers that pass can build their buy candidate dict (market
    # cap/liquidity) from THIS instead of making a redundant third API call
    # for data already sitting right here. None for every other caller of
    # this dataclass (the trenches-based checks never fetch their own info).
    info: dict | None = None

    def __bool__(self) -> bool:
        return self.passed


def _reject(reasons: list[str]) -> SecurityResult:
    return SecurityResult(passed=False, reasons=reasons)


def check_trenches_fields(token: dict) -> SecurityResult:
    """Run every threshold check that trenches' own response already
    provides, WITHOUT any extra API call -- this is the fast, cheap first
    pass every candidate goes through. Field names/nullability verified
    live against a real trenches response (2026-09-05): rug_ratio,
    top_10_holder_rate, rat_trader_amount_rate, liquidity, holder_count,
    renounced_mint, renounced_freeze_account are all directly present;
    bundler_rate/smart_degen_count/is_honeypot can be None on a token too
    new for GMGN to have computed them yet -- treated as a reject below,
    not skipped."""
    reasons: list[str] = []

    # Active adjustable screening thresholds use the paper-mode AI overrides.
    max_rug_ratio = ai_review.get_effective_max_rug_ratio()
    min_liquidity_usd = ai_review.get_effective_min_liquidity_usd()

    rug_ratio = token.get("rug_ratio")
    if rug_ratio is None:
        reasons.append("rug_ratio missing")
    elif rug_ratio > max_rug_ratio:
        reasons.append(f"rug_ratio {rug_ratio:.2f} > {max_rug_ratio:.2f} max")

    # top_10_holder_rate / bundler_rate / rat_trader_amount_rate enforcement
    # REMOVED (2026-09-19, user's explicit direction after watching live
    # trending charts: "we need to trade regardless of the amount of
    # holders snipers bundlers, they tend to hold on for a while once the
    # coin has good market cap"). These three used to reject a candidate
    # outright for holder-concentration/sniper/bundler activity; user's own
    # real-time observation trading manually is that this doesn't predict
    # an imminent dump the way it might on a raw newborn -- concentrated
    # holders on an already-establishing (trending-sourced) token tend to
    # sit rather than dump immediately. Replaced with a different, more
    # direct signal instead: bot/exit_manager.py's liquidity-drain check
    # (see config.py's liquidity_drain_mcap_threshold_usd/liquidity_drain_pct
    # docstring) watches the pool itself draining once a position has real
    # market cap behind it, rather than trying to infer risk from who's
    # holding. The three unused threshold fields remain in config for
    # compatibility but are not adjustable or enforced -- same
    # precedent as min_holder_count's own removal 2026-09-06.

    liquidity = token.get("liquidity")
    if liquidity is None:
        reasons.append("liquidity missing")
    elif liquidity < min_liquidity_usd:
        reasons.append(f"liquidity ${liquidity:.0f} < ${min_liquidity_usd:.0f} min")

    # meteora_virtual_curve launchpad type EXCLUDED entirely (2026-09-14,
    # user's explicit request -- "get rid of the cancer"). Real data, two
    # separate confirmed examples: Injective and a token that later got
    # instantly zombied both read near-zero liquidity ($0.01-$0.29)
    # regardless of launchpad_status/progress or real market cap -- the
    # liquidity figure for this specific launchpad type cannot be trusted
    # at all, at any stage. By contrast "Pump.fun"/"pump" (verified via
    # SITCOM, a genuine 655-day-old established token, AND TOXBAN, a
    # confirmed real rug -- both showed real, trustworthy numbers) and
    # "ray_launchpad" (verified via BASKET, consistent between the trenches
    # and get_token_info() endpoints) both report real liquidity and stay
    # fully tradeable. Hard-coded, NOT an AI-tunable field -- this is a
    # categorical exclusion the user decided directly. "launchpad" confirmed present on
    # trenches' own cheap response (no extra API call needed) and spelled
    # identically ("meteora_virtual_curve") on both the trenches and
    # get_token_info() endpoints.
    if token.get("launchpad") == "meteora_virtual_curve":
        reasons.append("launchpad meteora_virtual_curve -- liquidity reporting confirmed unreliable, excluded entirely")

    # Market cap trading band (2026-09-16) -- see config.py's own
    # min_market_cap_usd/max_market_cap_usd docstring for the full,
    # real-data-backed reasoning. Both values are AI-tunable in paper mode.
    min_market_cap_usd = ai_review.get_effective_value("min_market_cap_usd")
    max_market_cap_usd = ai_review.get_effective_value("max_market_cap_usd")
    market_cap = token.get("market_cap")
    if market_cap is not None:
        if min_market_cap_usd > 0 and market_cap < min_market_cap_usd:
            reasons.append(f"market_cap ${market_cap:.0f} < ${min_market_cap_usd:.0f} min")
        if max_market_cap_usd > 0 and market_cap > max_market_cap_usd:
            reasons.append(f"market_cap ${market_cap:.0f} > ${max_market_cap_usd:.0f} max")

    # Liquidity-to-market-cap ratio (2026-09-17) -- see config.py's own
    # min_liquidity_to_mcap_ratio docstring for the full reasoning. Catches
    # a top-heavy/artificially-inflated market cap even when liquidity and
    # market_cap individually both clear their own separate absolute
    # floors above. Both fields already read off `token` above -- no extra
    # API call. Skipped (not rejected) if either is missing/zero -- the
    # dedicated liquidity/market_cap checks above already fail-closed on a
    # missing value on their own, no need to double-reject the same gap.
    if liquidity is not None and market_cap is not None and market_cap > 0:
        ratio = liquidity / market_cap
        min_liquidity_to_mcap_ratio = ai_review.get_effective_value("min_liquidity_to_mcap_ratio")
        if min_liquidity_to_mcap_ratio > 0 and ratio < min_liquidity_to_mcap_ratio:
            reasons.append(f"liq_to_mcap_ratio {ratio:.3f} < {min_liquidity_to_mcap_ratio:.3f} min")

    # Buy-pressure / momentum entry filter (2026-09-17) -- see config.py's
    # own min_net_buy_24h_usd/min_buys_24h docstring for the full real-data
    # reasoning (52 low-liquidity + 22 stale-losing exits out of ~99 trades
    # prompted this). All three fields (net_buy_24h, buys_24h,
    # is_wash_trading) confirmed present on trenches' own response, no
    # extra API call. Fail-closed on missing, same as every other check
    # here.
    if token.get("is_wash_trading"):
        reasons.append("is_wash_trading flag set")

    # net_buy_24h/buys_24h key-presence check, not just None-check (2026-09-19,
    # user's explicit switch from trenches to `market trending` as the entry
    # source -- "trenches is brutal, 98% of the tokens never make it").
    # Confirmed live against a real get_trending() response: this endpoint
    # does not return these two keys AT ALL, on any token -- not None, not
    # present-but-null, simply absent. Same failure shape as the bundler_rate
    # bug found 2026-09-06 (a field one endpoint never populates, for any
    # token, any age -- not a freshness signal). Treating "key doesn't exist
    # on this response shape" as fail-closed would silently reject 100% of
    # every trending candidate, since trenches' own copy of this check was
    # written assuming a field that endpoint always provides. Checking for
    # actual key presence (not token.get(...) is None) distinguishes "this
    # endpoint never returns this field" (skip) from "trenches returned it as
    # null on a real candidate" (still fail-closed, unchanged).
    if "net_buy_24h" in token:
        net_buy_24h = token.get("net_buy_24h")
        if net_buy_24h is None:
            reasons.append("net_buy_24h missing")
        else:
            min_net_buy_24h_usd = ai_review.get_effective_value("min_net_buy_24h_usd")
            if net_buy_24h < min_net_buy_24h_usd:
                reasons.append(
                    f"net_buy_24h ${net_buy_24h:.0f} < ${min_net_buy_24h_usd:.0f} min")

    if "buys_24h" in token:
        buys_24h = token.get("buys_24h")
        if buys_24h is None:
            reasons.append("buys_24h missing")
        else:
            min_buys_24h = ai_review.get_effective_value("min_buys_24h")
            if buys_24h < min_buys_24h:
                reasons.append(f"buys_24h {buys_24h} < {min_buys_24h} min")

    # holder_count check REMOVED (2026-09-06, user's explicit request: "it
    # doesn't matter how many holders there is I want him to trade with all
    # of them"). Confirmed via AskUserQuestion this means holder count
    # specifically, NOT liquidity (min_liquidity_usd check above stays) and
    # NOT the scam-detection checks below (renounced mint/freeze) or in
    # check_token_security() (honeypot/tax) -- those are unrelated to
    # popularity and still gate every buy. config.min_holder_count is left
    # defined in config.py (unused now) rather than deleted, in case this
    # ever needs reverting.

    # renounced_mint/renounced_freeze_account come through as 0/1 ints from
    # trenches (confirmed live), not bool -- normalize before comparing.
    if ai_review.get_effective_value("require_renounced_mint"):
        renounced_mint = token.get("renounced_mint")
        if not renounced_mint:
            reasons.append("mint authority not renounced")
    if ai_review.get_effective_value("require_renounced_freeze"):
        renounced_freeze = token.get("renounced_freeze_account")
        if not renounced_freeze:
            reasons.append("freeze authority not renounced")

    return SecurityResult(passed=not reasons, reasons=reasons)


def check_token_security(security: dict) -> SecurityResult:
    """Second-pass check using bot.gmgn_client.get_token_security()'s
    response -- the honeypot/open_source/tax fields trenches doesn't
    reliably provide. Real field names verified live (2026-09-05): use
    `honeypot`/`open_source` (0/1 ints), NOT the is_honeypot/is_open_source
    fields in the same response -- those returned null on every real token
    checked, they're unused/deprecated aliases. See get_token_security()'s
    own docstring in bot/gmgn_client.py for the same finding."""
    reasons: list[str] = []

    honeypot = security.get("honeypot")
    if honeypot is None:
        reasons.append("honeypot status missing")
    elif honeypot:
        reasons.append("flagged as honeypot")

    open_source = security.get("open_source")
    if open_source is None:
        reasons.append("open_source status missing")
    elif not open_source:
        # Not automatically fatal on its own for a pump.fun-style token
        # (most launch unverified by design), but real -- surfaced as a
        # reason, not silently dropped. Not appended to `reasons` as a
        # reject on its own; left as informational only. Revisit once
        # real trade data shows whether this correlates with bad outcomes.
        pass

    for tax_field in ("buy_tax", "sell_tax"):
        tax = security.get(tax_field)
        if tax is not None:
            try:
                if float(tax) > 0:
                    reasons.append(f"{tax_field}={tax} (non-zero transfer tax)")
            except (TypeError, ValueError):
                reasons.append(f"{tax_field} unparseable: {tax!r}")

    return SecurityResult(passed=not reasons, reasons=reasons)


async def check_arbitrary_token_candidate(address: str, chain: str) -> SecurityResult:
    """Partial security check for any candidate that did NOT come from
    trenches screening -- an arbitrary, specific token looked up on demand.
    Originally built 2026-09-15 for bot/wallet_follow.py (was named
    check_wallet_follow_candidate() until bot/token_snipe.py, same day,
    needed the exact identical check for a completely different signal
    source -- renamed rather than duplicated). Reuses the exact same
    thresholds check_trenches_fields() uses for screening candidates
    (user's explicit call, after a consistency concern was raised about
    giving wallet-follow its own separate/wider threshold: "you know what
    you are right keep as is") -- there is no separate, looser standard
    for signals that come from a non-screening source vs. fresh screening.

    Genuinely CANNOT check rug_ratio/bundler_rate/rat_trader_amount_rate --
    confirmed live 2026-09-15 that neither get_token_info() nor
    get_token_security() returns them; they appear to be proprietary scores
    GMGN only computes as part of its own trenches screening product, not
    exposed for an arbitrary token looked up on demand. User's explicit,
    informed call: partial real protection (meteora_virtual_curve
    exclusion, liquidity, top-10 concentration, renounced mint/freeze,
    honeypot/tax) is still worth having rather than skipping screening for
    wallet-follow signals entirely.

    Two extra API calls per candidate (get_token_info + get_token_security)
    -- only happens when a real full-open buy signal fires (a handful of
    times a day for one followed wallet), not every poll cycle, so the
    added load is small.

    ASYNC (2026-09-15 audit fix, changed from a plain sync function): all 5
    callers (wallet_follow/dev_snipe/snipex/token_snipe/skyeye_copy) used to
    wrap this whole function in their own `asyncio.to_thread(...)`, which
    meant a GmgnRateLimitError caught INSIDE here (both API calls below
    already did, converting it to a silent rejection) never reached
    bot/rate_limiter.py's shared circuit breaker -- every one of the 5
    signal sources' security checks was invisible to the SAME shared
    backoff state screener.py/exit_manager.py already coordinate through,
    exactly the failure mode that module's own docstring warns about. Now
    does its own internal asyncio.to_thread() for each blocking gmgn-cli
    call and reports hits/resets directly, so callers just `await` this
    function directly instead of wrapping it themselves.

    PARALLELIZED (2026-09-15, later the same day, real speed fix -- user's
    explicit request: "let's try to get this guy some speed so he can stop
    doing so mush research on the tokens"): get_token_info() and
    get_token_security() are fully independent (different endpoints,
    neither's result feeds the other), so there was never a real reason to
    pay for them sequentially. Measured live before this fix: ~1.0s total
    for this whole function (~0.4s + ~0.4s + overhead) -- now ~0.4s, the
    max of the two instead of their sum. `info` is also returned on the
    SecurityResult now (see that dataclass's own `info` field docstring)
    so a caller that passes doesn't need a THIRD, redundant get_token_info()
    call just to build its buy candidate dict -- see wallet_follow.py/
    dev_snipe.py/skyeye_copy.py/token_snipe.py's own updated buy paths."""
    reasons: list[str] = []

    info_task = asyncio.to_thread(gmgn.get_token_info, address, chain=chain)
    sec_task = asyncio.to_thread(gmgn.get_token_security, address, chain=chain)
    info_result, sec_result = await asyncio.gather(info_task, sec_task, return_exceptions=True)

    if isinstance(info_result, gmgn.GmgnRateLimitError):
        await rate_limiter.handle_hit(info_result.retry_after_secs)
        return _reject([f"rate limited fetching token info: {info_result}"])
    if isinstance(info_result, gmgn.GmgnCliError):
        return _reject([f"could not fetch token info: {info_result}"])
    if isinstance(info_result, BaseException):
        raise info_result
    info = info_result
    rate_limiter.reset()

    # Same hard exclusion as check_trenches_fields()'s own copy -- see that
    # function's own comment for the full reasoning (real data: this
    # launchpad type reads near-zero liquidity regardless of real health).
    if info.get("launchpad") == "meteora_virtual_curve":
        reasons.append("launchpad meteora_virtual_curve -- liquidity reporting confirmed unreliable, excluded entirely")

    # Market cap trading band (2026-09-16) -- see config.py's own
    # min_market_cap_usd/max_market_cap_usd docstring for the full,
    # real-data-backed reasoning; same check_trenches_fields() applies,
    # kept identical here so every signal source is held to the exact same
    # boundary. get_token_info() doesn't return market_cap directly (unlike
    # trenches' own candidates) -- computed the same way every signal
    # source's own candidate dict already does, price * total_supply.
    market_cap = None  # stays None if either raw field is missing/unparseable below
    price_raw = (info.get("price") or {}).get("price")
    supply_raw = info.get("total_supply")
    if price_raw is not None and supply_raw is not None:
        try:
            market_cap = float(price_raw) * float(supply_raw)
        except (TypeError, ValueError):
            market_cap = None
        if market_cap is not None:
            min_market_cap_usd = ai_review.get_effective_value("min_market_cap_usd")
            max_market_cap_usd = ai_review.get_effective_value("max_market_cap_usd")
            if min_market_cap_usd > 0 and market_cap < min_market_cap_usd:
                reasons.append(f"market_cap ${market_cap:.0f} < ${min_market_cap_usd:.0f} min")
            if max_market_cap_usd > 0 and market_cap > max_market_cap_usd:
                reasons.append(f"market_cap ${market_cap:.0f} > ${max_market_cap_usd:.0f} max")

    # Defensive float() conversion (2026-09-15, real case hit live): an
    # unrecognized/not-yet-indexed token can come back from get_token_info()
    # with liquidity as the STRING '0' (and symbol/launchpad also blank)
    # instead of a real number or None -- a genuine "GMGN doesn't have this
    # token yet" case that a brand-new candidate could realistically hit,
    # not just a malformed lookup. Same fail-honest parse-or-reject pattern
    # as top_10_holder_rate/tax fields below, not a silent pass-through.
    min_liquidity_usd = ai_review.get_effective_min_liquidity_usd()
    liquidity = None  # stays None if missing/unparseable below
    liquidity_raw = info.get("liquidity")
    if liquidity_raw is None:
        reasons.append("liquidity missing")
    else:
        try:
            liquidity = float(liquidity_raw)
        except (TypeError, ValueError):
            reasons.append(f"liquidity unparseable: {liquidity_raw!r}")
        else:
            if liquidity < min_liquidity_usd:
                reasons.append(f"liquidity ${liquidity:.0f} < ${min_liquidity_usd:.0f} min")

    # Liquidity-to-market-cap ratio (2026-09-17) -- same check/reasoning as
    # check_trenches_fields()'s own copy, see config.py's
    # min_liquidity_to_mcap_ratio docstring. Kept identical here so every
    # signal source is held to the same boundary.
    if liquidity is not None and market_cap is not None and market_cap > 0:
        ratio = liquidity / market_cap
        min_liquidity_to_mcap_ratio = ai_review.get_effective_value("min_liquidity_to_mcap_ratio")
        if min_liquidity_to_mcap_ratio > 0 and ratio < min_liquidity_to_mcap_ratio:
            reasons.append(f"liq_to_mcap_ratio {ratio:.3f} < {min_liquidity_to_mcap_ratio:.3f} min")

    if isinstance(sec_result, gmgn.GmgnRateLimitError):
        await rate_limiter.handle_hit(sec_result.retry_after_secs)
        return _reject(reasons + [f"rate limited fetching token security: {sec_result}"])
    if isinstance(sec_result, gmgn.GmgnCliError):
        return _reject(reasons + [f"could not fetch token security: {sec_result}"])
    if isinstance(sec_result, BaseException):
        raise sec_result
    sec = sec_result
    rate_limiter.reset()

    # top_10_holder_rate enforcement REMOVED (2026-09-19) -- same reasoning
    # and same day as check_trenches_fields()'s own removal, kept
    # consistent across every signal source per this function's own
    # docstring precedent ("no separate, looser standard for signals that
    # come from a non-screening source"). get_effective_max_top_10_holder_rate()
    # left unused rather than deleted, same as check_trenches_fields()'s copy.

    # renounced_mint/renounced_freeze_account come back as real Python bool
    # here (confirmed live 2026-09-15) -- unlike trenches' 0/1 ints, but
    # `not renounced_mint` works correctly either way.
    if ai_review.get_effective_value("require_renounced_mint") and not sec.get("renounced_mint"):
        reasons.append("mint authority not renounced")
    if ai_review.get_effective_value("require_renounced_freeze") and not sec.get("renounced_freeze_account"):
        reasons.append("freeze authority not renounced")

    reasons.extend(check_token_security(sec).reasons)

    return SecurityResult(passed=not reasons, reasons=reasons, info=info)


def run_full_audit(token: dict, security: dict) -> SecurityResult:
    """Combined verdict: both passes must clear. `token` is one entry from
    bot.gmgn_client.get_trenches()'s new_creation list; `security` is the
    matching bot.gmgn_client.get_token_security() response for the same
    address."""
    trenches_result = check_trenches_fields(token)
    security_result = check_token_security(security)
    all_reasons = trenches_result.reasons + security_result.reasons
    return SecurityResult(passed=not all_reasons, reasons=all_reasons)

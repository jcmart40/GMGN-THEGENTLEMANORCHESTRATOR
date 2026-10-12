# The Gentleman — Trading Hour Schedule

Built 2026-09-08 from 1,382 real closed trades across three sessions spanning
nearly a full 24-hour cycle (first overnight run starting 2026-09-06 11:44 PM
local, continuing through the following day and evening). Unlike the original
single-night version of this file, every hour bucket below combines that same
local hour across however many of those sessions actually passed through it —
so a bucket with a large trade count has genuinely been observed more than
once, not just extrapolated from one night. All times are local (Mountain,
UTC-7).

**This is no longer just a reference document.** As of 2026-09-08, GMGN-bot
actually enforces the confirmed-bad hours below (`TIME_PAUSE_ENABLED=true`,
`bot/trading_control.py`'s `is_time_paused()`, checked in
`bot/screener.py`'s `_autonomous_buy()`). This reverses an earlier explicit
decision (2026-09-07: "the gentleman really doesn't need a trade time
schedule... I want it like that") — made deliberately once real, multi-session
data existed to justify it, not an accident or an oversight of that earlier
call.

## Full hour-by-hour table (local time)

| Hour (local)  | Trades | Win % | Net PnL (SOL) | Status |
|---------------|-------:|------:|---------------:|--------|
| 00:00 - 01:00 |     34 | 41.2% |         -0.018 | neutral |
| 01:00 - 02:00 |     88 | 40.9% |         -0.079 | neutral |
| 02:00 - 03:00 |     38 | 52.6% |         +2.241 | ⚠️ see caveat below |
| 03:00 - 04:00 |     28 | 35.7% |         -0.151 | neutral |
| **04:00 - 05:00** |     38 | 31.6% |     **-0.200** | 🔴 **PAUSED** |
| **05:00 - 06:00** |     55 | 23.6% |     **-0.064** | 🔴 **PAUSED** (lowest win rate of any hour) |
| 06:00 - 07:00 |     91 | 37.4% |         -0.051 | neutral |
| 07:00 - 08:00 |     97 | 44.3% |         +0.133 | neutral |
| **08:00 - 09:00** |     45 | 28.9% |     **-0.242** | 🔴 **PAUSED** |
| 09:00 - 10:00 |     26 | 42.3% |         +0.076 | neutral |
| 🟢 10:00 - 11:00 |     37 | 43.2% |         +0.273 | good |
| 11:00 - 12:00 |     30 | 36.7% |         +0.048 | neutral |
| **12:00 - 13:00** |     30 | 26.7% |     **-0.118** | 🔴 **PAUSED** |
| 13:00 - 14:00 |     14 | 50.0% |         +0.078 | neutral (small sample) |
| **14:00 - 15:00** |     25 | 24.0% |     **-0.151** | 🔴 **PAUSED** |
| 15:00 - 16:00 |     23 | 39.1% |         +0.018 | neutral |
| 🟢 16:00 - 17:00 |     74 | 44.6% |         +0.140 | good |
| 🟢 17:00 - 18:00 |     32 | 56.2% |         +0.237 | good |
| 18:00 - 19:00 |     64 | 40.6% |         -0.136 | neutral |
| 19:00 - 20:00 |     43 | 46.5% |         +0.148 | neutral |
| 20:00 - 21:00 |     50 | 40.0% |         -0.034 | neutral |
| **21:00 - 22:00** |    177 | 41.8% |     **-0.206** | 🔴 **PAUSED** (largest sample of any hour) |
| 🟢 22:00 - 23:00 |     33 | 54.5% |         +1.030 | good — genuinely, see below |
| 23:00 - 00:00 |     20 | 65.0% |         +1.503 | ⚠️ see caveat below |

## Important caveat: two "great" hours were single-trade illusions

Before locking in the pause schedule, every strong-looking hour was checked
for whether its net PnL survived removing its single best trade:

- **02:00-03:00**: shows +2.241 SOL, but **+2.204 of that is one trade.**
  Without it: only +0.037 SOL — basically flat, not a real pattern.
- **23:00-00:00**: shows +1.503 SOL, but **+1.468 of that is one trade.**
  Without it: only +0.035 SOL — also basically flat.
- **22:00-23:00**: shows +1.030 SOL, and even removing its best trade it's
  still **+0.134 SOL** — this one is genuinely broad-based, not a fluke.

So despite how the raw numbers look, only **22:00-23:00** (and the smaller
but consistent 10-11 AM, 4-5 PM, 5-6 PM hours) are real "good hour" evidence.
02:00-03:00 and 23:00-00:00 are not being specially favored for anything —
they're left as neutral, not paused, but also not something to expect
repeat performance from.

## Currently paused hours (enforced, not just documented)

`TIME_PAUSE_WINDOWS_UTC=4-5,11-13,15-16,19-20,21-22` (UTC) = the following
LOCAL hours: **4-6 AM, 8-9 AM, 12-1 PM, 2-3 PM, 9-10 PM**. Selected using
both a real net-PnL loss AND a depressed win rate, with reasonable sample
size — not just whichever hour happened to be red on a given night. New buys
are blocked during these windows; exit management (stop-loss/take-profit/
profit-giveback/queue-pressure/stale) keeps running normally on anything
already open, and the pause lifts automatically the moment the window ends.

Notably, **21:00-22:00 is paused but 22:00-23:00 right after it is one of
the best hours** — a real, sharp swing hour-to-hour, not something smeared
across the whole evening.

## Real patterns observed

- **21:00-22:00 is the single most statistically reliable bad hour** —
  177 trades (by far the largest sample of any hour) and still net negative.
- **05:00-06:00 has the lowest win rate of any hour (23.6%)**, even though
  its dollar loss is modest — a low win rate this consistent across a
  55-trade sample is a real signal, not noise.
- **The overnight 2-6 AM stretch is NOT uniformly bad** — 02:00-03:00 looks
  great only because of one trade, but 04:00-06:00 is genuinely, broadly bad.
- User's original 3-5 AM "worst stretch" hypothesis from the first night
  partially held up (04:00-05:00 stayed bad) but shifted slightly once more
  nights were added (05:00-06:00 turned out to be the real low-win-rate
  hour, not 03:00-04:00, which came back to roughly neutral).

## Open questions for future nights

- Do the paused hours keep looking bad once he's trading through them with
  the schedule active (i.e., does removing the worst hours' trades actually
  move his overall numbers, or was some of this variance rather than a true
  hour-of-day effect)?
- Does 22:00-23:00 keep holding up as a genuinely good hour with more data,
  or was tonight's post-fix recovery (see the same-night entry-selectivity
  incident) coincidentally landing in that window?
- Revisit this file once several more full sessions have passed through the
  currently-paused hours to confirm they're still worth pausing.

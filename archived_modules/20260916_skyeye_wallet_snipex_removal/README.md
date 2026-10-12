# Archived 2026-09-16: SkyEyeCopy, wallet_follow, SnipeX

Removed from the running bot per user + son's explicit request ("get rid of
the extra weight"). Nothing deleted -- everything needed to restore is here.

## What was changed (not archived, still in the live codebase)
- `bot/screener.py`'s `run_forever()` -- removed the `import bot.wallet_follow`,
  `import bot.snipex`, `import bot.skyeye_copy` lines and their three
  `.run_forever()` calls from the `asyncio.gather(...)` startup list.

## What was moved here
- `bot/skyeye_copy.py`, `bot/wallet_follow.py`, `bot/snipex.py`
- `logs/skyeye_copy_state.json` (+ `.lock`)
- `logs/wallet_follow_state.json` (+ `.lock`)
- `logs/snipex_state.json` (+ `.lock`), `logs/snipex_watchlist.json`

## To restore
1. Move the three `.py` files back into `bot/`.
2. Move the `logs/*` files back into `logs/`.
3. In `bot/screener.py`'s `run_forever()`, add back:
   ```python
   import bot.wallet_follow as wallet_follow
   import bot.snipex as snipex
   import bot.skyeye_copy as skyeye_copy
   ```
   and add `wallet_follow.run_forever(), snipex.run_forever(),
   skyeye_copy.run_forever()` back into the `asyncio.gather(...)` call.
4. `.env`'s `WALLET_FOLLOW_ENABLED`/`SNIPEX_ENABLED`/skyeye-related settings
   were left untouched the whole time -- check they're still set the way
   you want before restarting.

Note: `dev_snipe` and `token_snipe` were NOT touched -- both are still active
in the live bot.

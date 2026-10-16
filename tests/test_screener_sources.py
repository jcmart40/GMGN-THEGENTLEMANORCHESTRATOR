import unittest
from unittest.mock import AsyncMock, call, patch

import bot.screener as screener
from bot.security import SecurityResult
from config import config


class ScreenerSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_scans_new_deploys_completed_tokens_and_trending(self):
        new_deploy = {"address": "new-mint", "symbol": "NEW"}
        migrated = {"address": "migrated-mint", "symbol": "MIGRATED"}
        trending_duplicate = {"address": "new-mint", "symbol": "NEW"}
        trending_only = {"address": "trending-mint", "symbol": "TRENDING"}

        def get_trenches(**kwargs):
            if kwargs["types"] == ["new_creation"]:
                return {"new_creation": [new_deploy]}
            if kwargs["types"] == ["completed"]:
                return {"completed": [migrated]}
            self.fail(f"Unexpected trenches request: {kwargs!r}")

        seen = set()
        buy = AsyncMock()
        passed = SecurityResult(passed=True)
        with (
            patch.object(screener.gmgn, "get_trenches", side_effect=get_trenches) as trenches,
            patch.object(
                screener.gmgn,
                "get_trending",
                return_value=[trending_duplicate, trending_only],
            ),
            patch.object(screener.gmgn, "get_token_security", return_value={}),
            patch.object(
                screener.ai_review,
                "get_effective_value",
                side_effect=lambda name: "2m" if name == "new_creation_max_age" else 1,
            ) as effective_value,
            patch.object(screener.security, "check_trenches_fields", return_value=passed),
            patch.object(screener.security, "check_token_security", return_value=passed),
            patch.object(screener, "_append_activity"),
            patch.object(screener, "_autonomous_buy", buy),
            patch.object(screener.asyncio, "sleep", new_callable=AsyncMock),
        ):
            evaluated = await screener.screen_once(seen)

        self.assertEqual(evaluated, 3)
        self.assertEqual(
            trenches.call_args_list,
            [
                call(
                    chain=config.chain,
                    types=["new_creation"],
                    limit=80,
                    max_created="2m",
                ),
                call(chain=config.chain, types=["completed"], limit=80),
            ],
        )
        effective_value.assert_any_call("new_creation_max_age")
        self.assertEqual(
            [entry.kwargs["source"] for entry in buy.await_args_list],
            ["new_creation", "completed", "screening"],
        )
        self.assertEqual(
            [entry.args[0]["source"] for entry in buy.await_args_list],
            ["new_creation", "completed", "screening"],
        )
        self.assertEqual(seen, {"new-mint", "migrated-mint", "trending-mint"})

    async def test_open_position_cap_uses_effective_ai_override(self):
        import bot.paper_trader as paper_trader
        import bot.trading_control as trading_control

        with (
            patch.object(trading_control, "get_mode", return_value="paper"),
            patch.object(trading_control, "is_time_paused", return_value=False),
            patch.object(trading_control, "is_burst_paused", return_value=False),
            patch.object(trading_control, "is_min_interval_active", return_value=False),
            patch.object(paper_trader, "get_portfolio", return_value={"positions": {"open": {}}}),
            patch.object(screener.ai_review, "is_paused_for_review", return_value=False),
            patch.object(screener.ai_review, "get_effective_value", return_value=1) as effective_value,
            patch.object(screener, "_append_trade_log") as append_trade_log,
        ):
            await screener._autonomous_buy(
                {"address": "candidate", "symbol": "CANDIDATE"},
                SecurityResult(passed=True),
                SecurityResult(passed=True),
            )

        self.assertEqual(append_trade_log.call_args.args[0]["status"], "skipped_max_positions")
        effective_value.assert_called_once_with("max_concurrent_positions")


if __name__ == "__main__":
    unittest.main()

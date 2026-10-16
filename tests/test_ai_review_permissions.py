import json
import os
import tempfile
import unittest
from unittest.mock import patch

import bot.ai_review as ai_review
from config import config


class AIReviewPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.override_file = os.path.join(self.temp_dir.name, "overrides.json")
        self.lock_file = os.path.join(self.temp_dir.name, "overrides.lock")
        self.candidates_file = os.path.join(self.temp_dir.name, "cleared_candidates.json")
        self.config_patches = [
            patch.object(config, "ai_overrides_file", self.override_file),
            patch.object(config, "ai_overrides_lock_file", self.lock_file),
            patch.object(config, "cleared_candidates_file", self.candidates_file),
        ]
        for config_patch in self.config_patches:
            config_patch.start()
        ai_review._overrides_cache.update(mtime=None, data={})

    def tearDown(self):
        ai_review._overrides_cache.update(mtime=None, data={})
        for config_patch in reversed(self.config_patches):
            config_patch.stop()
        self.temp_dir.cleanup()

    def test_allowlist_includes_operational_limits_but_excludes_protected_controls(self):
        protected = {
            "trade_size_sol",
            "wallet_address",
            "default_trading_mode",
            "trading_control_file",
            "slippage_pct",
            "priority_fee_sol",
            "openai_api_key",
            "xai_api_key",
            "max_top_10_holder_rate",
            "max_bundler_rate",
            "max_rat_trader_rate",
        }
        self.assertTrue(protected.isdisjoint(ai_review._AI_TUNABLE_FIELDS))
        self.assertTrue({
            "max_concurrent_positions",
            "max_live_trades_per_session",
            "min_net_buy_24h_usd",
            "min_buys_24h",
            "new_creation_max_age",
            "queue_pressure_lookback_secs",
            "post_review_pause_secs",
        }.issubset(ai_review._AI_TUNABLE_FIELDS))

    def test_paper_adjustments_are_unclamped_and_take_effect(self):
        applied = ai_review._apply_review_adjustments({"stop_loss_pct": 500.0}, "paper")
        self.assertTrue(applied)
        self.assertEqual(ai_review.get_effective_value("stop_loss_pct"), 500.0)

    def test_market_cap_adjustment_reaches_security_screen(self):
        import bot.security as security

        ai_review._apply_adjustment("min_market_cap_usd", 30000.0)
        result = security.check_trenches_fields({
            "rug_ratio": 0.1,
            "liquidity": 5000.0,
            "market_cap": 25000.0,
            "renounced_mint": 1,
            "renounced_freeze_account": 1,
        })
        self.assertFalse(result.passed)
        self.assertTrue(any("market_cap" in reason for reason in result.reasons))

    def test_fixed_security_checks_remain_enforced(self):
        import bot.security as security

        result = security.check_token_security({
            "honeypot": 1,
            "open_source": 1,
            "buy_tax": 0,
            "sell_tax": 0,
        })
        self.assertFalse(result.passed)
        self.assertTrue(any("honeypot" in reason for reason in result.reasons))

        result = security.check_trenches_fields({
            "rug_ratio": 0.1,
            "liquidity": 5000.0,
            "market_cap": 30000.0,
            "renounced_mint": 1,
            "renounced_freeze_account": 1,
            "is_wash_trading": True,
            "launchpad": "meteora_virtual_curve",
        })
        self.assertFalse(result.passed)
        self.assertTrue(any("is_wash_trading" in reason for reason in result.reasons))
        self.assertTrue(any("meteora_virtual_curve" in reason for reason in result.reasons))

    def test_new_operational_threshold_adjustments_are_applied(self):
        import bot.security as security

        ai_review._apply_adjustment("min_net_buy_24h_usd", 20.0)
        ai_review._apply_adjustment("min_buys_24h", 5)
        ai_review._apply_adjustment("max_concurrent_positions", 8)
        ai_review._apply_adjustment("new_creation_max_age", "2m")

        result = security.check_trenches_fields({
            "rug_ratio": 0.1,
            "liquidity": 5000.0,
            "market_cap": 30000.0,
            "renounced_mint": 1,
            "renounced_freeze_account": 1,
            "net_buy_24h": 10.0,
            "buys_24h": 4,
        })
        self.assertTrue(any("net_buy_24h" in reason for reason in result.reasons))
        self.assertTrue(any("buys_24h" in reason for reason in result.reasons))
        self.assertEqual(ai_review.get_effective_value("max_concurrent_positions"), 8)
        self.assertEqual(ai_review.get_effective_value("new_creation_max_age"), "2m")

    def test_live_session_cap_reads_effective_value(self):
        import bot.trading_control as trading_control
        import bot.trade_history as trade_history

        ai_review._apply_adjustment("max_live_trades_per_session", 2)
        with (
            patch.object(
                trading_control, "get_state",
                return_value={"mode": "live", "updated_ts": 100.0},
            ),
            patch.object(trade_history, "count_live_trades_since", return_value=2),
        ):
            self.assertTrue(trading_control.is_live_session_paused())

    def test_queue_lookback_and_post_review_pause_read_effective_values(self):
        import bot.exit_manager as exit_manager

        ai_review._apply_adjustment("queue_pressure_lookback_secs", 20.0)
        ai_review._apply_adjustment("post_review_pause_secs", 30.0)
        with open(self.candidates_file, "w", encoding="utf-8") as stream:
            json.dump([{"status": "skipped_max_positions", "ts": 85.0}], stream)

        with patch("bot.exit_manager.time.time", return_value=100.0):
            self.assertTrue(exit_manager._has_queue_pressure())
        with patch("bot.ai_review.time.time", return_value=100.0):
            ai_review._start_review_pause()
        with patch("bot.ai_review.time.time", return_value=129.0):
            self.assertTrue(ai_review.is_paused_for_review())
        with patch("bot.ai_review.time.time", return_value=130.0):
            self.assertFalse(ai_review.is_paused_for_review())

    def test_exit_adjustment_reaches_exit_manager(self):
        import bot.exit_manager as exit_manager

        ai_review._apply_adjustment("max_hold_secs", 3.0)
        reason = exit_manager._decide_exit_reason(
            "test",
            pnl_pct=0.0,
            peak_pnl_pct=None,
            has_queue_pressure=False,
            age_secs=5.0,
            liquidity=3000.0,
            market_cap=20000.0,
            peak_liquidity=3000.0,
        )
        self.assertEqual(reason, "max-hold")

    def test_live_mode_rejects_all_ai_tuning(self):
        applied = ai_review._apply_review_adjustments({"stop_loss_pct": 500.0}, "live")
        self.assertFalse(applied)
        self.assertFalse(os.path.exists(self.override_file))

    def test_protected_suggestions_are_ignored_in_paper_mode(self):
        ai_review._apply_review_adjustments(
            {
                "trade_size_sol": 1.0,
                "wallet_address": "not-a-real-wallet",
                "default_trading_mode": "live",
                "slippage_pct": 99.0,
                "max_bundler_rate": 1.0,
            },
            "paper",
        )
        self.assertFalse(os.path.exists(self.override_file))

    def test_values_are_type_checked_without_clamping(self):
        with self.assertRaises(ValueError):
            ai_review._apply_adjustment("trade_size_sol", 1.0)
        with self.assertRaises(ValueError):
            ai_review._apply_adjustment("new_creation_max_age", 30)
        with self.assertRaises(ValueError):
            ai_review._apply_adjustment("stop_loss_pct", float("nan"))
        with self.assertRaises(ValueError):
            ai_review._apply_adjustment("require_renounced_mint", 0)
        with self.assertRaises(ValueError):
            ai_review._apply_adjustment("time_pause_windows_utc", [[0, 24]])

    def test_prompt_lists_only_allowlisted_adjustments(self):
        empty_stats = {
            "count": 0,
            "win_rate": None,
            "avg_pnl_pct": None,
            "total_pnl_sol": 0.0,
            "reason_breakdown": {},
            "fee_drag_by_reason": {},
            "skipped_no_pnl": 0,
        }
        settings = {
            field: getattr(config, field)
            for field in ai_review._AI_TUNABLE_FIELDS
        }
        prompt = ai_review._build_prompt(empty_stats, empty_stats, settings, [], [])
        self.assertIn('"stop_loss_pct": <number>', prompt)
        self.assertIn('"require_renounced_mint": <boolean>', prompt)
        self.assertIn('"time_pause_windows_utc": <array of [start_hour, end_hour] pairs>', prompt)
        self.assertIn('"max_concurrent_positions": <integer>', prompt)
        self.assertIn('"new_creation_max_age": <string>', prompt)
        self.assertNotIn('"trade_size_sol":', prompt)
        self.assertNotIn('"slippage_pct":', prompt)
        self.assertNotIn('"wallet_address":', prompt)
        self.assertNotIn('"default_trading_mode":', prompt)
        self.assertIn("there are no open questions", prompt)

    def test_only_strategy_values_are_written_to_override_file(self):
        time_pause_enabled = not config.time_pause_enabled
        windows = [[0, 1], [22, 23.5]]
        ai_review._apply_review_adjustments(
            {
                "min_market_cap_usd": 0.0,
                "time_pause_enabled": time_pause_enabled,
                "time_pause_windows_utc": windows,
            },
            "paper",
        )
        with open(self.override_file, encoding="utf-8") as stream:
            overrides = json.load(stream)
        self.assertEqual(overrides["min_market_cap_usd"], 0.0)
        self.assertIs(overrides["time_pause_enabled"], time_pause_enabled)
        self.assertEqual(ai_review.get_effective_value("time_pause_windows_utc"),
                         [(0.0, 1.0), (22.0, 23.5)])
        self.assertTrue({
            "trade_size_sol",
            "wallet_address",
            "default_trading_mode",
            "slippage_pct",
            "max_bundler_rate",
        }.isdisjoint(overrides))


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import bot.ai_review as ai_review
from config import config


class NotableOutcomeFeedTests(unittest.TestCase):
    """Tests for the event-driven trade-outcome feed (2026-10-16): a closed
    trade whose pnl_pct magnitude exceeded the threshold flag is stored in
    memory and triggers an early review."""

    def test_memory_schema_always_has_pending_field(self):
        with patch.object(config, "ai_memory_file", "logs/_test_mem.json"):
            memory = ai_review.load_memory()
            self.assertIn("_pending_notable_outcome", memory)
            self.assertIsNone(memory["_pending_notable_outcome"])

    def test_small_outcome_is_not_notable(self):
        # 3% move < 15% threshold -> no flag written.
        entry = {"pnl_pct": 0.03, "symbol": "SMALL", "mode": "paper"}
        with patch.object(config, "ai_review_notable_pnl_pct", 15.0), \
             patch.object(config, "ai_memory_file", "logs/_test_mem.json"), \
             patch.object(config, "ai_memory_lock_file", "logs/_test_mem.lock"), \
             patch.object(ai_review, "load_memory", return_value={}), \
             patch.object(ai_review, "save_memory") as save:
            ai_review.notify_trade_outcome(entry)
            save.assert_not_called()

    def test_large_loss_is_notable(self):
        # -30% < -15% -> flagged and persisted.
        entry = {"pnl_pct": -0.30, "symbol": "BIGLOSS", "mode": "live",
                 "reason": "stop_loss", "exit_ts": 1.0, "pnl_sol": -3.2}
        with patch.object(config, "ai_review_notable_pnl_pct", 15.0), \
             patch.object(config, "ai_memory_file", "logs/_test_mem.json"), \
             patch.object(config, "ai_memory_lock_file", "logs/_test_mem.lock"), \
             patch.object(ai_review, "load_memory", return_value={}), \
             patch.object(ai_review, "save_memory") as save:
            ai_review.notify_trade_outcome(entry)
            memory = save.call_args.args[0]
        self.assertEqual(memory["_pending_notable_outcome"]["symbol"], "BIGLOSS")
        self.assertEqual(memory["_pending_notable_outcome"]["mode"], "live")
        self.assertEqual(memory["_pending_notable_outcome"]["pnl_sol"], -3.2)

    def test_disabled_threshold_noop(self):
        entry = {"pnl_pct": -0.50, "symbol": "HUGELOSS", "mode": "paper"}
        with patch.object(config, "ai_review_notable_pnl_pct", 0), \
             patch.object(config, "ai_memory_file", "logs/_test_mem.json"), \
             patch.object(config, "ai_memory_lock_file", "logs/_test_mem.lock"), \
             patch.object(ai_review, "save_memory") as save:
            ai_review.notify_trade_outcome(entry)
            save.assert_not_called()

    def test_prompt_includes_notable_section(self):
        current_settings = {
            name: getattr(config, name) for name in ai_review._AI_TUNABLE_FIELDS
        }
        p = ai_review._build_prompt(
            stats={"count": 1, "win_rate": 0.0, "avg_pnl_pct": -20.0,
                   "total_pnl_sol": -3.2, "reason_breakdown": {"stop_loss": 1},
                   "fee_drag_by_reason": {}, "skipped_no_pnl": 0},
            manual_stats={"count": 0, "win_rate": None, "avg_pnl_pct": None,
                          "total_pnl_sol": 0.0},
            current_settings=current_settings,
            recent_lessons=[], unanswered_notes=[],
            notable_outcome={"pnl_pct": -0.20, "symbol": "BIGLOSS",
                             "mode": "live", "reason": "stop_loss", "pnl_sol": -3.2},
        )
        self.assertIn("NOTABLE OUTCOME JUST HAPPENED", p)
        self.assertIn("BIGLOSS", p)
        self.assertIn("-20.0%", p)

    def tearDown(self):
        import os
        for f in ("logs/_test_mem.json", "logs/_test_mem.lock"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

from __future__ import annotations

import unittest

from three_table_quant.dashboard import build_dashboard, validate_dashboard


def make_signal(
    decision_date: str,
    buy_date: str,
    exit_date: str,
    candidate_count: int = 1,
) -> dict:
    return {
        "signal_id": f"signal-{decision_date}",
        "decision_date": decision_date,
        "buy_date": buy_date,
        "exit_date": exit_date,
        "generated_at": f"{decision_date[:4]}-{decision_date[4:6]}-{decision_date[6:]}T20:00:00+08:00",
        "source_snapshots": [],
        "candidates": [
            {
                "ts_code": f"60000{rank}.SH",
                "name": f"票{rank}",
                "rank": rank,
                "metrics": {"utility_score": 0.01, "policy_trade_eligible": True},
                "features": {},
                "source_ranks": {},
                "action": "SHADOW",
                "action_reason": "test",
            }
            for rank in range(1, candidate_count + 1)
        ],
        "model_version": "test",
        "status": "RANKED" if candidate_count else "NO_CANDIDATE",
    }


def make_trade(signal: dict, status: str = "PENDING_BUY") -> dict:
    trade = {
        "trade_id": f"{signal['decision_date']}:R1",
        "signal_id": signal["signal_id"],
        "decision_date": signal["decision_date"],
        "buy_date": signal["buy_date"],
        "planned_exit_date": signal["exit_date"],
        "rank": 1,
        "ts_code": "600001.SH",
        "name": "票1",
        "status": status,
        "reason": "test",
        "buy": None,
        "exit": None,
        "pnl": None,
        "diagnostics": {},
    }
    if status == "BUY_UNFILLED":
        trade["buy"] = {"submitted_qty": 1000, "filled_qty": 0, "avg_price": None, "fees": 0.0}
    return trade


class DashboardContractTests(unittest.TestCase):
    def test_no_final_observation_is_null_and_pending(self) -> None:
        item = make_signal("20260803", "20260804", "20260805")
        payload = build_dashboard(
            {"signals": [item], "trades": [make_trade(item)]},
            [],
            "2026-08-05T12:00:00+08:00",
            {"status": "RANKED"},
            [1, 2, 3],
        )
        validate_dashboard(payload)
        rank_one = payload["rank_metrics"]["1"]
        self.assertIsNone(rank_one["cumulative_return"])
        self.assertEqual(rank_one["final_days"], 0)
        self.assertEqual(rank_one["pending_days"], 1)
        self.assertFalse(rank_one["is_provisional"])
        self.assertIsNone(payload["rank_daily"][0]["ranks"]["1"]["daily_return"])
        self.assertEqual(payload["rank_daily"][0]["ranks"]["1"]["return_date"], "2026-08-05")

    def test_final_zero_remains_distinct_from_pending_null(self) -> None:
        item = make_signal("20260803", "20260804", "20260805", candidate_count=0)
        payload = build_dashboard(
            {"signals": [item], "trades": []},
            [],
            "2026-08-05T12:00:00+08:00",
            {"status": "NO_CANDIDATE"},
            [1, 2, 3],
        )
        validate_dashboard(payload)
        self.assertEqual(payload["rank_metrics"]["1"]["cumulative_return"], 0.0)
        self.assertEqual(payload["rank_metrics"]["1"]["final_days"], 1)
        self.assertEqual(payload["rank_metrics"]["1"]["pending_days"], 0)

    def test_mixed_results_are_provisional_and_win_rate_uses_closed_only(self) -> None:
        closed_signal = make_signal("20260827", "20260828", "20260831")
        pending_signal = make_signal("20260828", "20260831", "20260901")
        cash_signal = make_signal("20260831", "20260901", "20260902")
        closed = make_trade(closed_signal, "CLOSED")
        closed["buy"] = {"filled_qty": 1000, "amount": 10000.0, "fees": 5.0}
        closed["exit"] = {
            "remaining_qty": 0,
            "actual_exit_date": "20260901",
            "actual_exit_at": "2026-09-01T11:04:00+08:00",
        }
        closed["pnl"] = {"net_return_on_allocated": 0.10}
        unfilled = make_trade(cash_signal, "BUY_UNFILLED")
        state = {
            "signals": [closed_signal, pending_signal, cash_signal],
            "trades": [closed, make_trade(pending_signal), unfilled],
        }
        payload = build_dashboard(
            state,
            [],
            "2026-09-02T12:00:00+08:00",
            {"status": "RANKED"},
            [1, 2, 3],
        )
        validate_dashboard(payload)
        metric = payload["rank_metrics"]["1"]
        self.assertAlmostEqual(metric["cumulative_return"], 0.10)
        self.assertEqual(metric["final_days"], 2)
        self.assertEqual(metric["pending_days"], 1)
        self.assertTrue(metric["is_provisional"])
        self.assertEqual(metric["closed_trades"], 1)
        self.assertEqual(metric["win_rate"], 1.0)
        closed_row = next(row for row in payload["rank_daily"] if row["decision_date"] == "2026-08-27")
        self.assertEqual(closed_row["date"], "2026-08-31")
        self.assertEqual(closed_row["ranks"]["1"]["return_date"], "2026-09-01")

        payload["rank_metrics"]["1"]["win_rate"] = 0.5
        with self.assertRaisesRegex(ValueError, "CLOSED observations only"):
            validate_dashboard(payload)

    def test_same_actual_exit_date_keeps_each_signal_as_an_independent_row(self) -> None:
        first_signal = make_signal("20260827", "20260828", "20260831")
        second_signal = make_signal("20260828", "20260831", "20260901")
        trades = []
        for item, value in ((first_signal, 0.10), (second_signal, -0.05)):
            trade = make_trade(item, "CLOSED")
            trade["buy"] = {"filled_qty": 1000, "amount": 10000.0, "fees": 5.0}
            trade["exit"] = {
                "remaining_qty": 0,
                "actual_exit_at": "20260902T11:04:00+08:00",
            }
            trade["pnl"] = {"net_return_on_allocated": value}
            trades.append(trade)
        payload = build_dashboard(
            {"signals": [first_signal, second_signal], "trades": trades},
            [],
            "2026-09-02T12:00:00+08:00",
            {"status": "RANKED"},
            [1, 2, 3],
        )
        validate_dashboard(payload)
        rank_one_rows = [row["ranks"]["1"] for row in payload["rank_daily"]]
        self.assertEqual(len(rank_one_rows), 2)
        self.assertEqual([row["return_date"] for row in rank_one_rows], ["2026-09-02", "2026-09-02"])
        self.assertEqual(payload["rank_metrics"]["1"]["closed_trades"], 2)
        self.assertAlmostEqual(payload["rank_metrics"]["1"]["cumulative_return"], 0.045)

    def test_validator_rejects_fixed_rank_identity_mismatch(self) -> None:
        item = make_signal("20260803", "20260804", "20260805", candidate_count=2)
        payload = build_dashboard(
            {"signals": [item], "trades": [make_trade(item)]},
            [],
            "2026-08-05T12:00:00+08:00",
            {"status": "RANKED"},
            [1, 2, 3],
        )
        payload["days"][0]["rank_slots"]["1"]["candidate_id"] = payload["days"][0]["candidates"][1]["candidate_id"]
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_dashboard(payload)

    def test_validator_rejects_nonfinal_numeric_return(self) -> None:
        item = make_signal("20260803", "20260804", "20260805")
        payload = build_dashboard(
            {"signals": [item], "trades": [make_trade(item)]},
            [],
            "2026-08-05T12:00:00+08:00",
            {"status": "RANKED"},
            [1, 2, 3],
        )
        payload["rank_daily"][0]["ranks"]["1"]["daily_return"] = 0.0
        with self.assertRaisesRegex(ValueError, "non-final"):
            validate_dashboard(payload)


if __name__ == "__main__":
    unittest.main()

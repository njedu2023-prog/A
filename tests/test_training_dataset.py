from __future__ import annotations

import unittest

from three_table_quant.domain import ContractError
from three_table_quant.training_dataset import build_training_dataset


def candidate(rank: int, code: str) -> dict:
    return {
        "rank": rank,
        "ts_code": code,
        "name": code,
        "source_ranks": {
            "a_top10": rank,
            "premium_top10": rank,
            "decision_table": rank,
        },
        "features": {
            "feature_schema_version": "frozen_features_v2",
            "ret_5d": 0.0,
            "cvar_loss_10pct": 0.0,
        },
    }


def base_state(candidates: list[dict], trades: list[dict]) -> dict:
    return {
        "schema_version": "three_table_state_v1",
        "signals": [
            {
                "signal_id": "20260803-frozen",
                "decision_date": "20260803",
                "buy_date": "20260804",
                "exit_date": "20260805",
                "feature_asof": "20260803",
                "feature_version": "signal_features_v2",
                "model_version": "transparent_shadow_baseline_v1",
                "source_snapshots": [
                    {
                        "source_id": "a_top10",
                        "repository_commit_sha": "a" * 40,
                        "content_sha256": "1" * 64,
                        "generated_at": "2026-08-03T13:00:00Z",
                        "decision_date": "20260803",
                    },
                    {
                        "source_id": "premium_top10",
                        "repository_commit_sha": "b" * 40,
                        "content_sha256": "2" * 64,
                        "generated_at": "2026-08-03T13:01:00Z",
                        "decision_date": "20260803",
                    },
                    {
                        "source_id": "decision_table",
                        "repository_commit_sha": "b" * 40,
                        "content_sha256": "3" * 64,
                        "generated_at": "2026-08-03T13:02:00Z",
                        "decision_date": "20260803",
                    },
                ],
                "candidates": candidates,
            }
        ],
        "trades": trades,
    }


def trade(rank: int, code: str, status: str) -> dict:
    return {
        "trade_id": f"20260803:R{rank}",
        "signal_id": "20260803-frozen",
        "decision_date": "20260803",
        "buy_date": "20260804",
        "planned_exit_date": "20260805",
        "rank": rank,
        "ts_code": code,
        "status": status,
        "buy": None,
        "exit": None,
        "pnl": None,
        "t_day_validation": {
            "status": "PENDING",
            "trade_date": "20260804",
            "is_promoted": None,
        },
    }


class TrainingDatasetTests(unittest.TestCase):
    def test_unknown_labels_remain_null_and_frozen_provenance_is_retained(self) -> None:
        item = candidate(1, "000001.SZ")
        state = base_state([item], [trade(1, "000001.SZ", "PENDING_BUY")])
        payload = build_training_dataset(state)

        self.assertEqual(payload["row_count"], 1)
        self.assertEqual(payload["mature_count"], 0)
        self.assertEqual(payload["cohort_count"], 1)
        self.assertEqual(len(payload["feature_contract_sha256"]), 64)
        self.assertEqual(len(payload["dataset_sha256"]), 64)
        row = payload["rows"][0]
        self.assertEqual(row["feature_asof"], "20260803")
        self.assertEqual(row["feature_version"], "signal_features_v2")
        self.assertEqual(row["model_version"], "transparent_shadow_baseline_v1")
        self.assertEqual(
            row["source_provenance"]["a_top10"]["repository_commit_sha"],
            "a" * 40,
        )
        self.assertEqual(row["features"]["ret_5d"], 0.0)
        self.assertEqual(row["features"]["cvar_loss_10pct"], 0.0)
        self.assertEqual(
            row["labels"],
            {
                "fill": None,
                "conditional_net_return": None,
                "policy_net_return": None,
                "promotion": None,
                "exit_delayed": None,
                "exit_delay_days": None,
                "target_end_dates": {
                    "fill": None,
                    "promotion": None,
                    "conditional_return": None,
                    "exit_delay": None,
                },
                "label_end_date": None,
                "is_mature": False,
            },
        )

    def test_dataset_fingerprint_is_deterministic_and_content_addressed(self) -> None:
        item = candidate(1, "000001.SZ")
        state = base_state([item], [trade(1, "000001.SZ", "PENDING_BUY")])
        first = build_training_dataset(state)
        second = build_training_dataset(state)
        self.assertEqual(first["dataset_sha256"], second["dataset_sha256"])

        changed = base_state(
            [candidate(1, "000001.SZ")],
            [trade(1, "000001.SZ", "PENDING_BUY")],
        )
        changed["signals"][0]["candidates"][0]["features"]["ret_5d"] = 0.01
        third = build_training_dataset(changed)
        self.assertNotEqual(first["dataset_sha256"], third["dataset_sha256"])

    def test_unfilled_is_fill_zero_but_never_a_conditional_return(self) -> None:
        item = candidate(1, "000001.SZ")
        record = trade(1, "000001.SZ", "BUY_UNFILLED")
        record["buy"] = {
            "filled_qty": 0,
            "label_quality": "ACTUAL",
        }
        record["t_day_validation"] = {
            "status": "VERIFIED",
            "trade_date": "20260804",
            "is_promoted": True,
        }
        row = build_training_dataset(base_state([item], [record]))["rows"][0]

        self.assertEqual(row["labels"]["fill"], 0)
        self.assertIsNone(row["labels"]["conditional_net_return"])
        self.assertEqual(row["labels"]["policy_net_return"], 0.0)
        self.assertEqual(row["labels"]["promotion"], 1)
        self.assertEqual(row["labels"]["label_end_date"], "20260804")
        self.assertTrue(row["labels"]["is_mature"])
        self.assertEqual(row["label_quality"]["fill"], "ACTUAL")

    def test_closed_uses_actual_delayed_exit_and_keeps_real_zero_return(self) -> None:
        item = candidate(1, "000001.SZ")
        record = trade(1, "000001.SZ", "CLOSED")
        record["buy"] = {
            "filled_qty": 1000,
            "label_quality": "ACTUAL",
        }
        record["exit"] = {
            "actual_exit_date": "20260807",
            "actual_exit_at": "2026-08-07T11:04:00+08:00",
            "delay_trading_days": 2,
            "label_quality": "CONSERVATIVE",
        }
        record["pnl"] = {"net_return_on_allocated": 0.0}
        row = build_training_dataset(base_state([item], [record]))["rows"][0]

        self.assertEqual(row["labels"]["fill"], 1)
        self.assertEqual(row["labels"]["conditional_net_return"], 0.0)
        self.assertEqual(row["labels"]["policy_net_return"], 0.0)
        self.assertEqual(row["labels"]["exit_delayed"], 1)
        self.assertEqual(row["labels"]["exit_delay_days"], 2)
        self.assertEqual(row["labels"]["label_end_date"], "20260807")
        self.assertEqual(
            row["labels"]["target_end_dates"]["conditional_return"],
            "20260807",
        )
        self.assertEqual(
            row["label_quality"]["conditional_return"],
            "CONSERVATIVE",
        )

    def test_promotion_truth_is_independent_of_execution_maturity(self) -> None:
        item = candidate(1, "000001.SZ")
        record = trade(1, "000001.SZ", "BUY_UNVERIFIABLE")
        record["t_day_validation"] = {
            "status": "VERIFIED",
            "trade_date": "20260804",
            "is_promoted": False,
        }
        row = build_training_dataset(base_state([item], [record]))["rows"][0]

        self.assertIsNone(row["labels"]["fill"])
        self.assertEqual(row["labels"]["promotion"], 0)
        self.assertEqual(
            row["labels"]["target_end_dates"]["promotion"],
            "20260804",
        )
        self.assertFalse(row["labels"]["is_mature"])

    def test_duplicate_or_mismatched_shadow_identity_fails_closed(self) -> None:
        first = candidate(1, "000001.SZ")
        second = candidate(1, "000002.SZ")
        with self.assertRaisesRegex(ContractError, "duplicate training row|mismatch"):
            build_training_dataset(
                base_state(
                    [first, second],
                    [trade(1, "000001.SZ", "PENDING_BUY")],
                )
            )

    def test_future_feature_asof_fails_closed(self) -> None:
        item = candidate(1, "000001.SZ")
        item["feature_asof"] = "20260804"
        with self.assertRaisesRegex(ContractError, "cannot be later"):
            build_training_dataset(
                base_state([item], [trade(1, "000001.SZ", "PENDING_BUY")])
            )


if __name__ == "__main__":
    unittest.main()

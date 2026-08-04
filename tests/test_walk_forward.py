from __future__ import annotations

import random
import unittest

from three_table_quant.domain import ContractError
from three_table_quant.walk_forward import (
    expanding_walk_forward,
    partition_lockbox,
)


def row(
    row_id: str,
    decision_date: str,
    label_end_date: str | None,
    *,
    rank: int = 1,
    mature: bool = True,
) -> dict:
    return {
        "row_id": row_id,
        "decision_date": decision_date,
        "rank": rank,
        "ts_code": f"{int(row_id.split('-')[-1]):06d}.SZ",
        "labels": {
            "is_mature": mature,
            "label_end_date": label_end_date,
        },
    }


class WalkForwardTests(unittest.TestCase):
    def test_entire_d_day_group_stays_in_one_validation_fold(self) -> None:
        rows = [
            row("r-1", "20260803", "20260803", rank=1),
            row("r-2", "20260803", "20260803", rank=2),
            row("r-3", "20260804", "20260805", rank=1),
            row("r-4", "20260804", "20260805", rank=2),
            row("r-5", "20260805", "20260806", rank=1),
        ]
        folds = expanding_walk_forward(rows, min_train_days=1)
        self.assertEqual(len(folds), 2)
        self.assertEqual(
            {item["row_id"] for item in folds[0].validation_rows},
            {"r-3", "r-4"},
        )
        self.assertEqual(folds[0].validation_dates, ("20260804",))
        self.assertTrue(
            all(
                item["decision_date"] != folds[0].validation_start
                for item in folds[0].train_rows
            )
        )

    def test_actual_label_end_purges_delayed_exit_overlap(self) -> None:
        rows = [
            row("r-1", "20260803", "20260806"),
            row("r-2", "20260804", "20260805"),
            row("r-3", "20260805", "20260806"),
            row("r-4", "20260806", "20260807"),
        ]
        folds = expanding_walk_forward(rows, min_train_days=1)
        validation_on_sixth = next(
            item for item in folds if item.validation_start == "20260806"
        )
        train_ids = {item["row_id"] for item in validation_on_sixth.train_rows}
        self.assertNotIn("r-1", train_ids)
        self.assertIn("r-2", train_ids)
        self.assertNotIn("r-3", train_ids)
        self.assertTrue(
            all(
                item["labels"]["label_end_date"]
                < validation_on_sixth.validation_start
                for item in validation_on_sixth.train_rows
            )
        )

    def test_embargo_counts_exchange_days_across_weekend(self) -> None:
        rows = [
            row("r-1", "20260805", "20260806"),
            row("r-2", "20260806", "20260807"),
            row("r-3", "20260807", "20260810"),
            row("r-4", "20260810", "20260811"),
        ]
        trading_days = ["20260805", "20260806", "20260807", "20260810", "20260811"]
        folds = expanding_walk_forward(
            rows,
            min_train_days=1,
            embargo_days=1,
            trading_days=trading_days,
        )
        monday = next(item for item in folds if item.validation_start == "20260810")
        self.assertEqual(monday.embargo_cutoff, "20260807")
        self.assertEqual(
            {item["row_id"] for item in monday.train_rows},
            {"r-1"},
        )

    def test_last_dates_are_separated_as_lockbox_and_never_enter_folds(self) -> None:
        rows = [
            row(f"r-{index}", f"202608{index:02d}", f"202608{index + 1:02d}")
            for index in range(1, 7)
        ]
        partition = partition_lockbox(rows, lockbox_days=2)
        self.assertEqual(partition.lockbox_dates, ("20260805", "20260806"))
        self.assertEqual(
            {item["decision_date"] for item in partition.lockbox_rows},
            {"20260805", "20260806"},
        )
        folds = expanding_walk_forward(
            rows,
            min_train_days=1,
            lockbox_days=2,
        )
        used = {
            item["decision_date"]
            for fold in folds
            for item in (*fold.train_rows, *fold.validation_rows)
        }
        self.assertFalse(used & {"20260805", "20260806"})

    def test_split_is_deterministic_and_immature_rows_are_excluded(self) -> None:
        rows = [
            row("r-1", "20260803", "20260804"),
            row("r-2", "20260804", None, mature=False),
            row("r-3", "20260805", "20260806"),
            row("r-4", "20260806", "20260807"),
        ]
        shuffled = rows[:]
        random.Random(7).shuffle(shuffled)
        left = expanding_walk_forward(rows, min_train_days=1)
        right = expanding_walk_forward(shuffled, min_train_days=1)
        self.assertEqual(left, right)
        observed = {
            item["row_id"]
            for fold in left
            for item in (*fold.train_rows, *fold.validation_rows)
        }
        self.assertNotIn("r-2", observed)

    def test_positive_embargo_requires_explicit_trading_calendar(self) -> None:
        rows = [
            row("r-1", "20260803", "20260804"),
            row("r-2", "20260804", "20260805"),
        ]
        with self.assertRaisesRegex(ContractError, "absent from trading_days"):
            expanding_walk_forward(
                rows,
                min_train_days=1,
                embargo_days=1,
            )


if __name__ == "__main__":
    unittest.main()

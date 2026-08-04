from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from three_table_quant.domain import ContractError
from three_table_quant.model_registry import (
    BASELINE_MODEL_ID,
    artifact_sha256,
    create_registry,
    evaluate_promotion_readiness,
    resolve_champion,
    validate_registry,
)


def training_row(
    index: int,
    *,
    rank: int,
    mature: bool = True,
    decision_date: str | None = None,
) -> dict:
    return {
        "row_id": f"row-{index}",
        "decision_date": decision_date or f"2026{(index // 28) + 1:02d}{(index % 28) + 1:02d}",
        "rank": rank,
        "labels": {
            "is_mature": mature,
            "label_end_date": "20261231" if mature else None,
        },
    }


class ModelRegistryTests(unittest.TestCase):
    def test_current_like_pending_data_is_insufficient_and_keeps_baseline(self) -> None:
        rows = [
            training_row(index, rank=index + 1, mature=False, decision_date="20260804")
            for index in range(3)
        ]
        registry = create_registry(rows)
        validate_registry(registry)
        self.assertEqual(
            registry["promotion_readiness"]["status"],
            "INSUFFICIENT_DATA",
        )
        self.assertEqual(registry["promotion_readiness"]["mature_candidates"], 0)
        self.assertEqual(registry["challenger"]["status"], "INSUFFICIENT_DATA")
        self.assertEqual(registry["champion"]["model_id"], BASELINE_MODEL_ID)
        self.assertEqual(
            resolve_champion(registry)["model"]["model_id"],
            BASELINE_MODEL_ID,
        )

    def test_all_three_sample_and_lockbox_thresholds_are_required(self) -> None:
        too_few = [
            training_row(index, rank=index % 3 + 1, decision_date=f"202601{index % 28 + 1:02d}")
            for index in range(179)
        ]
        result = evaluate_promotion_readiness(too_few)
        self.assertEqual(result["status"], "INSUFFICIENT_DATA")
        self.assertIn("mature_candidates_below_180", result["reasons"])
        self.assertIn("lockbox_decision_days_below_126", result["reasons"])

        enough_candidates_not_rank_three = [
            training_row(
                index,
                rank=1 if index < 120 else 2,
                decision_date=f"2026{index // 28 + 1:02d}{index % 28 + 1:02d}",
            )
            for index in range(180)
        ]
        result = evaluate_promotion_readiness(enough_candidates_not_rank_three)
        self.assertIn("rank_3_mature_candidates_below_60", result["reasons"])

    def test_eligible_requires_180_candidates_60_each_rank_and_126_days(self) -> None:
        rows: list[dict] = []
        index = 0
        for day in range(126):
            year = 2025 + day // 120
            day_in_year = day % 120
            month = day_in_year // 20 + 1
            dom = day_in_year % 20 + 1
            decision_date = f"{year}{month:02d}{dom:02d}"
            for rank in (1, 2, 3):
                rows.append(
                    training_row(
                        index,
                        rank=rank,
                        decision_date=decision_date,
                    )
                )
                index += 1
        result = evaluate_promotion_readiness(rows)
        self.assertEqual(result["status"], "ELIGIBLE_FOR_VALIDATION")
        self.assertGreaterEqual(result["mature_candidates"], 180)
        self.assertEqual(result["mature_decision_days"], 126)
        self.assertTrue(
            all(value >= 60 for value in result["fixed_rank_mature_counts"].values())
        )

    def test_valid_trained_champion_is_resolved_by_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "model.json"
            artifact.write_bytes(b'{"model":"challenger"}\n')
            registry = create_registry()
            registry["champion"] = {
                "model_id": "formal_quant_v2",
                "kind": "TRAINED",
                "status": "ACTIVE",
                "artifact_path": "model.json",
                "artifact_sha256": artifact_sha256(artifact),
            }
            validate_registry(registry)
            resolved = resolve_champion(registry, base_dir=directory)
            self.assertFalse(resolved["fallback"])
            self.assertEqual(resolved["model"]["model_id"], "formal_quant_v2")

    def test_corrupt_or_missing_champion_artifact_falls_back_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "model.json"
            artifact.write_bytes(b"original")
            registry = create_registry()
            registry["champion"] = {
                "model_id": "formal_quant_v2",
                "kind": "TRAINED",
                "status": "ACTIVE",
                "artifact_path": "model.json",
                "artifact_sha256": artifact_sha256(artifact),
            }
            artifact.write_bytes(b"corrupt")
            resolved = resolve_champion(registry, base_dir=directory)
            self.assertTrue(resolved["fallback"])
            self.assertEqual(
                resolved["fallback_reason"],
                "CHAMPION_ARTIFACT_CHECKSUM_MISMATCH",
            )
            self.assertEqual(resolved["model"]["model_id"], BASELINE_MODEL_ID)

            artifact.unlink()
            resolved = resolve_champion(registry, base_dir=directory)
            self.assertTrue(resolved["fallback"])
            self.assertEqual(
                resolved["fallback_reason"],
                "CHAMPION_ARTIFACT_UNAVAILABLE",
            )

    def test_registry_rejects_unsafe_paths_and_invalid_checksums(self) -> None:
        registry = create_registry()
        registry["champion"] = {
            "model_id": "formal_quant_v2",
            "kind": "TRAINED",
            "status": "ACTIVE",
            "artifact_path": "../source/model.json",
            "artifact_sha256": "not-a-checksum",
        }
        with self.assertRaises(ContractError):
            validate_registry(registry)


if __name__ == "__main__":
    unittest.main()


from __future__ import annotations

import json
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
from three_table_quant.promotion import (
    PROMOTION_REPORT_SCHEMA,
    REQUIRED_PROMOTION_CHECKS,
    artifact_fingerprint,
    attach_promotion_certificate,
    transition_promotion_state,
)


def certified_model(model_id: str = "formal_quant_v2") -> dict:
    artifact = {
        "schema": "model_artifact_v1",
        "model_id": model_id,
        "weights": [0.1, -0.2],
    }
    report = {
        "schema": PROMOTION_REPORT_SCHEMA,
        "status": "APPROVED",
        "promotion_state": "APPROVED",
        "model_id": model_id,
        "artifact_fingerprint": artifact_fingerprint(artifact),
        "evaluation_dataset_fingerprint": "a" * 64,
        "checks": {name: True for name in REQUIRED_PROMOTION_CHECKS},
    }
    return attach_promotion_certificate(artifact, report)


def trained_champion(artifact: dict, path: Path) -> dict:
    return {
        "model_id": artifact["model_id"],
        "kind": "TRAINED",
        "status": "ACTIVE",
        "artifact_path": path.name,
        "artifact_sha256": artifact_sha256(path),
        "artifact_fingerprint": artifact["artifact_fingerprint"],
        "promotion_certificate": artifact["promotion_certificate"],
    }


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
            payload = certified_model()
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            registry = create_registry()
            registry["champion"] = trained_champion(payload, artifact)
            validate_registry(registry)
            resolved = resolve_champion(registry, base_dir=directory)
            self.assertFalse(resolved["fallback"])
            self.assertEqual(resolved["model"]["model_id"], "formal_quant_v2")

    def test_corrupt_or_missing_champion_artifact_falls_back_to_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "model.json"
            payload = certified_model()
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            registry = create_registry()
            registry["champion"] = trained_champion(payload, artifact)
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

    def test_boolean_validation_cannot_activate_a_trained_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "model.json"
            payload = {
                "schema": "model_artifact_v1",
                "model_id": "legacy_boolean_model",
                "validation_passed": True,
            }
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            registry = create_registry()
            registry["champion"] = {
                "model_id": payload["model_id"],
                "kind": "TRAINED",
                "status": "ACTIVE",
                "artifact_path": artifact.name,
                "artifact_sha256": artifact_sha256(artifact),
            }
            with self.assertRaisesRegex(ContractError, "promotion certificate"):
                validate_registry(registry)
            resolved = resolve_champion(registry, base_dir=directory)
            self.assertTrue(resolved["fallback"])
            self.assertEqual(
                resolved["fallback_reason"],
                "CHAMPION_PROMOTION_CERTIFICATE_INVALID",
            )

    def test_certificate_and_artifact_fingerprints_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "model.json"
            payload = certified_model()
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            registry = create_registry()
            registry["champion"] = trained_champion(payload, artifact)

            payload["weights"][0] = 9.9
            artifact.write_text(json.dumps(payload), encoding="utf-8")
            registry["champion"]["artifact_sha256"] = artifact_sha256(artifact)
            resolved = resolve_champion(registry, base_dir=directory)
            self.assertTrue(resolved["fallback"])
            self.assertEqual(
                resolved["fallback_reason"],
                "CHAMPION_PROMOTION_CERTIFICATE_INVALID",
            )

    def test_promotion_state_machine_rejects_skipped_transitions(self) -> None:
        candidate = {"status": "CANDIDATE", "promotion_state": "CANDIDATE"}
        evaluating = transition_promotion_state(candidate, "EVALUATING")
        approved = transition_promotion_state(evaluating, "APPROVED")
        promoted = transition_promotion_state(approved, "PROMOTED")
        self.assertEqual(promoted["status"], "PROMOTED")
        with self.assertRaisesRegex(ContractError, "illegal promotion transition"):
            transition_promotion_state(candidate, "PROMOTED")

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

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from three_table_quant.pipeline import _compatible_training_rows, load_config


class PipelineModelRegistryTests(unittest.TestCase):
    def test_production_open_fill_policies_are_locked_together(self) -> None:
        config = load_config("config/system.json")
        self.assertTrue(config["ranking"]["assume_open_fill"])
        self.assertTrue(config["execution"]["daily_open_counts_as_fill"])

    def test_mismatched_open_fill_policies_are_rejected(self) -> None:
        payload = json.loads(Path("config/system.json").read_text(encoding="utf-8"))
        payload["execution"]["daily_open_counts_as_fill"] = False
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "open-fill policies must match",
            ):
                load_config(path)

    def test_single_stock_research_is_configuration_locked_to_audit_only(self) -> None:
        original = json.loads(Path("config/system.json").read_text(encoding="utf-8"))
        locked_mutations = {
            "schema_version": "other",
            "snapshot_schema_version": "other",
            "mode": "LIVE_GATE",
            "limit_lifecycle_evidence": "ORDER_BOOK",
            "required_full_session_minutes": 239,
        }
        for field, value in locked_mutations.items():
            with self.subTest(field=field):
                payload = json.loads(json.dumps(original))
                payload["single_stock_research"][field] = value
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "system.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, f"{field} must remain"):
                        load_config(path)

        for field in (
            "affects_strict_intersection",
            "affects_ranking",
            "affects_order_spec",
        ):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(original))
                payload["single_stock_research"][field] = True
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "system.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, f"{field} must remain"):
                        load_config(path)

        payload = json.loads(json.dumps(original))
        payload["single_stock_research"]["real_auction_gate_connected"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "real_auction_gate_connected must remain"):
                load_config(path)

        payload = json.loads(json.dumps(original))
        payload["single_stock_research"]["batch_deadline_seconds"] = 30.0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "batch_deadline_seconds must remain"):
                load_config(path)

        payload = json.loads(json.dumps(original))
        del payload["single_stock_research"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "configuration is required"):
                load_config(path)

        for path_field in ("single_stock_minute_archive", "security_master"):
            for unsafe_path in ("/tmp/outside-evidence", "../outside-evidence"):
                with self.subTest(path_field=path_field, unsafe_path=unsafe_path):
                    payload = json.loads(json.dumps(original))
                    payload["paths"][path_field] = unsafe_path
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "system.json"
                        path.write_text(json.dumps(payload), encoding="utf-8")
                        with self.assertRaisesRegex(
                            ValueError,
                            "contained relative path",
                        ):
                            load_config(path)

        payload = json.loads(json.dumps(original))
        del payload["paths"]["security_master"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "system.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "point-in-time security master path is required",
            ):
                load_config(path)

        for field, value in (
            ("fetch_timeout_seconds", 5.1),
            ("fetch_attempts", 2),
            ("max_parallel_fetches", 0),
        ):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(original))
                payload["single_stock_research"][field] = value
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "system.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "fetch budget"):
                        load_config(path)

    def test_legacy_frozen_features_never_count_as_formal_v2_samples(self) -> None:
        rows = [
            {"row_id": "legacy", "feature_version": None},
            {"row_id": "old", "feature_version": "legacy_features_v1"},
            {"row_id": "formal", "feature_version": "formal_features_v2"},
        ]
        self.assertEqual(
            _compatible_training_rows(rows, "formal_features_v2"),
            [rows[2]],
        )


if __name__ == "__main__":
    unittest.main()

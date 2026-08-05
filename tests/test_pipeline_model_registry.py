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

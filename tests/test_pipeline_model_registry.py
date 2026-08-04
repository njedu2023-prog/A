from __future__ import annotations

import unittest

from three_table_quant.pipeline import _compatible_training_rows


class PipelineModelRegistryTests(unittest.TestCase):
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

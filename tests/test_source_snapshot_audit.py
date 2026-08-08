from __future__ import annotations

import unittest

from three_table_quant.domain import SourceRow, SourceTable
from three_table_quant.pipeline import _source_snapshots


class SourceSnapshotAuditTests(unittest.TestCase):
    def test_snapshot_freezes_every_displayed_source_row_in_rank_order(self) -> None:
        table = SourceTable(
            source_id="a_top10",
            decision_date="20260807",
            buy_date="20260810",
            exit_date="20260811",
            rows=(
                SourceRow("a_top10", 2, "000002.SZ", "股票二"),
                SourceRow("a_top10", 1, "000001.SZ", "股票一"),
            ),
            url="https://example.test/source",
            content_sha256="content-sha",
            generated_at="2026-08-07T21:30:00+08:00",
            remote_blob_sha="commit-sha",
        )

        snapshots = _source_snapshots([table])

        self.assertEqual(
            snapshots[0]["ranked_rows"],
            [
                {"rank": 1, "ts_code": "000001.SZ", "name": "股票一"},
                {"rank": 2, "ts_code": "000002.SZ", "name": "股票二"},
            ],
        )
        self.assertEqual(snapshots[0]["row_count"], 2)
        self.assertNotIn("values", snapshots[0]["ranked_rows"][0])


if __name__ == "__main__":
    unittest.main()

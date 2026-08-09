from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from three_table_quant.domain import ContractError
from three_table_quant.single_stock_collection import (
    LIFECYCLE_INPUT_FIELD,
    build_candidate_single_stock_research,
)
from three_table_quant.single_stock_minute_archive import (
    MINUTE_ARCHIVE_REFERENCE_SCHEMA,
    MINUTE_ARCHIVE_SCHEMA,
    MINUTE_BAR_SCHEMA,
    MinuteArchiveReference,
    archive_minute_bars,
    load_minute_artifact,
    replay_minute_bars,
)
from three_table_quant.single_stock_research import research_snapshot_sha256
from tests.test_single_stock_collection import (
    ASOF,
    DAY,
    candidate,
    execution,
    minutes,
    security_master,
)


class SingleStockMinuteArchiveTests(unittest.TestCase):
    def test_full_session_is_content_addressed_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = archive_minute_bars(
                directory,
                "000815.SZ",
                DAY,
                minutes(),
                ASOF,
            )

            self.assertEqual(reference.schema_version, MINUTE_ARCHIVE_REFERENCE_SCHEMA)
            self.assertEqual(reference.artifact_schema_version, MINUTE_ARCHIVE_SCHEMA)
            self.assertEqual(reference.bar_schema_version, MINUTE_BAR_SCHEMA)
            self.assertTrue(reference.full_session)
            self.assertEqual(reference.bar_count, 240)
            self.assertEqual(reference.providers, ("TEST",))
            artifact_path = Path(directory) / reference.relative_path
            self.assertTrue(artifact_path.is_file())
            self.assertTrue(reference.relative_path.endswith(".json.gz"))
            self.assertEqual(reference.content_encoding, "gzip")
            self.assertEqual(artifact_path.read_bytes()[4:8], b"\x00\x00\x00\x00")
            self.assertEqual(
                reference.relative_path.split("/", 3)[:3],
                ["2026", DAY, "000815.SZ"],
            )

            payload = load_minute_artifact(directory, reference)
            replayed = replay_minute_bars(directory, reference.to_dict())
            self.assertEqual(payload["bars_content_sha256"], reference.bars_content_sha256)
            self.assertEqual(payload["bar_count"], 240)
            self.assertEqual(len(replayed), 240)
            self.assertEqual(replayed[0].time, "09:30")
            self.assertEqual(replayed[-1].time, "14:59")
            self.assertEqual(replayed[0].provider, "TEST")

    def test_same_capture_is_idempotent_and_changed_content_appends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = minutes()
            first = archive_minute_bars(
                directory, "000815.SZ", DAY, base, ASOF
            )
            before = (Path(directory) / first.relative_path).read_bytes()
            repeated = archive_minute_bars(
                directory,
                "000815.SZ",
                DAY,
                base,
                "2026-08-07T21:30:30+08:00",
            )
            self.assertEqual(repeated.artifact_sha256, first.artifact_sha256)
            self.assertEqual(repeated.relative_path, first.relative_path)
            self.assertNotEqual(repeated.captured_at, first.captured_at)
            self.assertEqual((Path(directory) / first.relative_path).read_bytes(), before)

            changed = list(base)
            changed[0] = replace(
                changed[0],
                close=9.8,
                low=9.8,
                amount=9800.0,
            )
            second = archive_minute_bars(
                directory, "000815.SZ", DAY, changed, ASOF
            )
            self.assertNotEqual(second.artifact_sha256, first.artifact_sha256)
            self.assertNotEqual(second.relative_path, first.relative_path)
            self.assertTrue((Path(directory) / first.relative_path).is_file())
            self.assertTrue((Path(directory) / second.relative_path).is_file())
            self.assertEqual((Path(directory) / first.relative_path).read_bytes(), before)

    def test_incomplete_evidence_is_archived_without_zero_filling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = archive_minute_bars(
                directory, "000815.SZ", DAY, minutes(5), ASOF
            )
            payload = load_minute_artifact(directory, reference)

            self.assertFalse(reference.full_session)
            self.assertEqual(reference.bar_count, 5)
            self.assertEqual(payload["bar_count"], 5)
            self.assertEqual(len(payload["bars"]), 5)
            self.assertNotIn("09:35", {bar["time"] for bar in payload["bars"]})

    def test_artifact_and_reference_tampering_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = archive_minute_bars(
                directory, "000815.SZ", DAY, minutes(), ASOF
            )
            artifact_path = Path(directory) / reference.relative_path
            tampered = bytearray(artifact_path.read_bytes())
            tampered[len(tampered) // 2] ^= 1
            artifact_path.write_bytes(bytes(tampered))
            with self.assertRaisesRegex(ContractError, "gzip|SHA-256 mismatch"):
                load_minute_artifact(directory, reference)

        with tempfile.TemporaryDirectory() as directory:
            reference = archive_minute_bars(
                directory, "000815.SZ", DAY, minutes(), ASOF
            )
            artifact_path = Path(directory) / reference.relative_path
            non_deterministic = bytearray(artifact_path.read_bytes())
            non_deterministic[4] = 1
            artifact_path.write_bytes(bytes(non_deterministic))
            with self.assertRaisesRegex(ContractError, "deterministic mtime=0"):
                load_minute_artifact(directory, reference)

        with tempfile.TemporaryDirectory() as directory:
            reference = archive_minute_bars(
                directory, "000815.SZ", DAY, minutes(), ASOF
            )
            wrong_identity = reference.to_dict()
            wrong_identity["ts_code"] = "000595.SZ"
            with self.assertRaisesRegex(ContractError, "relative_path|ts_code mismatch"):
                load_minute_artifact(directory, wrong_identity)

    def test_research_snapshot_contains_lightweight_reference_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bars = minutes()
            reference = archive_minute_bars(
                directory, "000815.SZ", DAY, bars, ASOF
            )
            item = candidate()
            before = copy.deepcopy(item.to_dict())
            research = build_candidate_single_stock_research(
                item,
                decision_date=DAY,
                decision_asof=ASOF,
                execution=execution(),
                security_master=security_master(),
                minute_bars=bars,
                minute_artifact=reference,
            )

            fact = research["single_stock"]["facts"][LIFECYCLE_INPUT_FIELD]
            embedded = fact["value"]["minute_artifact"]
            self.assertEqual(embedded["artifact_sha256"], reference.artifact_sha256)
            self.assertEqual(
                embedded["bars_content_sha256"],
                reference.bars_content_sha256,
            )
            self.assertNotIn("bars", fact["value"])
            self.assertEqual(
                research["snapshot_sha256"],
                research_snapshot_sha256(research),
            )
            self.assertEqual(item.to_dict(), before)

            legacy_compatible = build_candidate_single_stock_research(
                candidate(),
                decision_date=DAY,
                decision_asof=ASOF,
                execution=execution(),
                security_master=security_master(),
                minute_bars=bars,
            )
            legacy_fact = legacy_compatible["single_stock"]["facts"][
                LIFECYCLE_INPUT_FIELD
            ]
            self.assertNotIn("minute_artifact", legacy_fact["value"])

    def test_reference_must_match_bars_and_must_not_be_future_known(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reference = archive_minute_bars(
                directory, "000815.SZ", DAY, minutes(), ASOF
            )
            changed = minutes()
            changed[0] = replace(changed[0], amount=9800.0)
            with self.assertRaisesRegex(ContractError, "content SHA mismatch"):
                build_candidate_single_stock_research(
                    candidate(),
                    decision_date=DAY,
                    decision_asof=ASOF,
                    execution=execution(),
                    security_master=security_master(),
                    minute_bars=changed,
                    minute_artifact=reference,
                )

            future = reference.to_dict()
            future["captured_at"] = "2026-08-07T21:31:00+08:00"
            future_reference = MinuteArchiveReference.from_dict(future)
            with self.assertRaisesRegex(ContractError, "captured after decision_asof"):
                build_candidate_single_stock_research(
                    candidate(),
                    decision_date=DAY,
                    decision_asof=ASOF,
                    execution=execution(),
                    security_master=security_master(),
                    minute_bars=minutes(),
                    minute_artifact=future_reference,
                )


if __name__ == "__main__":
    unittest.main()

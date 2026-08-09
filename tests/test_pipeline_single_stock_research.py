from __future__ import annotations

import copy
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from three_table_quant.domain import Signal
from three_table_quant.ledger import empty_state, load_state
from three_table_quant.pipeline import (
    RESEARCH_COLLECTION_COMPLETE,
    RESEARCH_COLLECTION_PENDING,
    _complete_optional_single_stock_research,
    _freeze_single_stock_research,
    _persist_core_signal,
)
from three_table_quant.single_stock_collection import LIFECYCLE_INPUT_FIELD
from three_table_quant.single_stock_minute_archive import load_minute_artifact
from three_table_quant.single_stock_research import SINGLE_STOCK_RESEARCH_SCHEMA

from tests.test_single_stock_collection import ASOF, DAY, candidate, execution, minutes


SECURITY_MASTER_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "security_master_20260807.json"
)


class BlockingMarket:
    def minute_bars(self, code: str, trade_date: str) -> list:
        del code, trade_date
        time.sleep(60)
        return []


def decision_core(item: object) -> dict:
    payload = copy.deepcopy(item.to_dict())  # type: ignore[attr-defined]
    payload.pop("single_stock_research", None)
    return payload


def signal_with(item: object) -> dict:
    return Signal(
        signal_id=f"{DAY}-test",
        decision_date=DAY,
        buy_date="20260810",
        exit_date="20260811",
        generated_at=ASOF,
        source_snapshots=[],
        candidates=[item],  # type: ignore[list-item]
        model_version="transparent_shadow_champion_v2",
        status="RANKED",
        market_data_provenance={},
        ranking_engine={"selected_model_id": "transparent_shadow_champion_v2"},
    ).to_dict()


class PipelineSingleStockResearchTests(unittest.TestCase):
    def setUp(self) -> None:
        # Fixed historical D fixtures must not inherit the wall clock of the
        # day on which the test suite happens to run.  fork() carries this
        # deterministic child clock into the isolated research process.
        clock = patch("three_table_quant.pipeline._now", return_value=ASOF)
        clock.start()
        self.addCleanup(clock.stop)

    def test_core_signal_and_shadow_order_are_durable_before_research(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = empty_state()
            signal = signal_with(candidate())

            _persist_core_signal(
                state,
                signal,
                state_path=path,
                tracked_ranks=[1, 2, 3],
            )

            frozen = load_state(path)
            self.assertEqual(len(frozen["signals"]), 1)
            self.assertEqual(len(frozen["trades"]), 1)
            frozen_signal = frozen["signals"][0]
            frozen_candidate = frozen_signal["candidates"][0]
            self.assertEqual(
                frozen_signal["single_stock_research_collection_status"],
                RESEARCH_COLLECTION_PENDING,
            )
            self.assertIsNone(
                frozen_signal["single_stock_research_schema_version"]
            )
            self.assertEqual(frozen_candidate["ts_code"], "000815.SZ")
            self.assertEqual(frozen_candidate["rank"], 1)
            self.assertTrue(frozen_candidate["order_spec"])
            self.assertEqual(
                frozen["trades"][0]["order_spec"],
                frozen_candidate["order_spec"],
            )

    def test_batch_exception_cannot_erase_already_persisted_list(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = empty_state()
            signal = signal_with(candidate())
            _persist_core_signal(
                state,
                signal,
                state_path=path,
                tracked_ranks=[1, 2, 3],
            )
            core = load_state(path)
            observed: dict = {}

            def fail_after_check(*args: object, **kwargs: object) -> None:
                del args, kwargs
                checkpoint = load_state(path)
                observed["candidate_count"] = len(
                    checkpoint["signals"][0]["candidates"]
                )
                observed["trade_count"] = len(checkpoint["trades"])
                observed["order_spec"] = copy.deepcopy(
                    checkpoint["signals"][0]["candidates"][0]["order_spec"]
                )
                raise TimeoutError("research batch timed out")

            issues: list = []
            with patch(
                "three_table_quant.pipeline._freeze_single_stock_research",
                side_effect=fail_after_check,
            ):
                _complete_optional_single_stock_research(
                    state,
                    signal,
                    state_path=path,
                    market=Mock(),
                    execution=execution(),  # type: ignore[arg-type]
                    source_issues=issues,
                    max_workers=5,
                    decision_asof=ASOF,
                    security_master_path=SECURITY_MASTER_PATH,
                )

            final = load_state(path)
            final_signal = final["signals"][0]
            final_candidate = final_signal["candidates"][0]
            self.assertEqual(observed["candidate_count"], 1)
            self.assertEqual(observed["trade_count"], 1)
            self.assertEqual(observed["order_spec"], final_candidate["order_spec"])
            self.assertEqual(final_candidate["rank"], 1)
            self.assertEqual(final["trades"], core["trades"])
            self.assertEqual(
                final_signal["single_stock_research_collection_status"],
                RESEARCH_COLLECTION_COMPLETE,
            )
            self.assertEqual(
                final_signal["single_stock_research_schema_version"],
                SINGLE_STOCK_RESEARCH_SCHEMA,
            )
            self.assertEqual(
                final_candidate["single_stock_research"]["availability"],
                "UNAVAILABLE",
            )
            self.assertEqual(issues[0].code, "SINGLE_STOCK_RESEARCH_BATCH_FAILED")
            self.assertEqual(issues[0].severity, "warning")

    def test_real_blocking_pipeline_stage_returns_with_core_list_intact(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = empty_state()
            signal = signal_with(candidate())
            _persist_core_signal(
                state,
                signal,
                state_path=path,
                tracked_ranks=[1, 2, 3],
            )
            core = load_state(path)
            issues: list = []
            started = time.monotonic()

            _complete_optional_single_stock_research(
                state,
                signal,
                state_path=path,
                market=BlockingMarket(),
                decision_asof=ASOF,
                execution=execution(),  # type: ignore[arg-type]
                source_issues=issues,
                max_workers=1,
                batch_deadline_seconds=0.15,
                security_master_path=SECURITY_MASTER_PATH,
            )

            elapsed = time.monotonic() - started
            final = load_state(path)
            research = final["signals"][0]["candidates"][0][
                "single_stock_research"
            ]
            self.assertLess(elapsed, 1.5)
            self.assertEqual(final["trades"], core["trades"])
            self.assertEqual(
                final["signals"][0]["candidates"][0]["order_spec"],
                core["signals"][0]["candidates"][0]["order_spec"],
            )
            self.assertEqual(research["availability"], "UNAVAILABLE")
            self.assertIn(
                "SINGLE_STOCK_RESEARCH_BATCH_DEADLINE_EXCEEDED",
                research["unavailable_reason"],
            )
            self.assertEqual(
                issues[0].code,
                "SINGLE_STOCK_RESEARCH_BATCH_DEADLINE_EXCEEDED",
            )

    def test_success_archives_raw_minutes_after_core_boundary(self) -> None:
        with TemporaryDirectory() as folder:
            item = candidate()
            market = Mock()
            market.minute_bars.return_value = minutes()
            issues: list = []

            _freeze_single_stock_research(
                [item],
                market=market,
                decision_date=DAY,
                decision_asof=ASOF,
                execution=execution(),  # type: ignore[arg-type]
                source_issues=issues,
                max_workers=1,
                batch_deadline_seconds=2.0,
                minute_archive_root=folder,
                security_master_path=SECURITY_MASTER_PATH,
            )

            fact = item.single_stock_research["single_stock"]["facts"][
                LIFECYCLE_INPUT_FIELD
            ]
            reference = fact["value"]["minute_artifact"]
            artifact = load_minute_artifact(folder, reference)
            self.assertEqual(artifact["bar_count"], 240)
            self.assertEqual(artifact["ts_code"], item.ts_code)
            self.assertEqual(issues, [])

    def test_capture_and_provenance_time_come_from_after_fetch(self) -> None:
        batch_started_at = "2026-08-07T21:30:00+08:00"
        fetch_completed_at = "2026-08-07T21:30:07+08:00"
        with TemporaryDirectory() as folder:
            item = candidate()
            market = Mock()
            market.minute_bars.return_value = minutes()
            with patch(
                "three_table_quant.pipeline._now",
                return_value=fetch_completed_at,
            ):
                _freeze_single_stock_research(
                    [item],
                    market=market,
                    decision_date=DAY,
                    decision_asof=batch_started_at,
                    execution=execution(),  # type: ignore[arg-type]
                    source_issues=[],
                    max_workers=1,
                    batch_deadline_seconds=2.0,
                    minute_archive_root=folder,
                    security_master_path=SECURITY_MASTER_PATH,
                )

            lifecycle_input = item.single_stock_research["single_stock"][
                "facts"
            ][LIFECYCLE_INPUT_FIELD]
            reference = lifecycle_input["value"]["minute_artifact"]
            self.assertEqual(reference["captured_at"], fetch_completed_at)
            self.assertEqual(
                lifecycle_input["provenance"]["fetched_at"],
                fetch_completed_at,
            )
            self.assertNotEqual(reference["captured_at"], batch_started_at)

    def test_fetch_that_completes_after_d_cannot_create_available_evidence(self) -> None:
        with TemporaryDirectory() as folder:
            item = candidate()
            market = Mock()
            market.minute_bars.return_value = minutes()
            issues: list = []
            with patch(
                "three_table_quant.pipeline._now",
                return_value="2026-08-08T00:00:01+08:00",
            ):
                _freeze_single_stock_research(
                    [item],
                    market=market,
                    decision_date=DAY,
                    decision_asof="2026-08-07T23:59:59+08:00",
                    execution=execution(),  # type: ignore[arg-type]
                    source_issues=issues,
                    max_workers=1,
                    batch_deadline_seconds=2.0,
                    minute_archive_root=folder,
                    security_master_path=SECURITY_MASTER_PATH,
                )

            self.assertEqual(item.single_stock_research["availability"], "UNAVAILABLE")
            self.assertIn(
                "SINGLE_STOCK_RESEARCH_FETCH_COMPLETED_AFTER_D",
                item.single_stock_research["unavailable_reason"],
            )
            self.assertEqual(
                issues[0].code,
                "SINGLE_STOCK_RESEARCH_FETCH_COMPLETED_AFTER_D",
            )
            self.assertEqual(list(Path(folder).rglob("*.json.gz")), [])

    def test_archive_failure_is_unavailable_and_never_changes_decision(self) -> None:
        with TemporaryDirectory() as folder:
            item = candidate()
            before = decision_core(item)
            issues: list = []
            with patch(
                "three_table_quant.pipeline.archive_minute_bars",
                side_effect=OSError("archive storage unavailable"),
            ):
                _freeze_single_stock_research(
                    [item],
                    market=Mock(minute_bars=Mock(return_value=minutes())),
                    decision_date=DAY,
                    decision_asof=ASOF,
                    execution=execution(),  # type: ignore[arg-type]
                    source_issues=issues,
                    max_workers=1,
                    batch_deadline_seconds=2.0,
                    minute_archive_root=folder,
                    security_master_path=SECURITY_MASTER_PATH,
                )

            self.assertEqual(decision_core(item), before)
            self.assertEqual(item.single_stock_research["availability"], "UNAVAILABLE")
            self.assertIn(
                "SINGLE_STOCK_MINUTE_ARCHIVE_FAILED",
                item.single_stock_research["unavailable_reason"],
            )
            self.assertEqual(issues[0].code, "SINGLE_STOCK_MINUTE_ARCHIVE_FAILED")
            self.assertEqual(issues[0].severity, "warning")

    def test_pending_research_recovered_after_d_is_explicitly_unavailable(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = empty_state()
            signal = signal_with(candidate())
            _persist_core_signal(
                state,
                signal,
                state_path=path,
                tracked_ranks=[1, 2, 3],
            )
            market = Mock()
            issues: list = []

            _complete_optional_single_stock_research(
                state,
                signal,
                state_path=path,
                market=market,
                execution=execution(),  # type: ignore[arg-type]
                source_issues=issues,
                max_workers=5,
                decision_asof="2026-08-08T09:00:00+08:00",
                security_master_path=SECURITY_MASTER_PATH,
            )

            market.minute_bars.assert_not_called()
            research = load_state(path)["signals"][0]["candidates"][0][
                "single_stock_research"
            ]
            self.assertEqual(research["availability"], "UNAVAILABLE")
            self.assertIn(
                "SINGLE_STOCK_RESEARCH_RECOVERY_AFTER_D",
                research["unavailable_reason"],
            )
            self.assertEqual(
                issues[0].code,
                "SINGLE_STOCK_RESEARCH_RECOVERY_AFTER_D",
            )

    def test_legacy_signal_without_pending_marker_is_never_backfilled(self) -> None:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            state = empty_state()
            signal = signal_with(candidate())
            state["signals"].append(signal)
            market = Mock()

            _complete_optional_single_stock_research(
                state,
                signal,
                state_path=path,
                market=market,
                execution=execution(),  # type: ignore[arg-type]
                source_issues=[],
                max_workers=5,
                security_master_path=SECURITY_MASTER_PATH,
            )

            market.minute_bars.assert_not_called()
            self.assertFalse(path.exists())
            self.assertNotIn(
                "single_stock_research_collection_status",
                signal,
            )

    def test_success_adds_evidence_without_changing_frozen_decision(self) -> None:
        item = candidate()
        before = decision_core(item)
        market = Mock()
        market.minute_bars.return_value = minutes()
        issues: list = []

        _freeze_single_stock_research(
            [item],
            market=market,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            source_issues=issues,
            security_master_path=SECURITY_MASTER_PATH,
        )

        self.assertEqual(decision_core(item), before)
        self.assertEqual(item.single_stock_research["decision_date"], DAY)
        self.assertEqual(issues, [])

    def test_minute_failure_still_freezes_unknown_research_and_keeps_candidate(self) -> None:
        item = candidate()
        before = decision_core(item)
        market = Mock()
        market.minute_bars.side_effect = RuntimeError("provider unavailable")
        issues: list = []

        _freeze_single_stock_research(
            [item],
            market=market,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            source_issues=issues,
            security_master_path=SECURITY_MASTER_PATH,
        )

        self.assertEqual(decision_core(item), before)
        self.assertEqual(
            item.single_stock_research["limit_lifecycle"]["availability"],
            "UNAVAILABLE",
        )
        self.assertEqual([issue.severity for issue in issues], ["warning"])

    def test_research_builder_failure_is_warning_not_candidate_failure(self) -> None:
        item = candidate()
        before = decision_core(item)
        issues: list = []
        with patch(
            "three_table_quant.pipeline.build_candidate_single_stock_research",
            side_effect=RuntimeError("audit builder failed"),
        ):
            _freeze_single_stock_research(
                [item],
                market=Mock(minute_bars=Mock(return_value=minutes())),
                decision_date=DAY,
                decision_asof=ASOF,
                execution=execution(),
                source_issues=issues,
                security_master_path=SECURITY_MASTER_PATH,
            )

        self.assertEqual(decision_core(item), before)
        self.assertEqual(
            item.single_stock_research["schema_version"],
            "single_stock_research_audit_v1",
        )
        self.assertEqual(item.single_stock_research["availability"], "UNAVAILABLE")
        self.assertIsNone(item.single_stock_research["single_stock"])
        self.assertIn(
            "SINGLE_STOCK_RESEARCH_BUILD_FAILED",
            item.single_stock_research["unavailable_reason"],
        )
        self.assertEqual(item.single_stock_research["ts_code"], item.ts_code)
        self.assertEqual(issues[0].code, "SINGLE_STOCK_RESEARCH_BUILD_FAILED")
        self.assertEqual(issues[0].severity, "warning")

    def test_zero_candidates_never_requests_minute_data(self) -> None:
        market = Mock()
        _freeze_single_stock_research(
            [],
            market=market,
            decision_date=DAY,
            decision_asof=ASOF,
            execution=execution(),
            source_issues=[],
            security_master_path=SECURITY_MASTER_PATH,
        )
        market.minute_bars.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import unittest

from three_table_quant.domain import ContractError
from three_table_quant.security_master import (
    PointInTimeSecurityMaster,
    SECURITY_MASTER_FIELDS,
)


DAY = "20260807"
ASOF = "2026-08-07T21:30:00+08:00"


def facts(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "is_suspended": False,
        "is_st": False,
        "is_delisting_period": False,
        "trading_rules_verified": True,
        "board": "MAIN",
        "price_limit_pct": 10.0,
        "price_tick": 0.01,
    }
    values.update(overrides)
    return values


def record(
    record_id: str,
    *,
    known_at: str = "2026-08-07T15:01:00+08:00",
    fetched_at: str = "2026-08-07T15:05:00+08:00",
    values: dict[str, object] | None = None,
    missing_reasons: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "ts_code": "000815.SZ",
        "effective_from": "20260101",
        "effective_to": None,
        "known_at": known_at,
        "fetched_at": fetched_at,
        "provider": "OFFICIAL_SECURITY_MASTER_FIXTURE",
        "dataset_version": "fixture-v1",
        "revision_id": record_id,
        "source_uri": "https://www.sse.com.cn/fixture/security-master",
        "facts": values or facts(),
        "missing_reasons": missing_reasons or {},
    }


def payload(
    records: list[dict[str, object]],
    *,
    generated_at: str = ASOF,
) -> dict[str, object]:
    return {
        "schema_version": "point_in_time_security_master_v1",
        "provider": "TEST_MASTER",
        "dataset_version": "fixture-v1",
        "generated_at": generated_at,
        "records": records,
    }


class PointInTimeSecurityMasterTests(unittest.TestCase):
    def test_absent_record_is_all_unknown_never_false_or_pass(self) -> None:
        master = PointInTimeSecurityMaster.from_payload(payload([]))

        resolved = master.resolve(
            "000815.SZ",
            decision_date=DAY,
            decision_asof=ASOF,
        )

        self.assertEqual(set(resolved.values), set(SECURITY_MASTER_FIELDS))
        self.assertTrue(all(value is None for value in resolved.values.values()))
        self.assertEqual(
            set(resolved.missing_reasons.values()),
            {"NO_POINT_IN_TIME_SECURITY_RECORD"},
        )
        self.assertIsNone(resolved.record_id)

    def test_missing_provenance_uses_real_master_generation_time(self) -> None:
        generated_at = "2026-08-07T18:00:00+08:00"
        master = PointInTimeSecurityMaster.from_payload(
            payload([], generated_at=generated_at)
        )

        resolved = master.resolve("000815.SZ", decision_date=DAY, decision_asof=ASOF)

        self.assertEqual(resolved.known_at, generated_at)
        self.assertEqual(resolved.fetched_at, generated_at)

    def test_future_generated_bootstrap_cannot_masquerade_as_d_asof(self) -> None:
        master = PointInTimeSecurityMaster.from_payload(
            payload([], generated_at="2026-08-08T00:00:00+08:00")
        )

        with self.assertRaisesRegex(ContractError, "future bootstrap"):
            master.resolve("000815.SZ", decision_date=DAY, decision_asof=ASOF)

    def test_asof_excludes_a_later_correction(self) -> None:
        master = PointInTimeSecurityMaster.from_payload(
            payload(
                [
                    record("r1", values=facts(is_st=False)),
                    record(
                        "r2",
                        known_at="2026-08-07T21:31:00+08:00",
                        fetched_at="2026-08-07T21:32:00+08:00",
                        values=facts(is_st=True),
                    ),
                ]
            )
        )

        resolved = master.resolve("000815.SZ", decision_date=DAY, decision_asof=ASOF)

        self.assertEqual(resolved.record_id, "r1")
        self.assertIs(resolved.value("is_st"), False)

    def test_fetched_after_asof_is_not_eligible_even_if_known_earlier(self) -> None:
        master = PointInTimeSecurityMaster.from_payload(
            payload(
                [
                    record(
                        "late-fetch",
                        known_at="2026-08-07T15:00:00+08:00",
                        fetched_at="2026-08-07T21:31:00+08:00",
                    )
                ]
            )
        )

        resolved = master.resolve("000815.SZ", decision_date=DAY, decision_asof=ASOF)

        self.assertIsNone(resolved.record_id)
        self.assertIsNone(resolved.value("is_suspended"))

    def test_bool_contract_rejects_integer_zero(self) -> None:
        malformed = record("bad", values=facts(is_suspended=0))
        with self.assertRaisesRegex(ContractError, "is_suspended must be bool"):
            PointInTimeSecurityMaster.from_payload(payload([malformed]))

    def test_verified_rules_require_board_limit_and_tick(self) -> None:
        values = facts(board=None)
        reasons = {"board": "OFFICIAL_BOARD_NOT_AVAILABLE"}
        with self.assertRaisesRegex(ContractError, "requires board"):
            PointInTimeSecurityMaster.from_payload(
                payload([record("bad-rules", values=values, missing_reasons=reasons)])
            )

    def test_unknown_fact_requires_a_reason_and_known_fact_forbids_one(self) -> None:
        values = facts(is_st=None)
        with self.assertRaisesRegex(ContractError, "missing_reasons.is_st"):
            PointInTimeSecurityMaster.from_payload(
                payload([record("missing-reason", values=values)])
            )

        with_reason = record(
            "known-with-reason",
            values=facts(),
            missing_reasons={"is_st": "SHOULD_NOT_EXIST"},
        )
        with self.assertRaisesRegex(ContractError, "cannot have a missing reason"):
            PointInTimeSecurityMaster.from_payload(payload([with_reason]))

    def test_equal_asof_overlapping_revisions_are_ambiguous(self) -> None:
        second = copy.deepcopy(record("r2"))
        master = PointInTimeSecurityMaster.from_payload(
            payload([record("r1"), second])
        )

        with self.assertRaisesRegex(ContractError, "ambiguous"):
            master.resolve("000815.SZ", decision_date=DAY, decision_asof=ASOF)

    def test_decision_asof_is_checked_in_shanghai_timezone(self) -> None:
        master = PointInTimeSecurityMaster.from_payload(payload([]))
        resolved = master.resolve(
            "000815.SZ",
            decision_date=DAY,
            decision_asof="2026-08-07T13:30:00+00:00",
        )
        self.assertEqual(resolved.decision_date, DAY)


if __name__ == "__main__":
    unittest.main()

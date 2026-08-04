from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from three_table_quant.candidate_facts import (
    candidate_display_fields,
    candidate_validation_inputs,
)
from three_table_quant.domain import ContractError
from three_table_quant.sources import (
    SOURCE_A,
    SOURCE_DECISION,
    SOURCE_PREMIUM,
    SourceLoader,
    diagnose_source_quality,
    freeze_gate_issue,
    parse_a_top10,
    parse_decision,
    parse_pointer,
    parse_premium,
    source_snapshot_changes,
    strict_intersection,
    validate_timeline,
)


ROOT = Path(__file__).resolve().parents[1]
CALENDAR_PATH = ROOT / "data" / "trading_calendar_2026.json"
A_REPO_SHA = "a" * 40
DECISION_REPO_SHA = "b" * 40
A_MODEL_SHA = "c" * 40


def csv_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    lines = [",".join(header)] + [",".join(str(value) for value in row) for row in rows]
    return ("\n".join(lines) + "\n").encode()


A_HEADER = [
    "trade_date",
    "verify_date",
    "rank",
    "ts_code",
    "name",
    "prob_final",
    "run_id",
    "commit_sha",
    "generated_at_utc",
    "model_contract_status",
]

PREMIUM_HEADER = [
    "rank",
    "trade_date",
    "base_date",
    "buy_date",
    "target_date",
    "ts_code",
    "name",
    "is_top10",
    "rank_group",
    "premium_rank_score",
    "premium_final_score",
    "t_up_attack_score",
    "t1_accept_score",
    "premium_eligible",
    "premium_bucket",
    "model_can_rank",
    "close_T",
]


def a_csv(
    codes: list[str],
    *,
    decision_date: str = "20260803",
    buy_date: str = "20260804",
    commit_sha: str = A_MODEL_SHA,
) -> bytes:
    return csv_bytes(
        A_HEADER,
        [
            [
                decision_date,
                buy_date,
                rank,
                code,
                f"A{rank}",
                0.60 - rank / 100,
                "run-a",
                commit_sha,
                f"{decision_date[:4]}-{decision_date[4:6]}-{decision_date[6:]}T12:00:00Z",
                "valid",
            ]
            for rank, code in enumerate(codes, start=1)
        ],
    )


def premium_csv(
    codes: list[str],
    *,
    decision_date: str = "20260803",
    buy_date: str = "20260804",
    exit_date: str = "20260805",
    is_top10: str = "1",
    close_T: object = 10.25,
) -> bytes:
    return csv_bytes(
        PREMIUM_HEADER,
        [
            [
                rank,
                decision_date,
                decision_date,
                buy_date,
                exit_date,
                code,
                f"P{rank}",
                is_top10,
                "TOP10",
                50 - rank,
                49 - rank,
                48 - rank,
                47 - rank,
                0,
                "WATCH",
                0,
                close_T,
            ]
            for rank, code in enumerate(codes, start=1)
        ],
    )


def decision_row(
    rank: object,
    code: str,
    *,
    stage_watch_rank: object | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "rank": rank,
        "ts_code": code,
        "name": f"D{rank}",
        "action": "PENDING",
        "stage_transition": "2→3",
        "industry": "IT服务Ⅱ",
        "d_close": 10.25,
        "mechanism_limit_pct": 10.0,
        "estimated_up_limit": 11.28,
        "decision_p_fill": 0.5,
        "decision_e_ret": 0.01,
        "decision_ev": -0.001,
        "decision_cost": 0.001,
        "decision_risk_penalty": 0.01,
    }
    if stage_watch_rank is not None:
        row["stage_watch_rank"] = stage_watch_rank
    return row


def decision_bytes(
    rows: list[dict[str, object]],
    *,
    candidate_rows: list[dict[str, object]] | None = None,
    decision_date: str = "20260803",
    buy_date: str = "20260804",
    exit_date: str = "20260805",
) -> bytes:
    stage_rows = []
    for stage_rank, source_row in enumerate(rows, start=1):
        row = dict(source_row)
        row.setdefault("stage_watch_rank", stage_rank)
        row.setdefault("observation_rank", row["stage_watch_rank"])
        row.setdefault("observation_selected", 1)
        stage_rows.append(row)
    return json.dumps(
        {
            "schema_version": "decision_action_plan_v12_top10_trade_selector",
            "generated_at_utc": (
                f"{decision_date[:4]}-{decision_date[4:6]}-{decision_date[6:]}T12:10:00Z"
            ),
            "report_date": buy_date,
            "signal_date": decision_date,
            "exec_date": buy_date,
            "exit_date": exit_date,
            "stage_watch_count": len(stage_rows),
            "stage_watch_eligible_count": len(stage_rows),
            "stage_watch_display_limit": 10,
            "stage_watchlist": stage_rows,
            "candidates": candidate_rows if candidate_rows is not None else stage_rows,
        },
        ensure_ascii=False,
    ).encode()


class FakeHttp:
    def __init__(self, responses: dict[str, bytes | str]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get_bytes(self, url: str) -> bytes:
        self.requested.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected URL: {url}")
        value = self.responses[url]
        return value if isinstance(value, bytes) else value.encode()


def loader_config() -> dict[str, object]:
    return {
        "sources": {
            "a_repo_head_url": "https://api.test/a/head",
            "a_raw_base_template": "https://raw.test/a/{commit_sha}/",
            "a_pointer_path": "outputs/learning/_last_run.txt",
            "a_csv_path_template": "outputs/learning/pred_top10_{date}.csv",
            "decision_repo_head_url": "https://api.test/decision/head",
            "decision_raw_base_template": "https://raw.test/decision/{commit_sha}/",
            "premium_pointer_path": "outputs/premium/_last_run.txt",
            "premium_csv_path_template": "outputs/premium/premium_top10_{date}.csv",
            "decision_index_path": "outputs/decision/report_index.json",
        }
    }


def loader_responses(*, premium_ok: str = "True", a_pointer_commit: str = A_MODEL_SHA) -> dict[str, bytes | str]:
    a_base = f"https://raw.test/a/{A_REPO_SHA}/"
    decision_base = f"https://raw.test/decision/{DECISION_REPO_SHA}/"
    return {
        "https://api.test/a/head": json.dumps({"sha": A_REPO_SHA}),
        "https://api.test/decision/head": json.dumps({"sha": DECISION_REPO_SHA}),
        a_base + "outputs/learning/_last_run.txt": (
            f"trade_date=20260803 run_id=run-a commit_sha={a_pointer_commit} "
            "utc=2026-08-03T12:01:00Z\n"
        ),
        a_base + "outputs/learning/pred_top10_20260803.csv": a_csv(
            ["600000.SH", "000001.SZ", "600001.SH"]
        ),
        decision_base + "outputs/premium/_last_run.txt": (
            "trade_date: 20260803\n"
            "run_id: premium-run\n"
            "commit_sha: deadbeef\n"
            "created_at_utc: 2026-08-03T12:05:00Z\n"
            f"ok: {premium_ok}\n"
            "buy_date: 20260804\n"
            "target_date: 20260805\n"
        ),
        decision_base + "outputs/premium/premium_top10_20260803.csv": premium_csv(
            ["600000.SH", "000001.SZ", "000002.SZ"]
        ),
        decision_base + "outputs/decision/report_index.json": json.dumps(
            {
                "schema_version": "decision_report_index_v1",
                "generated_at_utc": "2026-08-03T12:11:00Z",
                "latest_report_date": "20260804",
                "latest_action_url": "outputs/decision/action_plan_latest.json",
                "reports": [
                    {
                        "report_date": "20260804",
                        "action_url": "outputs/decision/action_plan_20260804.json",
                    }
                ],
            }
        ),
        decision_base + "outputs/decision/action_plan_20260804.json": decision_bytes(
            [decision_row(1, "600000.SH"), decision_row(2, "000002.SZ")]
        ),
    }


class SourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.a = parse_a_top10(
            a_csv(["600000.SH", "000001.SZ", "600001.SH"]),
            "a.csv",
        )
        self.premium = parse_premium(
            premium_csv(["600000.SH", "000001.SZ", "000002.SZ"]),
            "premium.csv",
        )

    def decision(self, rows: list[dict[str, object]]) -> object:
        return parse_decision(decision_bytes(rows), "decision.json")

    def test_strict_three_way_intersection_has_actual_count(self) -> None:
        decision = self.decision(
            [decision_row(1, "600000.SH"), decision_row(2, "000002.SZ")]
        )
        result = strict_intersection([self.a, self.premium, decision])
        self.assertEqual([item.ts_code for item in result], ["600000.SH"])
        display = candidate_display_fields(result[0])
        validation = candidate_validation_inputs(result[0])
        self.assertEqual(
            display,
            {
                "stage_transition": "2→3",
                "industry": "IT服务Ⅱ",
                "d_close": 10.25,
                "mechanism_limit_pct": 10.0,
                "estimated_up_limit": 11.28,
            },
        )
        self.assertEqual(validation["decision_d_close"], 10.25)
        self.assertEqual(validation["premium_d_close"], 10.25)
        self.assertEqual(validation["limit_up_price"], 11.28)
        self.assertEqual(
            validation["limit_up_source"],
            "DECISION_FROZEN_LIMIT_PRICE",
        )

    def test_d_close_uses_decision_and_cross_checks_premium(self) -> None:
        decision = self.decision([decision_row(1, "600000.SH")])
        premium = parse_premium(
            premium_csv(
                ["600000.SH"],
                close_T=10.254,
            ),
            "premium.csv",
        )
        candidate = strict_intersection([self.a, premium, decision])[0]
        facts = candidate_validation_inputs(candidate)
        self.assertEqual(facts["d_close"], 10.25)
        self.assertEqual(facts["premium_d_close"], 10.254)

        mismatched = parse_premium(
            premium_csv(
                ["600000.SH"],
                close_T=10.26,
            ),
            "premium.csv",
        )
        with self.assertRaisesRegex(
            ContractError,
            "Decision d_close and Premium close_T disagree",
        ):
            strict_intersection([self.a, mismatched, decision])

    def test_frozen_limit_price_must_match_d_close_and_mechanism(self) -> None:
        invalid = decision_row(1, "600000.SH")
        invalid["estimated_up_limit"] = 11.27
        with self.assertRaisesRegex(
            ContractError,
            "estimated_up_limit disagrees",
        ):
            strict_intersection(
                [
                    self.a,
                    self.premium,
                    self.decision([invalid]),
                ]
            )

    def test_decision_uses_visible_top10_not_full_candidate_pool(self) -> None:
        displayed = [
            decision_row(50 + rank, f"{600000 + rank:06d}.SH")
            for rank in range(1, 11)
        ]
        full_pool = displayed + [decision_row(11, "600000.SH")]
        decision = parse_decision(
            decision_bytes(displayed, candidate_rows=full_pool),
            "decision.json",
        )
        result = strict_intersection([self.a, self.premium, decision])
        self.assertEqual(result, [])
        self.assertEqual([row.rank for row in decision.rows], list(range(1, 11)))
        self.assertEqual(decision.rows[0].values["rank"], 51)
        self.assertEqual(decision.rows[0].values["stage_transition"], "2→3")
        self.assertEqual(decision.rows[0].values["industry"], "IT服务Ⅱ")
        self.assertEqual(decision.rows[0].values["d_close"], 10.25)

    def test_decision_rejects_non_top10_display_contract(self) -> None:
        displayed = [
            decision_row(rank, f"{600000 + rank:06d}.SH")
            for rank in range(1, 11)
        ]
        displayed.append(decision_row(11, "600000.SH"))
        payload = json.loads(decision_bytes(displayed))
        payload["stage_watch_display_limit"] = 11
        with self.assertRaisesRegex(ContractError, "must equal 10"):
            parse_decision(
                json.dumps(payload, ensure_ascii=False).encode(),
                "decision.json",
            )

    def test_decision_missing_watchlist_never_falls_back_to_candidates(self) -> None:
        payload = json.loads(
            decision_bytes([decision_row(1, "600000.SH")])
        )
        payload.pop("stage_watchlist")
        with self.assertRaisesRegex(ContractError, "missing required fields"):
            parse_decision(
                json.dumps(payload, ensure_ascii=False).encode(),
                "decision.json",
            )

    def test_zero_intersection_is_valid_and_not_padded(self) -> None:
        decision = self.decision([decision_row(1, "002999.SZ")])
        self.assertEqual(strict_intersection([self.a, self.premium, decision]), [])

    def test_duplicate_code_fails_closed(self) -> None:
        with self.assertRaises(ContractError):
            self.decision(
                [decision_row(1, "600000.SH"), decision_row(2, "600000.SH")]
            )

    def test_missing_fractional_and_zero_visible_rank_fail_closed(self) -> None:
        for invalid in (None, 1.5, 0, -1, "1.0"):
            row = decision_row(53, "600000.SH", stage_watch_rank=invalid)
            if invalid is None:
                row["stage_watch_rank"] = None
            payload = json.loads(decision_bytes([row]))
            payload["stage_watchlist"][0]["stage_watch_rank"] = invalid
            with self.subTest(rank=invalid), self.assertRaises(ContractError):
                parse_decision(
                    json.dumps(payload, ensure_ascii=False).encode(),
                    "decision.json",
                )

    def test_decision_watch_counts_must_match_visible_rows(self) -> None:
        payload = json.loads(
            decision_bytes([decision_row(53, "600000.SH")])
        )
        payload["stage_watch_eligible_count"] = 2
        with self.assertRaisesRegex(ContractError, "must equal min"):
            parse_decision(
                json.dumps(payload, ensure_ascii=False).encode(),
                "decision.json",
            )

    def test_premium_requires_display_membership_flags(self) -> None:
        with self.assertRaises(ContractError):
            parse_premium(
                premium_csv(["600000.SH"], is_top10="0"),
                "premium.csv",
            )

    def test_premium_requires_finite_positive_d_close(self) -> None:
        for invalid in ("", 0, -1, "nan", "inf", True):
            with self.subTest(close_T=invalid), self.assertRaises(ContractError):
                parse_premium(
                    premium_csv(["600000.SH"], close_T=invalid),
                    "premium.csv",
                )

        lines = premium_csv(["600000.SH"]).decode().strip().splitlines()
        without_close = (
            ",".join(lines[0].split(",")[:-1])
            + "\n"
            + ",".join(lines[1].split(",")[:-1])
            + "\n"
        ).encode()
        with self.assertRaisesRegex(ContractError, "missing required columns"):
            parse_premium(without_close, "premium.csv")

    def test_decision_requires_frozen_candidate_facts(self) -> None:
        required_positive = (
            "d_close",
            "mechanism_limit_pct",
            "estimated_up_limit",
        )
        for field in required_positive:
            for invalid in ("", 0, -1, "nan", "inf", True):
                row = decision_row(1, "600000.SH")
                row[field] = invalid
                with (
                    self.subTest(field=field, value=invalid),
                    self.assertRaises(ContractError),
                ):
                    self.decision([row])

        row = decision_row(1, "600000.SH")
        row["industry"] = " "
        with self.assertRaisesRegex(ContractError, "industry must be non-empty"):
            self.decision([row])

    def test_consistent_duplicate_premium_header_is_merged_with_warning(self) -> None:
        lines = premium_csv(["600000.SH"]).decode().strip().splitlines()
        data = (lines[0] + ",premium_final_score\n" + lines[1] + ",48\n").encode()
        table = parse_premium(data, "premium.csv")
        warnings = diagnose_source_quality([table])
        merged = [item for item in warnings if item.code == "SOURCE_DUPLICATE_HEADERS_MERGED"]
        self.assertEqual(len(merged), 1)
        self.assertIn("premium_final_score", merged[0].details["headers"])

    def test_conflicting_duplicate_premium_header_fails_closed(self) -> None:
        lines = premium_csv(["600000.SH"]).decode().strip().splitlines()
        data = (lines[0] + ",premium_final_score\n" + lines[1] + ",999\n").encode()
        with self.assertRaises(ContractError):
            parse_premium(data, "premium.csv")

    def test_missing_decision_auxiliary_fields_warn_but_keep_membership(self) -> None:
        row = decision_row(1, "600000.SH")
        for field in (
            "decision_p_fill",
            "decision_e_ret",
            "decision_ev",
            "decision_cost",
            "decision_risk_penalty",
        ):
            row[field] = ""
        table = self.decision([row])
        self.assertEqual(table.rows[0].ts_code, "600000.SH")
        issues = diagnose_source_quality([table])
        coverage = [
            item
            for item in issues
            if item.code == "DECISION_AUXILIARY_COVERAGE_INCOMPLETE"
        ]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0].details["non_null_counts"]["decision_p_fill"], 0)

    def test_timeline_mismatch_is_error(self) -> None:
        decision = parse_decision(
            decision_bytes(
                [],
                decision_date="20260804",
                buy_date="20260805",
                exit_date="20260806",
            ),
            "decision.json",
        )
        issues = validate_timeline(
            [self.a, self.premium, decision],
            CALENDAR_PATH,
        )
        self.assertIn("SOURCE_DATE_MISMATCH", {item.code for item in issues})

    def test_official_sse_calendar_accepts_spring_festival_gap(self) -> None:
        tables = [
            replace(
                self.a,
                decision_date="20260213",
                buy_date="20260224",
            ),
            replace(
                self.premium,
                decision_date="20260213",
                buy_date="20260224",
                exit_date="20260225",
            ),
            replace(
                self.decision([]),
                decision_date="20260213",
                buy_date="20260224",
                exit_date="20260225",
            ),
        ]
        self.assertEqual(validate_timeline(tables, CALENDAR_PATH), [])

    def test_official_sse_calendar_rejects_holiday_as_t(self) -> None:
        tables = [
            replace(self.a, decision_date="20260213", buy_date="20260223"),
            replace(
                self.premium,
                decision_date="20260213",
                buy_date="20260223",
                exit_date="20260224",
            ),
            replace(
                self.decision([]),
                decision_date="20260213",
                buy_date="20260223",
                exit_date="20260224",
            ),
        ]
        issues = validate_timeline(tables, CALENDAR_PATH)
        self.assertIn("TRADING_CALENDAR_MISMATCH", {item.code for item in issues})

    def test_invalid_calendar_date_fails_closed(self) -> None:
        tables = [
            replace(self.a, decision_date="20261340", buy_date="20260804"),
            replace(self.premium, decision_date="20261340"),
            replace(self.decision([]), decision_date="20261340"),
        ]
        issues = validate_timeline(tables, CALENDAR_PATH)
        self.assertIn("TRADING_CALENDAR_MISMATCH", {item.code for item in issues})

    def test_freeze_gate_is_inclusive_and_now_is_injectable(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        early = freeze_gate_issue(
            "20260803",
            "20:00",
            now=datetime(2026, 8, 3, 19, 59, tzinfo=tz),
        )
        self.assertIsNotNone(early)
        self.assertEqual(early.code, "FIRST_FREEZE_TOO_EARLY")
        self.assertIsNone(
            freeze_gate_issue(
                "20260803",
                "20:00",
                now=datetime(2026, 8, 3, 20, 0, tzinfo=tz),
            )
        )

    def test_frozen_snapshot_detects_hash_or_repository_commit_change(self) -> None:
        frozen = [
            {
                "source_id": SOURCE_A,
                "content_sha256": "old-hash",
                "repository_commit_sha": "old-commit",
            }
        ]
        live = [
            {
                "source_id": SOURCE_A,
                "content_sha256": "new-hash",
                "repository_commit_sha": "old-commit",
            }
        ]
        changes = source_snapshot_changes(frozen, live)
        self.assertEqual(changes[SOURCE_A]["frozen_content_sha256"], "old-hash")
        self.assertEqual(changes[SOURCE_A]["live_content_sha256"], "new-hash")
        self.assertEqual(source_snapshot_changes(live, live), {})

    def test_pointer_accepts_colon_and_space_delimited_equals_formats(self) -> None:
        self.assertEqual(
            parse_pointer("trade_date: 20260804\nbuy_date: 20260805\n")[
                "trade_date"
            ],
            "20260804",
        )
        parsed = parse_pointer(
            "trade_date=20260804 run_id=123 commit_sha=abc utc=2026-08-04T11:00:00Z\n"
        )
        self.assertEqual(parsed["trade_date"], "20260804")
        self.assertEqual(parsed["commit_sha"], "abc")

    def test_loader_pins_both_repositories_and_uses_reports_zero_action(self) -> None:
        responses = loader_responses()
        http = FakeHttp(responses)
        tables, issues = SourceLoader(loader_config(), http).load()
        self.assertEqual(issues, [])
        self.assertEqual({table.source_id for table in tables}, {
            SOURCE_A,
            SOURCE_PREMIUM,
            SOURCE_DECISION,
        })
        by_id = {table.source_id: table for table in tables}
        self.assertEqual(by_id[SOURCE_A].remote_blob_sha, A_REPO_SHA)
        self.assertEqual(by_id[SOURCE_PREMIUM].remote_blob_sha, DECISION_REPO_SHA)
        self.assertEqual(by_id[SOURCE_DECISION].remote_blob_sha, DECISION_REPO_SHA)
        self.assertNotIn(
            f"https://raw.test/decision/{DECISION_REPO_SHA}/"
            "outputs/decision/action_plan_latest.json",
            http.requested,
        )
        self.assertTrue(
            all("/main/" not in url for url in http.requested if "raw.test" in url)
        )

    def test_loader_rejects_premium_pointer_ok_false(self) -> None:
        tables, issues = SourceLoader(
            loader_config(),
            FakeHttp(loader_responses(premium_ok="False")),
        ).load()
        self.assertNotIn(SOURCE_PREMIUM, {table.source_id for table in tables})
        premium_issues = [item for item in issues if item.source_id == SOURCE_PREMIUM]
        self.assertTrue(premium_issues)
        self.assertIn("pointer ok is not true", premium_issues[0].message)

    def test_loader_rejects_a_pointer_provenance_mismatch(self) -> None:
        tables, issues = SourceLoader(
            loader_config(),
            FakeHttp(loader_responses(a_pointer_commit="d" * 40)),
        ).load()
        self.assertNotIn(SOURCE_A, {table.source_id for table in tables})
        a_issues = [item for item in issues if item.source_id == SOURCE_A]
        self.assertTrue(a_issues)
        self.assertIn("commit_sha does not match", a_issues[0].message)


if __name__ == "__main__":
    unittest.main()

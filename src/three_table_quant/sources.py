from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .calendar import DEFAULT_CALENDAR_PATH, TradingCalendar, parse_calendar_date
from .domain import (
    Candidate,
    ContractError,
    SourceIssue,
    SourceRow,
    SourceTable,
    normalize_date,
    normalize_ts_code,
)
from .http import HttpClient


SOURCE_A = "a_top10"
SOURCE_PREMIUM = "premium_top10"
SOURCE_DECISION = "decision_table"
SOURCE_IDS = (SOURCE_A, SOURCE_PREMIUM, SOURCE_DECISION)
SHANGHAI = ZoneInfo("Asia/Shanghai")
FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
PROVENANCE_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
POSITIVE_INTEGER_RE = re.compile(r"^[1-9][0-9]*$")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _csv_values_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    left_number = _number(left)
    right_number = _number(right)
    return (
        left_number is not None
        and right_number is not None
        and math.isfinite(left_number)
        and math.isfinite(right_number)
        and left_number == right_number
    )


def _csv_rows(
    data: bytes,
    source_id: str,
) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    text = data.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        headers = next(reader)
    except StopIteration:
        headers = []
    if not headers:
        raise ContractError(f"{source_id}: missing CSV header")
    headers = [str(header or "").strip() for header in headers]
    if any(not header for header in headers):
        raise ContractError(f"{source_id}: blank CSV header")
    duplicate_headers = tuple(
        sorted({header for header in headers if headers.count(header) > 1})
    )
    rows: list[dict[str, str]] = []
    for row_number, values in enumerate(reader, start=2):
        if len(values) != len(headers):
            raise ContractError(
                f"{source_id}: CSV row {row_number} has {len(values)} values for "
                f"{len(headers)} headers"
            )
        merged: dict[str, str] = {}
        for header, raw_value in zip(headers, values, strict=True):
            value = raw_value.strip()
            if header not in merged or not merged[header]:
                merged[header] = value
                continue
            if not value:
                continue
            if not _csv_values_equivalent(merged[header], value):
                raise ContractError(
                    f"{source_id}: duplicate CSV header {header!r} has conflicting "
                    f"values on row {row_number}"
                )
        if duplicate_headers:
            merged["_merged_duplicate_headers"] = "|".join(duplicate_headers)
        rows.append(merged)
    return rows, duplicate_headers


def _first_value(row: dict[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _number(value: Any) -> float | None:
    if value in (None, "", "null", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _require_fields(
    rows: list[dict[str, Any]],
    required: Iterable[str],
    source_id: str,
) -> None:
    required_set = set(required)
    available = set(rows[0]) if rows else set()
    missing_columns = sorted(required_set - available)
    if missing_columns:
        raise ContractError(f"{source_id}: missing required columns {missing_columns}")
    for index, row in enumerate(rows, start=1):
        missing_values = sorted(
            field for field in required_set if row.get(field) in (None, "")
        )
        if missing_values:
            raise ContractError(
                f"{source_id}: row {index} has empty required fields {missing_values}"
            )


def _require_any_numeric(
    rows: list[dict[str, Any]],
    alternatives: tuple[str, ...],
    source_id: str,
) -> None:
    if not any(field in rows[0] for field in alternatives):
        raise ContractError(
            f"{source_id}: missing required numeric alternatives {list(alternatives)}"
        )
    for index, row in enumerate(rows, start=1):
        value = _number(_first_value(row, alternatives))
        if value is None or not math.isfinite(value):
            raise ContractError(
                f"{source_id}: row {index} has invalid numeric value for "
                f"{'/'.join(alternatives)}"
            )


def _require_numeric_fields(
    rows: list[dict[str, Any]],
    fields: Iterable[str],
    source_id: str,
) -> None:
    for index, row in enumerate(rows, start=1):
        for field in fields:
            value = _number(row.get(field))
            if value is None or not math.isfinite(value):
                raise ContractError(
                    f"{source_id}: row {index} has invalid numeric {field}={row.get(field)!r}"
                )


def _validate_optional_numeric_fields(
    rows: list[dict[str, Any]],
    fields: Iterable[str],
    source_id: str,
) -> None:
    for index, row in enumerate(rows, start=1):
        for field in fields:
            value = row.get(field)
            if value in (None, "", "null", "None"):
                continue
            parsed = _number(value)
            if parsed is None or not math.isfinite(parsed):
                raise ContractError(
                    f"{source_id}: row {index} has invalid optional numeric "
                    f"{field}={value!r}"
                )


def _uniform_value(
    rows: list[dict[str, Any]],
    field: str,
    source_id: str,
) -> str:
    values = {str(row.get(field) or "").strip() for row in rows}
    if "" in values or len(values) != 1:
        raise ContractError(f"{source_id}: mixed or empty {field}")
    return next(iter(values))


def _parse_rank(value: Any, source_id: str) -> int:
    if isinstance(value, bool):
        raise ContractError(f"{source_id}: invalid rank {value!r}")
    if isinstance(value, int):
        rank = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ContractError(f"{source_id}: rank must be an integer, got {value!r}")
        rank = int(value)
    else:
        raw = str(value or "").strip()
        if not POSITIVE_INTEGER_RE.fullmatch(raw):
            raise ContractError(f"{source_id}: invalid rank {value!r}")
        rank = int(raw)
    if rank <= 0:
        raise ContractError(f"{source_id}: rank must be positive, got {rank}")
    return rank


def _parse_aware_timestamp(value: Any, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ContractError(f"{field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_post_close_timestamp(value: Any, decision_date: str, field_name: str) -> str:
    parsed = _parse_aware_timestamp(value, field_name)
    local = parsed.astimezone(SHANGHAI)
    if local.strftime("%Y%m%d") != decision_date:
        raise ContractError(
            f"{field_name} local date {local:%Y%m%d} does not match D={decision_date}"
        )
    if local.time() < time(15, 0):
        raise ContractError(f"{field_name} is earlier than the D-day 15:00 close")
    return parsed.isoformat()


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "1.0", "true", "yes"}


def _validate_provenance_commit(value: Any, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    if not PROVENANCE_COMMIT_RE.fullmatch(raw):
        raise ContractError(f"{field_name} must be a 7..40 character hex commit")
    return raw


def _resolve_commit_sha(data: bytes, source_id: str) -> str:
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{source_id}: invalid repository head JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"{source_id}: repository head payload must be an object")
    sha = str(payload.get("sha") or (payload.get("object") or {}).get("sha") or "").lower()
    if not FULL_COMMIT_RE.fullmatch(sha):
        raise ContractError(f"{source_id}: repository head does not contain a full commit SHA")
    return sha


def _pinned_url(base_template: str, commit_sha: str, path: str) -> str:
    base = base_template.format(commit_sha=commit_sha).rstrip("/") + "/"
    clean_path = str(path).lstrip("/")
    if ".." in Path(clean_path).parts:
        raise ContractError(f"source path may not traverse directories: {path!r}")
    return base + clean_path


def _ranked_rows(
    raw_rows: list[dict[str, Any]],
    source_id: str,
    *,
    max_rank: int | None,
    rank_field: str = "rank",
) -> tuple[SourceRow, ...]:
    parsed: list[SourceRow] = []
    seen_codes: set[str] = set()
    seen_ranks: set[int] = set()
    for row in raw_rows:
        if rank_field not in row or row.get(rank_field) in (None, ""):
            raise ContractError(
                f"{source_id}: explicit {rank_field} is required"
            )
        rank = _parse_rank(row[rank_field], source_id)
        if max_rank is not None and rank > max_rank:
            continue
        code = normalize_ts_code(_first_value(row, ("ts_code", "symbol", "code")))
        if code in seen_codes:
            raise ContractError(f"{source_id}: duplicate code {code}")
        if rank in seen_ranks:
            raise ContractError(f"{source_id}: duplicate rank {rank}")
        seen_codes.add(code)
        seen_ranks.add(rank)
        parsed.append(
            SourceRow(
                source_id=source_id,
                rank=rank,
                ts_code=code,
                name=str(_first_value(row, ("name", "stock_name"), code)).strip(),
                values=dict(row),
            )
        )
    parsed.sort(key=lambda item: (item.rank, item.ts_code))
    ranks = [item.rank for item in parsed]
    if ranks and ranks != list(range(1, len(ranks) + 1)):
        raise ContractError(
            f"{source_id}: displayed ranks must be contiguous from 1, got {ranks}"
        )
    return tuple(parsed)


def parse_a_top10(data: bytes, url: str) -> SourceTable:
    rows, _ = _csv_rows(data, SOURCE_A)
    if not rows:
        raise ContractError("a_top10: empty table")
    _require_fields(
        rows,
        (
            "trade_date",
            "verify_date",
            "rank",
            "ts_code",
            "name",
            "run_id",
            "commit_sha",
            "generated_at_utc",
            "model_contract_status",
        ),
        SOURCE_A,
    )
    _require_any_numeric(rows, ("prob_final", "Probability", "prob"), SOURCE_A)
    for index, row in enumerate(rows, start=1):
        probability = _number(_first_value(row, ("prob_final", "Probability", "prob")))
        if probability is None or not 0.0 <= probability <= 1.0:
            raise ContractError(
                f"a_top10: row {index} probability must be within [0, 1]"
            )
        if str(row.get("model_contract_status") or "").strip().lower() != "valid":
            raise ContractError(
                f"a_top10: row {index} model_contract_status is not valid"
            )
    decision_dates = {normalize_date(row.get("trade_date"), "trade_date") for row in rows}
    buy_dates = {normalize_date(row.get("verify_date"), "verify_date") for row in rows}
    if len(decision_dates) != 1 or len(buy_dates) != 1:
        raise ContractError("a_top10: mixed trade_date or verify_date")
    ranked = _ranked_rows(rows, SOURCE_A, max_rank=10)
    if not 1 <= len(ranked) <= 10:
        raise ContractError(f"a_top10: expected 1..10 displayed rows, got {len(ranked)}")
    run_id = _uniform_value(rows, "run_id", SOURCE_A)
    commit_sha = _validate_provenance_commit(
        _uniform_value(rows, "commit_sha", SOURCE_A),
        "a_top10 commit_sha",
    )
    generated = _uniform_value(rows, "generated_at_utc", SOURCE_A)
    _validate_post_close_timestamp(
        generated,
        next(iter(decision_dates)),
        "a_top10 generated_at_utc",
    )
    for row in rows:
        if str(row.get("run_id") or "").strip() != run_id:
            raise ContractError("a_top10: run_id changed within the table")
        if str(row.get("commit_sha") or "").strip().lower() != commit_sha:
            raise ContractError("a_top10: commit_sha changed within the table")
    return SourceTable(
        source_id=SOURCE_A,
        decision_date=next(iter(decision_dates)),
        buy_date=next(iter(buy_dates)),
        exit_date="",
        rows=ranked,
        url=url,
        content_sha256=_sha256(data),
        generated_at=str(generated) if generated else None,
    )


def parse_pointer(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and ("=" not in line or line.index(":") < line.index("=")):
            key, value = line.split(":", 1)
            values[key.strip()] = value.strip()
            continue
        # a-top10 currently uses one space-delimited key=value line.
        for token in line.split():
            if "=" in token:
                key, value = token.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def _require_pointer_fields(
    pointer: dict[str, str],
    fields: Iterable[str],
    source_id: str,
) -> None:
    missing = sorted(field for field in fields if not str(pointer.get(field) or "").strip())
    if missing:
        raise ContractError(f"{source_id}: pointer missing required fields {missing}")


def _validate_a_pointer(pointer: dict[str, str], table: SourceTable) -> None:
    _require_pointer_fields(
        pointer,
        ("trade_date", "run_id", "commit_sha", "utc"),
        SOURCE_A,
    )
    pointer_date = normalize_date(pointer["trade_date"], "a-top10 pointer trade_date")
    if table.decision_date != pointer_date:
        raise ContractError("a-top10 pointer date does not match dated CSV")
    pointer_commit = _validate_provenance_commit(
        pointer["commit_sha"],
        "a-top10 pointer commit_sha",
    )
    row = table.rows[0]
    if str(row.values.get("run_id") or "").strip() != pointer["run_id"].strip():
        raise ContractError("a-top10 pointer run_id does not match CSV")
    if str(row.values.get("commit_sha") or "").strip().lower() != pointer_commit:
        raise ContractError("a-top10 pointer commit_sha does not match CSV")
    pointer_time = _parse_aware_timestamp(pointer["utc"], "a-top10 pointer utc")
    _validate_post_close_timestamp(
        pointer["utc"],
        table.decision_date,
        "a-top10 pointer utc",
    )
    table_time = _parse_aware_timestamp(table.generated_at, "a-top10 generated_at_utc")
    if pointer_time < table_time:
        raise ContractError("a-top10 pointer timestamp precedes the dated CSV")


def _validate_premium_pointer(pointer: dict[str, str], table: SourceTable) -> str:
    _require_pointer_fields(
        pointer,
        (
            "trade_date",
            "run_id",
            "commit_sha",
            "created_at_utc",
            "ok",
            "buy_date",
            "target_date",
        ),
        SOURCE_PREMIUM,
    )
    if not _is_true(pointer["ok"]):
        raise ContractError(f"premium_top10: pointer ok is not true: {pointer['ok']!r}")
    _validate_provenance_commit(
        pointer["commit_sha"],
        "premium pointer commit_sha",
    )
    pointer_date = normalize_date(pointer["trade_date"], "premium pointer trade_date")
    if table.decision_date != pointer_date:
        raise ContractError("premium pointer date does not match dated CSV")
    if normalize_date(pointer["buy_date"], "premium pointer buy_date") != table.buy_date:
        raise ContractError("premium pointer buy_date does not match CSV")
    if normalize_date(pointer["target_date"], "premium pointer target_date") != table.exit_date:
        raise ContractError("premium pointer target_date does not match CSV")
    _validate_post_close_timestamp(
        pointer["created_at_utc"],
        table.decision_date,
        "premium pointer created_at_utc",
    )
    return pointer["created_at_utc"]


def parse_premium(data: bytes, url: str) -> SourceTable:
    rows, _ = _csv_rows(data, SOURCE_PREMIUM)
    if not rows:
        raise ContractError("premium_top10: empty table")
    _require_fields(
        rows,
        (
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
        ),
        SOURCE_PREMIUM,
    )
    _require_numeric_fields(
        rows,
        (
            "premium_rank_score",
            "premium_final_score",
            "t_up_attack_score",
            "t1_accept_score",
        ),
        SOURCE_PREMIUM,
    )
    for index, row in enumerate(rows, start=1):
        for field in (
            "premium_rank_score",
            "premium_final_score",
            "t_up_attack_score",
            "t1_accept_score",
        ):
            value = float(row[field])
            if not 0.0 <= value <= 100.0:
                raise ContractError(
                    f"premium_top10: row {index} {field} must be within [0, 100]"
                )
        if not _is_true(row.get("is_top10")) or str(row.get("rank_group")).strip() != "TOP10":
            raise ContractError(
                f"premium_top10: row {index} is not marked as a displayed TOP10 row"
            )
    date_keys = {
        "decision_date": "trade_date",
        "base_date": "base_date",
        "buy_date": "buy_date",
        "exit_date": "target_date",
    }
    values: dict[str, str] = {}
    for target, key in date_keys.items():
        dates = {normalize_date(row.get(key), key) for row in rows}
        if len(dates) != 1:
            raise ContractError(f"premium_top10: mixed {key}")
        values[target] = next(iter(dates))
    if values["decision_date"] != values["base_date"]:
        raise ContractError("premium_top10: trade_date != base_date")
    ranked = _ranked_rows(rows, SOURCE_PREMIUM, max_rank=10)
    if not 1 <= len(ranked) <= 10:
        raise ContractError(f"premium_top10: expected 1..10 displayed rows, got {len(ranked)}")
    return SourceTable(
        source_id=SOURCE_PREMIUM,
        decision_date=values["decision_date"],
        buy_date=values["buy_date"],
        exit_date=values["exit_date"],
        rows=ranked,
        url=url,
        content_sha256=_sha256(data),
    )


def parse_decision(data: bytes, url: str) -> SourceTable:
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"decision_table: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError("decision_table: payload must be an object")
    required_top_level = (
        "schema_version",
        "generated_at_utc",
        "report_date",
        "signal_date",
        "exec_date",
        "exit_date",
        "candidates",
        "stage_watchlist",
        "stage_watch_count",
        "stage_watch_eligible_count",
        "stage_watch_display_limit",
    )
    missing_top_level = [
        field for field in required_top_level if payload.get(field) in (None, "")
    ]
    if missing_top_level:
        raise ContractError(
            f"decision_table: missing required fields {sorted(missing_top_level)}"
        )
    raw_rows = payload.get("stage_watchlist")
    if not isinstance(raw_rows, list):
        raise ContractError("decision_table: stage_watchlist must be a list")

    def parse_count(field: str, *, positive: bool = False) -> int:
        value = payload.get(field)
        minimum = 1 if positive else 0
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            qualifier = "positive" if positive else "nonnegative"
            raise ContractError(
                f"decision_table: {field} must be a {qualifier} integer"
            )
        return value

    watch_count = parse_count("stage_watch_count")
    eligible_count = parse_count("stage_watch_eligible_count")
    display_limit = parse_count("stage_watch_display_limit", positive=True)
    if display_limit != 10:
        raise ContractError(
            "decision_table: stage_watch_display_limit must equal 10"
        )
    if watch_count != len(raw_rows):
        raise ContractError(
            "decision_table: stage_watch_count does not match stage_watchlist"
        )
    if watch_count != min(eligible_count, display_limit):
        raise ContractError(
            "decision_table: stage_watch_count must equal "
            "min(stage_watch_eligible_count, stage_watch_display_limit)"
        )
    candidate_rows = payload.get("candidates")
    if not isinstance(candidate_rows, list) or not all(
        isinstance(row, dict) for row in candidate_rows
    ):
        raise ContractError("decision_table: candidates must be a list of objects")
    if raw_rows:
        if not all(isinstance(row, dict) for row in raw_rows):
            raise ContractError(
                "decision_table: every stage_watchlist row must be an object"
            )
        _require_fields(
            raw_rows,
            (
                "stage_watch_rank",
                "rank",
                "ts_code",
                "name",
                "action",
                "stage_transition",
                "observation_rank",
                "observation_selected",
            ),
            SOURCE_DECISION,
        )
        optional_numeric_fields = (
            "decision_p_fill",
            "decision_e_ret",
            "decision_ev",
            "decision_cost",
            "decision_risk_penalty",
        )
        _validate_optional_numeric_fields(
            raw_rows,
            optional_numeric_fields,
            SOURCE_DECISION,
        )
        for index, row in enumerate(raw_rows, start=1):
            stage_rank = _parse_rank(row.get("stage_watch_rank"), SOURCE_DECISION)
            observation_rank = _parse_rank(
                row.get("observation_rank"),
                SOURCE_DECISION,
            )
            if observation_rank != stage_rank:
                raise ContractError(
                    f"decision_table: row {index} observation_rank must equal "
                    "stage_watch_rank"
                )
            if not _is_true(row.get("observation_selected")):
                raise ContractError(
                    f"decision_table: row {index} observation_selected must be true"
                )
            if str(row.get("stage_transition") or "").strip() not in {"2→3", "3→4"}:
                raise ContractError(
                    f"decision_table: row {index} has invalid stage_transition"
                )
            p_fill = _number(row.get("decision_p_fill"))
            cost = _number(row.get("decision_cost"))
            risk_penalty = _number(row.get("decision_risk_penalty"))
            if p_fill is not None and not 0.0 <= p_fill <= 1.0:
                raise ContractError(
                    f"decision_table: row {index} decision_p_fill must be within [0, 1]"
                )
            if (cost is not None and cost < 0.0) or (
                risk_penalty is not None and risk_penalty < 0.0
            ):
                raise ContractError(
                    f"decision_table: row {index} cost and risk penalty must be nonnegative"
                )
        candidates_by_code: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidate_rows:
            code = normalize_ts_code(
                _first_value(candidate, ("ts_code", "symbol", "code"))
            )
            candidates_by_code.setdefault(code, []).append(candidate)
        for index, row in enumerate(raw_rows, start=1):
            code = normalize_ts_code(
                _first_value(row, ("ts_code", "symbol", "code"))
            )
            matches = candidates_by_code.get(code, [])
            if len(matches) != 1:
                raise ContractError(
                    f"decision_table: row {index} must map to exactly one candidate"
                )
            if _parse_rank(matches[0].get("rank"), SOURCE_DECISION) != _parse_rank(
                row.get("rank"),
                SOURCE_DECISION,
            ):
                raise ContractError(
                    f"decision_table: row {index} candidate rank does not match"
                )
    # The main Decision table is rendered from `stage_watchlist`.
    # Its `rank` is the full candidate-pool rank; `stage_watch_rank` is the
    # visible 1..N table rank.  The aggregation contract uses exactly these
    # zero-to-ten visible rows and never pads from the full candidate pool.
    ranked = _ranked_rows(
        raw_rows,
        SOURCE_DECISION,
        max_rank=None,
        rank_field="stage_watch_rank",
    )
    decision_date = normalize_date(payload.get("signal_date"), "signal_date")
    generated = str(payload.get("generated_at_utc") or "")
    _validate_post_close_timestamp(
        generated,
        decision_date,
        "decision generated_at_utc",
    )
    report_date = normalize_date(payload.get("report_date"), "report_date")
    buy_date = normalize_date(payload.get("exec_date"), "exec_date")
    if report_date != buy_date:
        raise ContractError("decision_table: report_date must equal exec_date")
    return SourceTable(
        source_id=SOURCE_DECISION,
        decision_date=decision_date,
        buy_date=buy_date,
        exit_date=normalize_date(payload.get("exit_date"), "exit_date"),
        rows=ranked,
        url=url,
        content_sha256=_sha256(data),
        generated_at=generated,
    )


class SourceLoader:
    def __init__(self, config: dict[str, Any], http: HttpClient | Any | None = None) -> None:
        self.config = config
        self.http = http or HttpClient()

    def load(self) -> tuple[list[SourceTable], list[SourceIssue]]:
        issues: list[SourceIssue] = []
        tables: list[SourceTable] = []
        source_config = self.config["sources"]
        a_commit: str | None = None
        decision_repo_commit: str | None = None
        try:
            a_commit = _resolve_commit_sha(
                self.http.get_bytes(source_config["a_repo_head_url"]),
                SOURCE_A,
            )
        except Exception as exc:
            issues.append(SourceIssue("SOURCE_HEAD_FAILED", "error", SOURCE_A, str(exc)))
        try:
            decision_repo_commit = _resolve_commit_sha(
                self.http.get_bytes(source_config["decision_repo_head_url"]),
                "top10_decision",
            )
        except Exception as exc:
            for source_id in (SOURCE_PREMIUM, SOURCE_DECISION):
                issues.append(
                    SourceIssue("SOURCE_HEAD_FAILED", "error", source_id, str(exc))
                )

        if a_commit is not None:
            try:
                pointer_url = _pinned_url(
                    source_config["a_raw_base_template"],
                    a_commit,
                    source_config["a_pointer_path"],
                )
                pointer = parse_pointer(self.http.get_bytes(pointer_url).decode("utf-8-sig"))
                pointer_date = normalize_date(
                    pointer.get("trade_date"),
                    "a-top10 pointer trade_date",
                )
                csv_path = source_config["a_csv_path_template"].format(date=pointer_date)
                url = _pinned_url(
                    source_config["a_raw_base_template"],
                    a_commit,
                    csv_path,
                )
                table = replace(
                    parse_a_top10(self.http.get_bytes(url), url),
                    remote_blob_sha=a_commit,
                )
                _validate_a_pointer(pointer, table)
                tables.append(table)
            except Exception as exc:  # fail closed at the source boundary
                issues.append(
                    SourceIssue(
                        "SOURCE_LOAD_FAILED",
                        "error",
                        SOURCE_A,
                        str(exc),
                        {"repository_commit_sha": a_commit},
                    )
                )

        premium_pointer: dict[str, str] = {}
        if decision_repo_commit is not None:
            try:
                pointer_url = _pinned_url(
                    source_config["decision_raw_base_template"],
                    decision_repo_commit,
                    source_config["premium_pointer_path"],
                )
                premium_pointer = parse_pointer(
                    self.http.get_bytes(pointer_url).decode("utf-8-sig")
                )
                pointer_date = normalize_date(
                    premium_pointer.get("trade_date"),
                    "premium pointer trade_date",
                )
                premium_path = source_config["premium_csv_path_template"].format(
                    date=pointer_date
                )
                premium_url = _pinned_url(
                    source_config["decision_raw_base_template"],
                    decision_repo_commit,
                    premium_path,
                )
                premium = replace(
                    parse_premium(self.http.get_bytes(premium_url), premium_url),
                    remote_blob_sha=decision_repo_commit,
                )
                generated = _validate_premium_pointer(premium_pointer, premium)
                premium = replace(premium, generated_at=generated)
                tables.append(premium)
            except Exception as exc:
                issues.append(
                    SourceIssue(
                        "SOURCE_LOAD_FAILED",
                        "error",
                        SOURCE_PREMIUM,
                        str(exc),
                        {
                            "pointer": premium_pointer,
                            "repository_commit_sha": decision_repo_commit,
                        },
                    )
                )

            try:
                index_url = _pinned_url(
                    source_config["decision_raw_base_template"],
                    decision_repo_commit,
                    source_config["decision_index_path"],
                )
                index_data = self.http.get_bytes(index_url)
                try:
                    index_payload = json.loads(index_data.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ContractError(f"decision index is invalid JSON: {exc}") from exc
                if not isinstance(index_payload, dict):
                    raise ContractError("decision index must be an object")
                if index_payload.get("schema_version") != "decision_report_index_v1":
                    raise ContractError(
                        f"unexpected decision index schema "
                        f"{index_payload.get('schema_version')!r}"
                    )
                reports = index_payload.get("reports")
                if not isinstance(reports, list) or not reports or not isinstance(reports[0], dict):
                    raise ContractError("decision index reports[0] is required")
                first_report = reports[0]
                action_path = str(first_report.get("action_url") or "").strip()
                first_report_date = normalize_date(
                    first_report.get("report_date"),
                    "decision index reports[0].report_date",
                )
                latest_report_date = normalize_date(
                    index_payload.get("latest_report_date"),
                    "decision index latest_report_date",
                )
                if first_report_date != latest_report_date:
                    raise ContractError(
                        "decision index latest_report_date does not match reports[0]"
                    )
                if not action_path:
                    raise ContractError("decision index reports[0] has no action_url")
                decision_url = _pinned_url(
                    source_config["decision_raw_base_template"],
                    decision_repo_commit,
                    action_path,
                )
                decision_data = self.http.get_bytes(decision_url)
                decision = replace(
                    parse_decision(decision_data, decision_url),
                    remote_blob_sha=decision_repo_commit,
                )
                if decision.buy_date != first_report_date:
                    raise ContractError(
                        "decision action report_date does not match index reports[0]"
                    )
                _validate_post_close_timestamp(
                    index_payload.get("generated_at_utc"),
                    decision.decision_date,
                    "decision index generated_at_utc",
                )
                tables.append(decision)
            except Exception as exc:
                issues.append(
                    SourceIssue(
                        "SOURCE_LOAD_FAILED",
                        "error",
                        SOURCE_DECISION,
                        str(exc),
                        {"repository_commit_sha": decision_repo_commit},
                    )
                )

        return tables, issues


def _is_false(value: Any) -> bool:
    return str(value or "").strip().lower() in {"0", "0.0", "false", "no", "none", ""}


def diagnose_source_quality(tables: list[SourceTable]) -> list[SourceIssue]:
    """Report upstream quality flags without changing upstream files or membership."""
    by_id = {table.source_id: table for table in tables}
    issues: list[SourceIssue] = []
    for table in tables:
        duplicate_headers = sorted(
            {
                header
                for row in table.rows
                for header in str(
                    row.values.get("_merged_duplicate_headers") or ""
                ).split("|")
                if header
            }
        )
        if duplicate_headers:
            issues.append(
                SourceIssue(
                    "SOURCE_DUPLICATE_HEADERS_MERGED",
                    "warning",
                    table.source_id,
                    "重复CSV列名的逐行值一致或仅一侧非空，已安全合并；源仓库仍应修复表头",
                    {
                        "headers": duplicate_headers,
                        "row_count": len(table.rows),
                    },
                )
            )
    a_table = by_id.get(SOURCE_A)
    if a_table and a_table.rows and all(_is_false(row.values.get("intraday_available")) for row in a_table.rows):
        issues.append(
            SourceIssue(
                "A_TOP10_INTRADAY_COVERAGE_ZERO",
                "warning",
                SOURCE_A,
                "当前展示行的 intraday_available 全为0；成员关系仍可读取，但分时证据缺失",
                {"row_count": len(a_table.rows)},
            )
        )
    premium = by_id.get(SOURCE_PREMIUM)
    if premium and premium.rows:
        if all(_is_false(row.values.get("premium_eligible")) for row in premium.rows):
            issues.append(
                SourceIssue(
                    "PREMIUM_NO_ELIGIBLE_ROWS",
                    "warning",
                    SOURCE_PREMIUM,
                    "当前 Premium 展示行的 premium_eligible 全为0；保留其表内成员关系，但不把该状态视为交易背书",
                    {"row_count": len(premium.rows)},
                )
            )
        if all(_is_false(row.values.get("model_can_rank")) for row in premium.rows):
            issues.append(
                SourceIssue(
                    "PREMIUM_MODEL_NOT_PROMOTED",
                    "warning",
                    SOURCE_PREMIUM,
                    "当前 Premium 展示行的 model_can_rank 全为 false，模型尚未通过晋级门槛",
                    {"row_count": len(premium.rows)},
                )
            )
    decision = by_id.get(SOURCE_DECISION)
    if decision and decision.rows:
        auxiliary_fields = (
            "decision_p_fill",
            "decision_e_ret",
            "decision_ev",
            "decision_cost",
            "decision_risk_penalty",
        )
        coverage = {
            field: sum(
                1
                for row in decision.rows
                if (
                    (value := _number(row.values.get(field))) is not None
                    and math.isfinite(value)
                )
            )
            for field in auxiliary_fields
        }
        if any(count < len(decision.rows) for count in coverage.values()):
            issues.append(
                SourceIssue(
                    "DECISION_AUXILIARY_COVERAGE_INCOMPLETE",
                    "warning",
                    SOURCE_DECISION,
                    "Decision辅助模型字段存在缺失；实际展示成员继续参与交集，缺失值不作为数值特征",
                    {
                        "row_count": len(decision.rows),
                        "non_null_counts": coverage,
                    },
                )
            )
        actions = sorted({str(row.values.get("action") or "") for row in decision.rows})
        if not any(action == "BUY" for action in actions):
            issues.append(
                SourceIssue(
                    "DECISION_NO_FORMAL_BUY",
                    "warning",
                    SOURCE_DECISION,
                    "当前 Decision 表没有正式 BUY 行；这里只使用成员关系与辅助特征",
                    {"actions": actions, "row_count": len(decision.rows)},
                )
            )
    return issues


def validate_timeline(
    tables: list[SourceTable],
    calendar_path: str | Path = DEFAULT_CALENDAR_PATH,
) -> list[SourceIssue]:
    issues: list[SourceIssue] = []
    by_id = {table.source_id: table for table in tables}
    if set(by_id) != set(SOURCE_IDS):
        issues.append(
            SourceIssue(
                "SOURCE_SET_INCOMPLETE",
                "error",
                "aggregate",
                "all three source tables are required",
                {"available": sorted(by_id), "required": list(SOURCE_IDS)},
            )
        )
        return issues
    decision_dates = {source_id: by_id[source_id].decision_date for source_id in SOURCE_IDS}
    buy_dates = {source_id: by_id[source_id].buy_date for source_id in SOURCE_IDS}
    exit_dates = {
        SOURCE_PREMIUM: by_id[SOURCE_PREMIUM].exit_date,
        SOURCE_DECISION: by_id[SOURCE_DECISION].exit_date,
    }
    if len(set(decision_dates.values())) != 1:
        issues.append(
            SourceIssue(
                "SOURCE_DATE_MISMATCH",
                "error",
                "aggregate",
                "decision dates differ; cross-date intersection is forbidden",
                {"decision_dates": decision_dates},
            )
        )
    if len(set(buy_dates.values())) != 1:
        issues.append(
            SourceIssue(
                "BUY_DATE_MISMATCH",
                "error",
                "aggregate",
                "T dates differ",
                {"buy_dates": buy_dates},
            )
        )
    if len(set(exit_dates.values())) != 1:
        issues.append(
            SourceIssue(
                "EXIT_DATE_MISMATCH",
                "error",
                "aggregate",
                "T+1 dates differ",
                {"exit_dates": exit_dates},
            )
        )
    if (
        len(set(decision_dates.values())) == 1
        and len(set(buy_dates.values())) == 1
        and len(set(exit_dates.values())) == 1
    ):
        decision_date = next(iter(decision_dates.values()))
        buy_date = next(iter(buy_dates.values()))
        exit_date = next(iter(exit_dates.values()))
        try:
            calendar = TradingCalendar.from_file(calendar_path)
            calendar.validate_d_t_t1(decision_date, buy_date, exit_date)
        except ContractError as exc:
            issues.append(
                SourceIssue(
                    "TRADING_CALENDAR_MISMATCH",
                    "error",
                    "aggregate",
                    str(exc),
                    {
                        "D": decision_date,
                        "T": buy_date,
                        "T1": exit_date,
                        "calendar_path": str(calendar_path),
                    },
                )
            )
    return issues


def freeze_gate_issue(
    decision_date: str,
    freeze_after_local_time: str,
    *,
    now: datetime | None = None,
) -> SourceIssue | None:
    """Return an error while a same-day first freeze is earlier than the configured gate."""
    try:
        d = parse_calendar_date(decision_date, "freeze D")
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", freeze_after_local_time):
            raise ContractError(
                f"freeze_after_local_time must be HH:MM, got {freeze_after_local_time!r}"
            )
        hour, minute = (int(item) for item in freeze_after_local_time.split(":"))
        current = now or datetime.now(SHANGHAI)
        if current.tzinfo is None:
            raise ContractError("freeze gate now must include a timezone")
        local = current.astimezone(SHANGHAI)
        if local.date() < d:
            raise ContractError(
                f"D={decision_date} is in the future relative to {local:%Y%m%d}"
            )
        if local.date() == d and local.time() < time(hour, minute):
            return SourceIssue(
                "FIRST_FREEZE_TOO_EARLY",
                "error",
                "aggregate",
                f"first D freeze is blocked before {freeze_after_local_time} Asia/Shanghai",
                {
                    "D": decision_date,
                    "now": local.isoformat(timespec="seconds"),
                    "freeze_after_local_time": freeze_after_local_time,
                },
            )
        return None
    except ContractError as exc:
        return SourceIssue(
            "FREEZE_GATE_INVALID",
            "error",
            "aggregate",
            str(exc),
            {
                "D": decision_date,
                "freeze_after_local_time": freeze_after_local_time,
            },
        )


def source_snapshot_changes(
    frozen_items: list[dict[str, Any]],
    live_items: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compare immutable input bytes and their pinned repository commits by source."""
    frozen = {
        str(item.get("source_id")): item
        for item in frozen_items
        if item.get("source_id")
    }
    live = {
        str(item.get("source_id")): item
        for item in live_items
        if item.get("source_id")
    }
    return {
        source_id: {
            "frozen_content_sha256": frozen.get(source_id, {}).get("content_sha256"),
            "live_content_sha256": live.get(source_id, {}).get("content_sha256"),
            "frozen_repository_commit_sha": frozen.get(source_id, {}).get(
                "repository_commit_sha"
            ),
            "live_repository_commit_sha": live.get(source_id, {}).get(
                "repository_commit_sha"
            ),
        }
        for source_id in sorted(set(frozen) | set(live))
        if (
            frozen.get(source_id, {}).get("content_sha256")
            != live.get(source_id, {}).get("content_sha256")
            or frozen.get(source_id, {}).get("repository_commit_sha")
            != live.get(source_id, {}).get("repository_commit_sha")
        )
    }


def strict_intersection(tables: list[SourceTable]) -> list[Candidate]:
    by_id = {table.source_id: table for table in tables}
    if set(by_id) != set(SOURCE_IDS):
        raise ContractError("strict intersection requires all three sources")
    common = set.intersection(*(by_id[source_id].codes() for source_id in SOURCE_IDS))
    row_maps = {
        source_id: {row.ts_code: row for row in by_id[source_id].rows}
        for source_id in SOURCE_IDS
    }
    candidates: list[Candidate] = []
    for code in sorted(common):
        source_rows = {source_id: row_maps[source_id][code] for source_id in SOURCE_IDS}
        name = next((row.name for row in source_rows.values() if row.name), code)
        candidates.append(
            Candidate(
                ts_code=code,
                name=name,
                source_ranks={source_id: row.rank for source_id, row in source_rows.items()},
                source_values={source_id: row.values for source_id, row in source_rows.items()},
            )
        )
    return candidates


def numeric_source_value(candidate: Candidate, source_id: str, *keys: str) -> float | None:
    values = candidate.source_values.get(source_id, {})
    return _number(_first_value(values, keys))

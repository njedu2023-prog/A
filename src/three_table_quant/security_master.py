from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from .domain import ContractError, normalize_ts_code


SECURITY_MASTER_SCHEMA = "point_in_time_security_master_v1"
DEFAULT_SECURITY_MASTER_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "security_master.v1.json"
)
SECURITY_MASTER_FIELDS = (
    "is_suspended",
    "is_st",
    "is_delisting_period",
    "trading_rules_verified",
    "board",
    "price_limit_pct",
    "price_tick",
)
ALLOWED_BOARDS = frozenset({"MAIN", "STAR", "CHINEXT", "BSE"})


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _date(value: Any, field_name: str) -> date:
    raw = str(value or "").strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ContractError(f"{field_name} must be YYYYMMDD")
    try:
        parsed = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ContractError(f"{field_name} is not a valid date") from exc
    if parsed.strftime("%Y%m%d") != raw:
        raise ContractError(f"{field_name} must be canonical YYYYMMDD")
    return parsed


def _timestamp(value: Any, field_name: str) -> datetime:
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field_name} requires a timezone offset")
    return parsed


def _text(value: Any, field_name: str) -> str:
    parsed = str(value or "").strip()
    if not parsed:
        raise ContractError(f"{field_name} must be non-empty")
    return parsed


def _positive_number(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be a positive finite number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ContractError(f"{field_name} must be a positive finite number")
    return parsed


@dataclass(frozen=True, slots=True)
class SecurityMasterRecord:
    record_id: str
    ts_code: str
    effective_from: date
    effective_to: date | None
    known_at: datetime
    fetched_at: datetime
    provider: str
    dataset_version: str
    revision_id: str
    source_uri: str
    facts: Mapping[str, Any]
    missing_reasons: Mapping[str, str]
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SecurityMasterResolution:
    ts_code: str
    decision_date: str
    decision_asof: str
    values: Mapping[str, Any]
    missing_reasons: Mapping[str, str]
    provider: str
    dataset_version: str
    known_at: str
    fetched_at: str
    content_sha256: str
    revision_id: str | None
    source_uri: str | None
    record_id: str | None

    def value(self, field_name: str) -> Any:
        if field_name not in SECURITY_MASTER_FIELDS:
            raise ContractError(f"unsupported security-master field: {field_name}")
        return self.values[field_name]

    def missing_reason(self, field_name: str) -> str | None:
        if field_name not in SECURITY_MASTER_FIELDS:
            raise ContractError(f"unsupported security-master field: {field_name}")
        return self.missing_reasons.get(field_name)


def _parse_record(raw: Any, index: int) -> SecurityMasterRecord:
    if not isinstance(raw, Mapping):
        raise ContractError(f"security-master record {index} must be an object")
    record_id = _text(raw.get("record_id"), f"records[{index}].record_id")
    ts_code = normalize_ts_code(raw.get("ts_code"))
    effective_from = _date(
        raw.get("effective_from"), f"records[{index}].effective_from"
    )
    raw_effective_to = raw.get("effective_to")
    effective_to = (
        None
        if raw_effective_to in (None, "")
        else _date(raw_effective_to, f"records[{index}].effective_to")
    )
    if effective_to is not None and effective_to < effective_from:
        raise ContractError("security-master effective_to precedes effective_from")
    known_at = _timestamp(raw.get("known_at"), f"records[{index}].known_at")
    fetched_at = _timestamp(raw.get("fetched_at"), f"records[{index}].fetched_at")
    if fetched_at < known_at:
        raise ContractError("security-master fetched_at cannot precede known_at")
    provider = _text(raw.get("provider"), f"records[{index}].provider")
    dataset_version = _text(
        raw.get("dataset_version"), f"records[{index}].dataset_version"
    )
    revision_id = _text(raw.get("revision_id"), f"records[{index}].revision_id")
    source_uri = _text(raw.get("source_uri"), f"records[{index}].source_uri")
    if not source_uri.startswith("https://"):
        raise ContractError("security-master source_uri must be HTTPS")

    raw_facts = raw.get("facts")
    if not isinstance(raw_facts, Mapping) or set(raw_facts) != set(
        SECURITY_MASTER_FIELDS
    ):
        raise ContractError("security-master facts must contain the exact field contract")
    raw_reasons = raw.get("missing_reasons", {})
    if not isinstance(raw_reasons, Mapping):
        raise ContractError("security-master missing_reasons must be an object")
    facts: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for field_name in SECURITY_MASTER_FIELDS:
        value = raw_facts[field_name]
        reason = raw_reasons.get(field_name)
        if value is None:
            reasons[field_name] = _text(
                reason,
                f"records[{index}].missing_reasons.{field_name}",
            )
            facts[field_name] = None
            continue
        if reason is not None:
            raise ContractError(
                f"known security-master field {field_name} cannot have a missing reason"
            )
        if field_name in {
            "is_suspended",
            "is_st",
            "is_delisting_period",
            "trading_rules_verified",
        }:
            if type(value) is not bool:
                raise ContractError(f"security-master {field_name} must be bool or null")
            facts[field_name] = value
        elif field_name == "board":
            board = str(value).strip().upper()
            if board not in ALLOWED_BOARDS:
                raise ContractError(f"unsupported security-master board: {value!r}")
            facts[field_name] = board
        elif field_name in {"price_limit_pct", "price_tick"}:
            facts[field_name] = _positive_number(
                value,
                f"records[{index}].facts.{field_name}",
            )

    if facts["trading_rules_verified"] is True and any(
        facts[field_name] is None
        for field_name in ("board", "price_limit_pct", "price_tick")
    ):
        raise ContractError(
            "trading_rules_verified=true requires board, price_limit_pct and price_tick"
        )

    canonical = {
        "record_id": record_id,
        "ts_code": ts_code,
        "effective_from": effective_from.strftime("%Y%m%d"),
        "effective_to": (
            effective_to.strftime("%Y%m%d") if effective_to is not None else None
        ),
        "known_at": known_at.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "provider": provider,
        "dataset_version": dataset_version,
        "revision_id": revision_id,
        "source_uri": source_uri,
        "facts": facts,
        "missing_reasons": reasons,
    }
    return SecurityMasterRecord(
        record_id=record_id,
        ts_code=ts_code,
        effective_from=effective_from,
        effective_to=effective_to,
        known_at=known_at,
        fetched_at=fetched_at,
        provider=provider,
        dataset_version=dataset_version,
        revision_id=revision_id,
        source_uri=source_uri,
        facts=MappingProxyType(facts),
        missing_reasons=MappingProxyType(reasons),
        content_sha256=_digest(canonical),
    )


class PointInTimeSecurityMaster:
    """Versioned local contract; it never infers a missing security state."""

    def __init__(
        self,
        records: tuple[SecurityMasterRecord, ...],
        *,
        provider: str,
        dataset_version: str,
        generated_at: str,
        content_sha256: str,
    ) -> None:
        if not isinstance(records, tuple) or not all(
            isinstance(item, SecurityMasterRecord) for item in records
        ):
            raise ContractError("security-master records must be an immutable tuple")
        digest = str(content_sha256 or "").strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ContractError("security-master content_sha256 must be lowercase SHA-256")
        self.records = records
        self.provider = _text(provider, "security-master provider")
        self.dataset_version = _text(
            dataset_version,
            "security-master dataset_version",
        )
        self.generated_at = _timestamp(
            generated_at,
            "security-master generated_at",
        ).isoformat()
        self.content_sha256 = digest

    @classmethod
    def from_file(
        cls,
        path: str | Path = DEFAULT_SECURITY_MASTER_PATH,
    ) -> PointInTimeSecurityMaster:
        master_path = Path(path)
        try:
            raw_bytes = master_path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"cannot load point-in-time security master: {exc}") from exc
        return cls.from_payload(
            payload,
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )

    @classmethod
    def from_payload(
        cls,
        payload: Any,
        *,
        content_sha256: str | None = None,
    ) -> PointInTimeSecurityMaster:
        if not isinstance(payload, Mapping):
            raise ContractError("point-in-time security master must be an object")
        if payload.get("schema_version") != SECURITY_MASTER_SCHEMA:
            raise ContractError("unsupported point-in-time security-master schema")
        provider = _text(payload.get("provider"), "security-master provider")
        dataset_version = _text(
            payload.get("dataset_version"), "security-master dataset_version"
        )
        generated_at = _timestamp(
            payload.get("generated_at"),
            "security-master generated_at",
        )
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ContractError("security-master records must be a list")
        records = tuple(_parse_record(item, index) for index, item in enumerate(raw_records))
        record_ids = [item.record_id for item in records]
        if len(record_ids) != len(set(record_ids)):
            raise ContractError("duplicate security-master record_id")
        return cls(
            records,
            provider=provider,
            dataset_version=dataset_version,
            generated_at=generated_at.isoformat(),
            content_sha256=(content_sha256 or _digest(payload)),
        )

    def resolve(
        self,
        ts_code: str,
        *,
        decision_date: str,
        decision_asof: str,
    ) -> SecurityMasterResolution:
        code = normalize_ts_code(ts_code)
        day = _date(decision_date, "security-master decision_date")
        asof = _timestamp(decision_asof, "security-master decision_asof")
        if asof.astimezone(ZoneInfo("Asia/Shanghai")).date() != day:
            raise ContractError(
                "security-master decision_asof must fall on decision_date "
                "in Asia/Shanghai"
            )
        generated_at = _timestamp(
            self.generated_at,
            "security-master generated_at",
        )
        if generated_at > asof:
            raise ContractError(
                "security-master generated_at is later than decision_asof; "
                "a future bootstrap cannot prove D-as-of missingness"
            )
        eligible = [
            item
            for item in self.records
            if item.ts_code == code
            and item.effective_from <= day
            and (item.effective_to is None or day <= item.effective_to)
            and item.known_at <= asof
            and item.fetched_at <= asof
        ]
        if not eligible:
            values = {field_name: None for field_name in SECURITY_MASTER_FIELDS}
            reasons = {
                field_name: "NO_POINT_IN_TIME_SECURITY_RECORD"
                for field_name in SECURITY_MASTER_FIELDS
            }
            return SecurityMasterResolution(
                ts_code=code,
                decision_date=day.strftime("%Y%m%d"),
                decision_asof=asof.isoformat(),
                values=MappingProxyType(values),
                missing_reasons=MappingProxyType(reasons),
                provider=self.provider,
                dataset_version=self.dataset_version,
                known_at=generated_at.isoformat(),
                fetched_at=generated_at.isoformat(),
                content_sha256=self.content_sha256,
                revision_id=None,
                source_uri=None,
                record_id=None,
            )
        eligible.sort(
            key=lambda item: (
                item.known_at,
                item.fetched_at,
                item.effective_from,
                item.record_id,
            )
        )
        selected = eligible[-1]
        if len(eligible) > 1:
            previous = eligible[-2]
            if (
                previous.known_at == selected.known_at
                and previous.fetched_at == selected.fetched_at
            ):
                raise ContractError("ambiguous point-in-time security-master records")
        return SecurityMasterResolution(
            ts_code=code,
            decision_date=day.strftime("%Y%m%d"),
            decision_asof=asof.isoformat(),
            values=selected.facts,
            missing_reasons=selected.missing_reasons,
            provider=selected.provider,
            dataset_version=selected.dataset_version,
            known_at=selected.known_at.isoformat(),
            fetched_at=selected.fetched_at.isoformat(),
            content_sha256=selected.content_sha256,
            revision_id=selected.revision_id,
            source_uri=selected.source_uri,
            record_id=selected.record_id,
        )


__all__ = [
    "ALLOWED_BOARDS",
    "DEFAULT_SECURITY_MASTER_PATH",
    "PointInTimeSecurityMaster",
    "SECURITY_MASTER_FIELDS",
    "SECURITY_MASTER_SCHEMA",
    "SecurityMasterRecord",
    "SecurityMasterResolution",
]

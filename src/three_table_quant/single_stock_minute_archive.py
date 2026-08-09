from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import tempfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .domain import ContractError, normalize_date, normalize_ts_code
from .limit_lifecycle import EXPECTED_SESSION_MINUTES
from .market import Bar


MINUTE_ARCHIVE_SCHEMA = "single_stock_minute_archive_v1"
MINUTE_BAR_SCHEMA = "canonical_ohlcv_minute_v1"
MINUTE_ARCHIVE_REFERENCE_SCHEMA = "single_stock_minute_archive_ref_v1"
DEFAULT_MINUTE_ARCHIVE_ROOT = (
    Path(__file__).resolve().parents[2] / "data" / "single_stock_minute_archive"
)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("minute archive must be canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _deterministic_gzip(value: bytes) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=buffer,
        compresslevel=9,
        mtime=0,
    ) as handle:
        handle.write(value)
    return buffer.getvalue()


def _gunzip(value: bytes) -> bytes:
    if len(value) < 10 or value[:2] != b"\x1f\x8b" or value[4:8] != b"\x00\x00\x00\x00":
        raise ContractError("minute artifact gzip must use deterministic mtime=0")
    try:
        return gzip.decompress(value)
    except (gzip.BadGzipFile, EOFError, OSError, zlib.error) as exc:
        raise ContractError("minute artifact is not valid gzip") from exc


def _timestamp(value: Any, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ContractError(f"{field_name} must be non-empty")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field_name} must include a timezone offset")
    return parsed


def _finite_number(
    value: Any,
    field_name: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{field_name} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{field_name} must be numeric") from exc
    if not math.isfinite(parsed):
        raise ContractError(f"{field_name} must be finite")
    if positive and parsed <= 0:
        raise ContractError(f"{field_name} must be positive")
    if nonnegative and parsed < 0:
        raise ContractError(f"{field_name} must be nonnegative")
    return parsed


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ContractError(f"{field_name} must be non-empty")
    return text


def _minute_time(value: Any, field_name: str) -> str:
    text = _required_text(value, field_name)
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ContractError(f"{field_name} must be canonical HH:MM") from exc
    if parsed.strftime("%H:%M") != text:
        raise ContractError(f"{field_name} must be canonical HH:MM")
    return text


def canonicalize_minute_bars(
    bars: Sequence[Any],
    *,
    decision_date: str,
) -> list[dict[str, Any]]:
    """Return strict, deterministic D-day OHLCV evidence.

    The archive records what the provider returned, including incomplete
    sessions.  Incompleteness is metadata, never synthetic zero-filled bars.
    """

    day = normalize_date(decision_date, "minute archive decision_date")
    if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence):
        raise ContractError("minute archive bars must be a sequence")

    canonical: list[dict[str, Any]] = []
    observed_times: set[str] = set()
    for index, bar in enumerate(bars):
        prefix = f"minute archive bars[{index}]"
        bar_day = normalize_date(getattr(bar, "date", None), f"{prefix}.date")
        if bar_day != day:
            raise ContractError(f"{prefix}.date must equal decision_date")
        time_value = _minute_time(getattr(bar, "time", None), f"{prefix}.time")
        if time_value in observed_times:
            raise ContractError(f"minute archive contains duplicate minute {time_value}")
        observed_times.add(time_value)

        open_price = _finite_number(
            getattr(bar, "open", None), f"{prefix}.open", positive=True
        )
        close_price = _finite_number(
            getattr(bar, "close", None), f"{prefix}.close", positive=True
        )
        high = _finite_number(
            getattr(bar, "high", None), f"{prefix}.high", positive=True
        )
        low = _finite_number(
            getattr(bar, "low", None), f"{prefix}.low", positive=True
        )
        volume = _finite_number(
            getattr(bar, "volume", None), f"{prefix}.volume", nonnegative=True
        )
        amount = _finite_number(
            getattr(bar, "amount", None), f"{prefix}.amount", nonnegative=True
        )
        if high < low or high < max(open_price, close_price) or low > min(
            open_price, close_price
        ):
            raise ContractError(f"{prefix} contains inconsistent OHLC prices")

        raw_source_time = getattr(bar, "source_time", None)
        source_time = (
            None
            if raw_source_time in (None, "")
            else _minute_time(raw_source_time, f"{prefix}.source_time")
        )
        canonical.append(
            {
                "date": bar_day,
                "time": time_value,
                "open": open_price,
                "close": close_price,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": amount,
                "volume_unit": _required_text(
                    getattr(bar, "volume_unit", None), f"{prefix}.volume_unit"
                ).upper(),
                "price_tick": _finite_number(
                    getattr(bar, "price_tick", None),
                    f"{prefix}.price_tick",
                    positive=True,
                ),
                "source_time": source_time,
                "time_semantics": _required_text(
                    getattr(bar, "time_semantics", None),
                    f"{prefix}.time_semantics",
                ).upper(),
                "provider": _required_text(
                    getattr(bar, "provider", None), f"{prefix}.provider"
                ).upper(),
                "price_adjustment": _required_text(
                    getattr(bar, "price_adjustment", None),
                    f"{prefix}.price_adjustment",
                ).upper(),
            }
        )
    canonical.sort(key=lambda item: (item["date"], item["time"]))
    return canonical


def minute_bars_content_sha256(canonical_bars: Sequence[Mapping[str, Any]]) -> str:
    return _sha256_bytes(_canonical_json_bytes(list(canonical_bars)))


@dataclass(frozen=True)
class MinuteArchiveReference:
    decision_date: str
    ts_code: str
    relative_path: str
    artifact_sha256: str
    bars_content_sha256: str
    bar_count: int
    full_session: bool
    providers: tuple[str, ...]
    captured_at: str
    content_encoding: str = "gzip"
    schema_version: str = MINUTE_ARCHIVE_REFERENCE_SCHEMA
    artifact_schema_version: str = MINUTE_ARCHIVE_SCHEMA
    bar_schema_version: str = MINUTE_BAR_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_date",
            normalize_date(self.decision_date, "minute artifact reference decision_date"),
        )
        object.__setattr__(self, "ts_code", normalize_ts_code(self.ts_code))
        if self.schema_version != MINUTE_ARCHIVE_REFERENCE_SCHEMA:
            raise ContractError("unsupported minute artifact reference schema")
        if self.artifact_schema_version != MINUTE_ARCHIVE_SCHEMA:
            raise ContractError("unsupported minute artifact schema")
        if self.bar_schema_version != MINUTE_BAR_SCHEMA:
            raise ContractError("unsupported canonical minute bar schema")
        if self.content_encoding != "gzip":
            raise ContractError("minute artifact content_encoding must be gzip")
        relative = PurePosixPath(str(self.relative_path or ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ContractError("minute artifact relative_path must be contained")
        object.__setattr__(self, "relative_path", relative.as_posix())
        for field_name in ("artifact_sha256", "bars_content_sha256"):
            digest = str(getattr(self, field_name) or "").strip().lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ContractError(f"{field_name} must be lowercase SHA-256")
            object.__setattr__(self, field_name, digest)
        if type(self.bar_count) is not int or self.bar_count < 0:
            raise ContractError("minute artifact bar_count must be a nonnegative integer")
        if type(self.full_session) is not bool:
            raise ContractError("minute artifact full_session must be boolean")
        if self.full_session and self.bar_count != len(EXPECTED_SESSION_MINUTES):
            raise ContractError("full minute artifact must contain 240 bars")
        normalized_providers = tuple(
            sorted(
                {
                    _required_text(value, "minute artifact provider").upper()
                    for value in self.providers
                }
            )
        )
        if self.bar_count and not normalized_providers:
            raise ContractError("minute artifact providers are required when bars exist")
        object.__setattr__(self, "providers", normalized_providers)
        _timestamp(self.captured_at, "minute artifact captured_at")
        expected_path = _artifact_relative_path(
            self.decision_date,
            self.ts_code,
            self.artifact_sha256,
        )
        if self.relative_path != expected_path:
            raise ContractError(
                "minute artifact relative_path does not match D/code/content SHA"
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MinuteArchiveReference:
        if not isinstance(payload, Mapping):
            raise ContractError("minute artifact reference must be an object")
        expected = {
            "schema_version",
            "artifact_schema_version",
            "bar_schema_version",
            "decision_date",
            "ts_code",
            "relative_path",
            "artifact_sha256",
            "bars_content_sha256",
            "bar_count",
            "full_session",
            "providers",
            "captured_at",
            "content_encoding",
        }
        if set(payload) != expected:
            raise ContractError("minute artifact reference fields do not match schema")
        providers = payload.get("providers")
        if not isinstance(providers, (list, tuple)):
            raise ContractError("minute artifact providers must be a list")
        return cls(
            schema_version=str(payload["schema_version"]),
            artifact_schema_version=str(payload["artifact_schema_version"]),
            bar_schema_version=str(payload["bar_schema_version"]),
            decision_date=str(payload["decision_date"]),
            ts_code=str(payload["ts_code"]),
            relative_path=str(payload["relative_path"]),
            artifact_sha256=str(payload["artifact_sha256"]),
            bars_content_sha256=str(payload["bars_content_sha256"]),
            bar_count=payload["bar_count"],
            full_session=payload["full_session"],
            providers=tuple(providers),
            captured_at=str(payload["captured_at"]),
            content_encoding=str(payload["content_encoding"]),
        )

    def validate_for(
        self,
        *,
        ts_code: str,
        decision_date: str,
        decision_asof: str | None = None,
        bars_content_sha256: str | None = None,
        bar_count: int | None = None,
        full_session: bool | None = None,
    ) -> None:
        if self.ts_code != normalize_ts_code(ts_code):
            raise ContractError("minute artifact reference ts_code mismatch")
        if self.decision_date != normalize_date(
            decision_date, "minute artifact expected decision_date"
        ):
            raise ContractError("minute artifact reference decision_date mismatch")
        if decision_asof is not None and _timestamp(
            self.captured_at, "minute artifact captured_at"
        ).astimezone(timezone.utc) > _timestamp(
            decision_asof, "minute artifact decision_asof"
        ).astimezone(timezone.utc):
            raise ContractError("minute artifact was captured after decision_asof")
        if bars_content_sha256 is not None and self.bars_content_sha256 != str(
            bars_content_sha256
        ).lower():
            raise ContractError("minute artifact bars content SHA mismatch")
        if bar_count is not None and self.bar_count != bar_count:
            raise ContractError("minute artifact bar_count mismatch")
        if full_session is not None and self.full_session is not full_session:
            raise ContractError("minute artifact full_session mismatch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_schema_version": self.artifact_schema_version,
            "bar_schema_version": self.bar_schema_version,
            "decision_date": self.decision_date,
            "ts_code": self.ts_code,
            "relative_path": self.relative_path,
            "artifact_sha256": self.artifact_sha256,
            "bars_content_sha256": self.bars_content_sha256,
            "bar_count": self.bar_count,
            "full_session": self.full_session,
            "providers": list(self.providers),
            "captured_at": self.captured_at,
            "content_encoding": self.content_encoding,
        }


def _artifact_relative_path(
    decision_date: str,
    ts_code: str,
    artifact_sha256: str,
) -> str:
    return PurePosixPath(
        decision_date[:4],
        decision_date,
        ts_code,
        f"{artifact_sha256}.json.gz",
    ).as_posix()


def archive_minute_bars(
    root: str | Path,
    ts_code: str,
    decision_date: str,
    bars: Sequence[Any],
    captured_at: str,
) -> MinuteArchiveReference:
    """Atomically append one immutable, content-addressed minute artifact."""

    day = normalize_date(decision_date, "minute archive decision_date")
    code = normalize_ts_code(ts_code)
    _timestamp(captured_at, "minute archive captured_at")
    canonical_bars = canonicalize_minute_bars(bars, decision_date=day)
    observed_times = tuple(item["time"] for item in canonical_bars)
    full_session = observed_times == EXPECTED_SESSION_MINUTES
    providers = tuple(sorted({item["provider"] for item in canonical_bars}))
    bars_sha = minute_bars_content_sha256(canonical_bars)
    payload = {
        "schema_version": MINUTE_ARCHIVE_SCHEMA,
        "bar_schema_version": MINUTE_BAR_SCHEMA,
        "decision_date": day,
        "ts_code": code,
        "expected_bar_count": len(EXPECTED_SESSION_MINUTES),
        "bar_count": len(canonical_bars),
        "full_session": full_session,
        "providers": list(providers),
        "bars_content_sha256": bars_sha,
        "bars": canonical_bars,
    }
    artifact_bytes = _canonical_json_bytes(payload)
    artifact_sha = _sha256_bytes(artifact_bytes)
    compressed_bytes = _deterministic_gzip(artifact_bytes)
    relative_path = _artifact_relative_path(day, code, artifact_sha)
    root_path = Path(root)
    artifact_path = root_path / Path(relative_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=artifact_path.parent,
            prefix=".minute-artifact-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(compressed_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link publishes the fully-written inode atomically and, in
            # contrast with replace/rename, can never overwrite an old artifact.
            os.link(temporary_path, artifact_path)
        except FileExistsError:
            existing = artifact_path.read_bytes()
            existing_content = _gunzip(existing)
            if (
                _sha256_bytes(existing_content) != artifact_sha
                or existing_content != artifact_bytes
            ):
                raise ContractError(
                    "content-addressed minute artifact exists with different bytes"
                )
        else:
            directory_descriptor = os.open(artifact_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    return MinuteArchiveReference(
        decision_date=day,
        ts_code=code,
        relative_path=relative_path,
        artifact_sha256=artifact_sha,
        bars_content_sha256=bars_sha,
        bar_count=len(canonical_bars),
        full_session=full_session,
        providers=providers,
        captured_at=captured_at,
    )


def load_minute_artifact(
    root: str | Path,
    reference: MinuteArchiveReference | Mapping[str, Any],
) -> dict[str, Any]:
    """Load and fully verify an archived artifact before replay."""

    parsed = (
        reference
        if isinstance(reference, MinuteArchiveReference)
        else MinuteArchiveReference.from_dict(reference)
    )
    root_path = Path(root).resolve()
    artifact_path = (root_path / Path(parsed.relative_path)).resolve()
    try:
        artifact_path.relative_to(root_path)
    except ValueError as exc:
        raise ContractError("minute artifact path escapes archive root") from exc
    try:
        compressed_bytes = artifact_path.read_bytes()
    except OSError as exc:
        raise ContractError(f"cannot read minute artifact: {exc}") from exc
    artifact_bytes = _gunzip(compressed_bytes)
    if _sha256_bytes(artifact_bytes) != parsed.artifact_sha256:
        raise ContractError("minute artifact SHA-256 mismatch")
    try:
        payload = json.loads(artifact_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("minute artifact is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("minute artifact root must be an object")
    if _canonical_json_bytes(payload) != artifact_bytes:
        raise ContractError("minute artifact bytes are not canonical JSON")
    expected_fields = {
        "schema_version",
        "bar_schema_version",
        "decision_date",
        "ts_code",
        "expected_bar_count",
        "bar_count",
        "full_session",
        "providers",
        "bars_content_sha256",
        "bars",
    }
    if set(payload) != expected_fields:
        raise ContractError("minute artifact fields do not match schema")
    if payload.get("schema_version") != MINUTE_ARCHIVE_SCHEMA:
        raise ContractError("minute artifact schema mismatch")
    if payload.get("bar_schema_version") != MINUTE_BAR_SCHEMA:
        raise ContractError("minute artifact bar schema mismatch")
    if payload.get("expected_bar_count") != len(EXPECTED_SESSION_MINUTES):
        raise ContractError("minute artifact expected_bar_count mismatch")
    bars_payload = payload.get("bars")
    if not isinstance(bars_payload, list):
        raise ContractError("minute artifact bars must be a list")
    replayed = [
        Bar(
            date=item.get("date"),
            time=item.get("time"),
            open=item.get("open"),
            close=item.get("close"),
            high=item.get("high"),
            low=item.get("low"),
            volume=item.get("volume"),
            amount=item.get("amount"),
            volume_unit=item.get("volume_unit"),
            price_tick=item.get("price_tick"),
            source_time=item.get("source_time"),
            time_semantics=item.get("time_semantics"),
            provider=item.get("provider"),
            price_adjustment=item.get("price_adjustment"),
        )
        for item in bars_payload
        if isinstance(item, Mapping)
    ]
    if len(replayed) != len(bars_payload):
        raise ContractError("minute artifact contains a non-object bar")
    canonical_bars = canonicalize_minute_bars(
        replayed,
        decision_date=str(payload.get("decision_date")),
    )
    if canonical_bars != bars_payload:
        raise ContractError("minute artifact bars are not canonical")
    observed_times = tuple(item["time"] for item in canonical_bars)
    expected_full_session = observed_times == EXPECTED_SESSION_MINUTES
    expected_providers = sorted({item["provider"] for item in canonical_bars})
    expected_bars_sha = minute_bars_content_sha256(canonical_bars)
    if payload.get("bar_count") != len(canonical_bars):
        raise ContractError("minute artifact bar_count mismatch")
    if payload.get("full_session") is not expected_full_session:
        raise ContractError("minute artifact full_session mismatch")
    if payload.get("providers") != expected_providers:
        raise ContractError("minute artifact providers mismatch")
    if payload.get("bars_content_sha256") != expected_bars_sha:
        raise ContractError("minute artifact bars content SHA mismatch")
    parsed.validate_for(
        ts_code=str(payload.get("ts_code")),
        decision_date=str(payload.get("decision_date")),
        bars_content_sha256=expected_bars_sha,
        bar_count=len(canonical_bars),
        full_session=expected_full_session,
    )
    if list(parsed.providers) != expected_providers:
        raise ContractError("minute artifact reference providers mismatch")
    return payload


def replay_minute_bars(
    root: str | Path,
    reference: MinuteArchiveReference | Mapping[str, Any],
) -> list[Bar]:
    payload = load_minute_artifact(root, reference)
    return [
        Bar(
            date=item["date"],
            time=item["time"],
            open=item["open"],
            close=item["close"],
            high=item["high"],
            low=item["low"],
            volume=item["volume"],
            amount=item["amount"],
            volume_unit=item["volume_unit"],
            price_tick=item["price_tick"],
            source_time=item["source_time"],
            time_semantics=item["time_semantics"],
            provider=item["provider"],
            price_adjustment=item["price_adjustment"],
        )
        for item in payload["bars"]
    ]


__all__ = [
    "DEFAULT_MINUTE_ARCHIVE_ROOT",
    "MINUTE_ARCHIVE_REFERENCE_SCHEMA",
    "MINUTE_ARCHIVE_SCHEMA",
    "MINUTE_BAR_SCHEMA",
    "MinuteArchiveReference",
    "archive_minute_bars",
    "canonicalize_minute_bars",
    "load_minute_artifact",
    "minute_bars_content_sha256",
    "replay_minute_bars",
]

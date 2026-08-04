from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .domain import ContractError, normalize_date


@dataclass(frozen=True)
class LockboxPartition:
    development_rows: tuple[dict[str, Any], ...]
    lockbox_rows: tuple[dict[str, Any], ...]
    lockbox_dates: tuple[str, ...]


@dataclass(frozen=True)
class WalkForwardFold:
    fold: int
    validation_start: str
    validation_end: str
    embargo_cutoff: str
    train_dates: tuple[str, ...]
    validation_dates: tuple[str, ...]
    train_rows: tuple[dict[str, Any], ...]
    validation_rows: tuple[dict[str, Any], ...]


def _row_date(row: Mapping[str, Any], field: str) -> str:
    return normalize_date(row.get(field), field)


def _label_end(row: Mapping[str, Any]) -> str | None:
    labels = row.get("labels")
    value = labels.get("label_end_date") if isinstance(labels, Mapping) else row.get("label_end_date")
    if value is None or str(value).strip() == "":
        return None
    return normalize_date(value, "label_end_date")


def _is_mature(row: Mapping[str, Any]) -> bool:
    labels = row.get("labels")
    value = labels.get("is_mature") if isinstance(labels, Mapping) else row.get("is_mature")
    return value is True


def _sort_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    copied = [dict(item) for item in rows]
    identities: set[str] = set()
    for index, item in enumerate(copied):
        row_id = str(item.get("row_id") or f"row:{index}")
        if row_id in identities:
            raise ContractError(f"duplicate walk-forward row_id: {row_id}")
        identities.add(row_id)
        _row_date(item, "decision_date")
        end = _label_end(item)
        if _is_mature(item) and end is None:
            raise ContractError("mature walk-forward row requires label_end_date")
    return sorted(
        copied,
        key=lambda item: (
            _row_date(item, "decision_date"),
            int(item.get("rank") or 0),
            str(item.get("ts_code") or ""),
            str(item.get("row_id") or ""),
        ),
    )


def partition_lockbox(
    rows: Iterable[Mapping[str, Any]],
    lockbox_days: int = 126,
) -> LockboxPartition:
    if (
        isinstance(lockbox_days, bool)
        or not isinstance(lockbox_days, int)
        or lockbox_days < 0
    ):
        raise ContractError("lockbox_days must be a nonnegative integer")
    ordered = _sort_rows(rows)
    dates = sorted({_row_date(item, "decision_date") for item in ordered})
    lockbox_dates = tuple(dates[-lockbox_days:]) if lockbox_days else ()
    locked = set(lockbox_dates)
    return LockboxPartition(
        development_rows=tuple(
            item for item in ordered if _row_date(item, "decision_date") not in locked
        ),
        lockbox_rows=tuple(
            item for item in ordered if _row_date(item, "decision_date") in locked
        ),
        lockbox_dates=lockbox_dates,
    )


def _trading_day_index(trading_days: Sequence[str] | None) -> tuple[list[str], dict[str, int]]:
    if trading_days is None:
        return [], {}
    normalized = [normalize_date(item, "trading_day") for item in trading_days]
    if normalized != sorted(set(normalized)):
        raise ContractError("trading_days must be unique and strictly increasing")
    return normalized, {value: index for index, value in enumerate(normalized)}


def _embargo_cutoff(
    validation_start: str,
    embargo_days: int,
    trading_days: Sequence[str] | None,
) -> str:
    if isinstance(embargo_days, bool) or embargo_days < 0:
        raise ContractError("embargo_days must be a nonnegative integer")
    if embargo_days == 0:
        return validation_start
    days, positions = _trading_day_index(trading_days)
    if validation_start not in positions:
        raise ContractError("validation_start is absent from trading_days")
    cutoff_index = positions[validation_start] - embargo_days
    if cutoff_index < 0:
        raise ContractError("trading_days do not cover the requested embargo")
    return days[cutoff_index]


def expanding_walk_forward(
    rows: Iterable[Mapping[str, Any]],
    *,
    min_train_days: int = 1,
    validation_days: int = 1,
    embargo_days: int = 0,
    lockbox_days: int = 0,
    trading_days: Sequence[str] | None = None,
) -> tuple[WalkForwardFold, ...]:
    """Create deterministic expanding folds grouped by the entire D-day cohort.

    Only mature rows enter a fold.  Training labels must end strictly before
    the validation boundary, with an optional pre-validation gap counted in
    supplied exchange trading days.
    """

    for name, value, allow_zero in (
        ("min_train_days", min_train_days, False),
        ("validation_days", validation_days, False),
        ("lockbox_days", lockbox_days, True),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < int(not allow_zero):
            qualifier = "nonnegative" if allow_zero else "positive"
            raise ContractError(f"{name} must be a {qualifier} integer")

    partition = partition_lockbox(rows, lockbox_days)
    mature = [
        item
        for item in partition.development_rows
        if _is_mature(item) and _label_end(item) is not None
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in mature:
        groups.setdefault(_row_date(item, "decision_date"), []).append(item)
    dates = sorted(groups)
    folds: list[WalkForwardFold] = []
    fold_number = 1

    for start_index in range(min_train_days, len(dates), validation_days):
        validation_dates = tuple(dates[start_index : start_index + validation_days])
        if not validation_dates:
            continue
        validation_start = validation_dates[0]
        cutoff = _embargo_cutoff(validation_start, embargo_days, trading_days)
        training = [
            item
            for date in dates[:start_index]
            for item in groups[date]
            if str(_label_end(item)) < cutoff
        ]
        train_dates = tuple(
            sorted({_row_date(item, "decision_date") for item in training})
        )
        if len(train_dates) < min_train_days:
            continue
        validation = [
            item for date in validation_dates for item in groups[date]
        ]
        folds.append(
            WalkForwardFold(
                fold=fold_number,
                validation_start=validation_start,
                validation_end=validation_dates[-1],
                embargo_cutoff=cutoff,
                train_dates=train_dates,
                validation_dates=validation_dates,
                train_rows=tuple(training),
                validation_rows=tuple(validation),
            )
        )
        fold_number += 1
    return tuple(folds)

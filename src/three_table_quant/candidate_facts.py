from __future__ import annotations

import math
import re
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .domain import ContractError


DECISION_SOURCE = "decision_table"
PREMIUM_SOURCE = "premium_top10"
A_TOP10_SOURCE = "a_top10"
D_CLOSE_ABS_TOL = 0.005
STAGE_TRANSITION_RE = re.compile(r"^([1-9][0-9]*)→([1-9][0-9]*)$")


def _candidate_sources(candidate: Any) -> Mapping[str, Any]:
    if isinstance(candidate, Mapping):
        values = candidate.get("source_values")
    else:
        values = getattr(candidate, "source_values", None)
    return values if isinstance(values, Mapping) else {}


def _source(values: Mapping[str, Any], source_id: str) -> Mapping[str, Any]:
    row = values.get(source_id)
    return row if isinstance(row, Mapping) else {}


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _stage(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = STAGE_TRANSITION_RE.fullmatch(text)
    if match is None or int(match.group(2)) != int(match.group(1)) + 1:
        raise ContractError(f"candidate: invalid stage transition {value!r}")
    return text


def _positive_number(value: Any, field_name: str) -> float | None:
    if value in (None, "", "null", "None"):
        return None
    if isinstance(value, bool):
        raise ContractError(f"candidate: {field_name} must be a finite positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"candidate: {field_name} must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ContractError(f"candidate: {field_name} must be a finite positive number")
    return parsed


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def candidate_validation_inputs(candidate: Any) -> dict[str, Any]:
    """Return normalized frozen facts used by dashboard validation.

    Decision is authoritative for the D-day close.  Premium ``close_T`` is the
    same D-day close under the frozen date contract and therefore serves as an
    independent cross-check.  Missing values remain compatible with legacy
    frozen signals; current inputs are made mandatory at their source parsers.
    """

    values = _candidate_sources(candidate)
    decision = _source(values, DECISION_SOURCE)
    premium = _source(values, PREMIUM_SOURCE)
    a_top10 = _source(values, A_TOP10_SOURCE)

    stage_transition = _stage(
        _first_text(
            decision.get("stage_transition"),
            premium.get("stage_transition"),
            premium.get("晋阶"),
            a_top10.get("advance_stage"),
            a_top10.get("晋阶"),
        )
    )
    industry = _first_text(
        decision.get("industry"),
        premium.get("sector"),
        a_top10.get("board"),
    )
    decision_d_close = _positive_number(
        decision.get("d_close"),
        "decision d_close",
    )
    premium_d_close = _positive_number(
        premium.get("close_T"),
        "premium close_T",
    )
    if (
        decision_d_close is not None
        and premium_d_close is not None
        and not math.isclose(
            decision_d_close,
            premium_d_close,
            rel_tol=0.0,
            abs_tol=D_CLOSE_ABS_TOL,
        )
    ):
        raise ContractError(
            "candidate: Decision d_close and Premium close_T disagree "
            f"({decision_d_close} vs {premium_d_close})"
        )

    mechanism_limit_pct = _positive_number(
        decision.get("mechanism_limit_pct"),
        "mechanism_limit_pct",
    )
    estimated_up_limit = _positive_number(
        decision.get("estimated_up_limit"),
        "estimated_up_limit",
    )
    d_close = (
        decision_d_close
        if decision_d_close is not None
        else premium_d_close
    )
    if (
        d_close is not None
        and mechanism_limit_pct is not None
        and estimated_up_limit is not None
    ):
        expected_limit = float(
            (
                Decimal(str(d_close))
                * (Decimal("1") + Decimal(str(mechanism_limit_pct)) / Decimal("100"))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        if not math.isclose(
            estimated_up_limit,
            expected_limit,
            rel_tol=0.0,
            abs_tol=D_CLOSE_ABS_TOL,
        ):
            raise ContractError(
                "candidate: estimated_up_limit disagrees with frozen D close "
                f"and mechanism ({estimated_up_limit} vs {expected_limit})"
            )

    return {
        "stage_transition": stage_transition,
        "industry": industry,
        "decision_d_close": decision_d_close,
        "premium_d_close": premium_d_close,
        "d_close": d_close,
        "mechanism_limit_pct": mechanism_limit_pct,
        "estimated_up_limit": estimated_up_limit,
        "limit_up_price": estimated_up_limit,
        "limit_up_source": (
            "DECISION_FROZEN_LIMIT_PRICE"
            if estimated_up_limit is not None
            else None
        ),
    }


def candidate_display_fields(candidate: Any) -> dict[str, Any]:
    """Return the normalized fields shared by both candidate detail tables."""

    facts = candidate_validation_inputs(candidate)
    return {
        "stage_transition": facts["stage_transition"] or "—",
        "industry": facts["industry"] or "—",
        "d_close": facts["d_close"],
        "mechanism_limit_pct": facts["mechanism_limit_pct"],
        "estimated_up_limit": facts["estimated_up_limit"],
    }


__all__ = [
    "D_CLOSE_ABS_TOL",
    "candidate_display_fields",
    "candidate_validation_inputs",
]

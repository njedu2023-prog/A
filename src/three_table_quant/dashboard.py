from __future__ import annotations

import math
from typing import Any

from .domain import iso_date


def _compound(returns: list[float]) -> float:
    nav = 1.0
    for value in returns:
        nav *= 1.0 + value
    return nav - 1.0


FINAL_CASH_STATUSES = {"NOT_AVAILABLE", "NO_TRADE", "BUY_UNFILLED"}
NONFINAL_STATUSES = {
    "PENDING_BUY",
    "BUY_UNVERIFIABLE",
    "OPEN",
    "EXIT_UNVERIFIABLE",
    "EXIT_DELAYED",
}
ALLOWED_SLOT_STATUSES = FINAL_CASH_STATUSES | NONFINAL_STATUSES | {"CLOSED"}


def _finite(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _event_date(value: Any) -> str:
    """Normalize either YYYYMMDD or an event timestamp to an ISO date."""

    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) < 8:
        raise ValueError(f"event date is not recoverable from {value!r}")
    return iso_date(digits[:8])


def _return_date(signal: dict[str, Any], trade: dict[str, Any] | None) -> str:
    if trade and trade.get("status") == "CLOSED":
        exit_payload = trade.get("exit") or {}
        actual = exit_payload.get("actual_exit_date") or exit_payload.get("actual_exit_at")
        if actual:
            return _event_date(actual)
    return iso_date(signal["exit_date"])


def build_dashboard(
    state: dict[str, Any],
    issues: list[dict[str, Any]],
    generated_at: str,
    current_run: dict[str, Any],
    tracked_ranks: list[int],
) -> dict[str, Any]:
    trade_keys = [(item["decision_date"], item["rank"]) for item in state["trades"]]
    if len(trade_keys) != len(set(trade_keys)):
        raise ValueError("duplicate trade for fixed decision-date/rank slot")
    trades_by_key = {
        (item["decision_date"], item["rank"]): item for item in state["trades"]
    }
    days: list[dict[str, Any]] = []
    rank_daily: list[dict[str, Any]] = []
    for signal in sorted(state["signals"], key=lambda item: item["decision_date"]):
        candidates = signal.get("candidates", [])
        slots: dict[str, Any] = {}
        daily_ranks: dict[str, Any] = {}
        for rank in tracked_ranks:
            trade = trades_by_key.get((signal["decision_date"], rank))
            if trade is None:
                slot = {
                    "candidate_id": None,
                    "status": "NOT_AVAILABLE",
                    "reason": "NO_CANDIDATE_FOR_FIXED_RANK",
                    "buy": None,
                    "exit": None,
                    "pnl": None,
                }
                daily_return = 0.0
                is_final = True
                state_label = "CASH"
            else:
                slot = {
                    "candidate_id": f"{signal['decision_date']}:{trade['ts_code']}",
                    "status": trade["status"],
                    "reason": trade.get("reason"),
                    "buy": trade.get("buy"),
                    "exit": trade.get("exit"),
                    "pnl": trade.get("pnl"),
                    "diagnostics": trade.get("diagnostics", {}),
                }
                if trade["status"] == "CLOSED":
                    daily_return = float(trade["pnl"]["net_return_on_allocated"])
                    is_final = True
                    state_label = "CLOSED"
                elif trade["status"] in {"NO_TRADE", "BUY_UNFILLED"}:
                    daily_return = 0.0
                    is_final = True
                    state_label = "CASH"
                else:
                    daily_return = None
                    is_final = False
                    state_label = trade["status"]
            slots[str(rank)] = slot
            daily_ranks[str(rank)] = {
                "state": state_label,
                "is_final": is_final,
                "daily_return": daily_return,
                "return_date": _return_date(signal, trade),
            }
        days.append(
            {
                "decision_date": iso_date(signal["decision_date"]),
                "buy_date": iso_date(signal["buy_date"]),
                "planned_exit_date": iso_date(signal["exit_date"]),
                "selection_status": signal["status"],
                "source_snapshots": signal.get("source_snapshots", []),
                "market_data_provenance": signal.get("market_data_provenance", {}),
                "intersection_count": len(candidates),
                "candidates": [
                    {
                        "candidate_id": f"{signal['decision_date']}:{item['ts_code']}",
                        "symbol": item["ts_code"],
                        "name": item["name"],
                        "rank": item.get("rank"),
                        "model_score": item.get("metrics", {}).get("utility_score"),
                        "action": item.get("action"),
                        "action_reason": item.get("action_reason"),
                        "source_ranks": item.get("source_ranks", {}),
                        "metrics": item.get("metrics", {}),
                        "features": item.get("features", {}),
                    }
                    for item in candidates
                ],
                "rank_slots": slots,
            }
        )
        rank_daily.append(
            {
                "date": iso_date(signal["exit_date"]),
                "decision_date": iso_date(signal["decision_date"]),
                "ranks": daily_ranks,
            }
        )

    rank_daily.sort(key=lambda item: (item["date"], item["decision_date"]))
    metrics: dict[str, Any] = {}
    for rank in tracked_ranks:
        key = str(rank)
        equity = 1.0
        values: list[float] = []
        closed_values: list[float] = []
        pending_days = 0
        ordered_rows = sorted(
            rank_daily,
            key=lambda row: (row["ranks"][key]["return_date"], row["decision_date"]),
        )
        for row in ordered_rows:
            item = row["ranks"][key]
            value = item["daily_return"]
            if value is not None:
                equity *= 1.0 + value
                values.append(value)
            else:
                pending_days += 1
            item["equity_index"] = equity
            if item["state"] == "CLOSED":
                closed_values.append(float(value))
        metrics[key] = {
            "cumulative_return": _compound(values) if values else None,
            "final_days": len(values),
            "pending_days": pending_days,
            "is_provisional": bool(values and pending_days),
            "closed_trades": len(closed_values),
            "win_rate": (
                sum(value > 0 for value in closed_values) / len(closed_values)
                if closed_values
                else None
            ),
        }

    months = sorted(
        {
            item["return_date"][:7]
            for row in rank_daily
            for item in row["ranks"].values()
        },
        reverse=True,
    )
    return {
        "schema_version": "dashboard_v1",
        "generated_at": generated_at,
        "timezone": "Asia/Shanghai",
        "return_unit": "decimal",
        "policy": {
            "intersection": "ALL_THREE_ACTUAL_ROWS",
            "tracked_ranks": tracked_ranks,
            "allow_backfill": False,
            "execution_mode": "SHADOW_ONLY",
            "entry": "T 09:25 opening call auction exact fill truth required",
            "exit": "T+1 11:00-11:05 five one-minute slices",
        },
        "current_run": current_run,
        "source_issues": issues,
        "available_months": months,
        "rank_metrics": metrics,
        "days": days,
        "rank_daily": rank_daily,
    }


def validate_dashboard(payload: dict[str, Any]) -> None:
    if payload.get("schema_version") != "dashboard_v1":
        raise ValueError("dashboard schema mismatch")
    tracked = payload["policy"]["tracked_ranks"]
    if tracked != [1, 2, 3]:
        raise ValueError("dashboard must track fixed ranks 1, 2, and 3")
    expected_rank_keys = {str(rank) for rank in tracked}
    seen_decision_dates: set[str] = set()
    days_by_decision_date: dict[str, dict[str, Any]] = {}
    for day in payload["days"]:
        decision_date = day["decision_date"]
        if decision_date in seen_decision_dates:
            raise ValueError("duplicate dashboard decision_date")
        seen_decision_dates.add(decision_date)
        days_by_decision_date[decision_date] = day
        if day["intersection_count"] != len(day["candidates"]):
            raise ValueError("intersection_count does not match candidates")
        candidate_ranks = [item["rank"] for item in day["candidates"]]
        if candidate_ranks != list(range(1, len(candidate_ranks) + 1)):
            raise ValueError("candidate ranks must be contiguous and deterministic")
        candidate_ids = [item["candidate_id"] for item in day["candidates"]]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate ids must be unique within a decision date")
        if day["intersection_count"] == 0:
            if day["selection_status"] != "NO_CANDIDATE":
                raise ValueError("zero intersection must be NO_CANDIDATE")
        elif day["selection_status"] != "RANKED":
            raise ValueError("non-empty intersection must be RANKED")
        if set(day["rank_slots"]) != expected_rank_keys:
            raise ValueError("fixed rank slots are incomplete")
        candidates_by_rank = {item["rank"]: item for item in day["candidates"]}
        for rank in tracked:
            slot = day["rank_slots"][str(rank)]
            status = slot.get("status")
            if status not in ALLOWED_SLOT_STATUSES:
                raise ValueError(f"unsupported fixed-rank status: {status}")
            candidate = candidates_by_rank.get(rank)
            if candidate is None:
                if status != "NOT_AVAILABLE" or slot.get("candidate_id") is not None:
                    raise ValueError("missing fixed rank must remain NOT_AVAILABLE")
            elif slot.get("candidate_id") != candidate["candidate_id"]:
                raise ValueError("fixed-rank slot candidate does not match ranked candidate")
            if status in {"NOT_AVAILABLE", "NO_TRADE"}:
                if any(slot.get(field) is not None for field in ("buy", "exit", "pnl")):
                    raise ValueError(f"{status} slot cannot contain execution data")
            elif status == "BUY_UNFILLED":
                buy = slot.get("buy") or {}
                if buy.get("filled_qty") != 0 or slot.get("exit") is not None or slot.get("pnl") is not None:
                    raise ValueError("BUY_UNFILLED slot has inconsistent execution data")
            elif status == "CLOSED":
                buy = slot.get("buy") or {}
                exit_payload = slot.get("exit") or {}
                pnl = slot.get("pnl") or {}
                if not buy or exit_payload.get("remaining_qty") != 0:
                    raise ValueError("CLOSED slot must have a fully exited position")
                if not _finite(pnl.get("net_return_on_allocated")):
                    raise ValueError("CLOSED slot must have a finite allocated return")
            elif status == "EXIT_DELAYED":
                exit_payload = slot.get("exit") or {}
                if not slot.get("buy") or int(exit_payload.get("remaining_qty") or 0) <= 0:
                    raise ValueError("EXIT_DELAYED slot must retain an open quantity")

    seen_rank_decision_dates: set[str] = set()
    for row in payload["rank_daily"]:
        if row.get("decision_date") in seen_rank_decision_dates:
            raise ValueError("duplicate rank_daily decision_date")
        seen_rank_decision_dates.add(row.get("decision_date"))
        day = days_by_decision_date.get(row.get("decision_date"))
        if day is None or row["date"] != day["planned_exit_date"]:
            raise ValueError("rank_daily row is not linked to its decision day")
        if set(row["ranks"]) != expected_rank_keys:
            raise ValueError("rank_daily fixed slots are incomplete")
        for rank in tracked:
            key = str(rank)
            item = row["ranks"][key]
            slot = day["rank_slots"][key]
            value = item.get("daily_return")
            is_final = item.get("is_final")
            return_date = item.get("return_date")
            if not isinstance(return_date, str) or len(return_date) != 10:
                raise ValueError("rank_daily return_date must be an ISO date")
            if not isinstance(is_final, bool):
                raise ValueError("rank_daily is_final must be boolean")
            if is_final:
                if not _finite(value):
                    raise ValueError("final rank day must contain a finite return")
            elif value is not None:
                raise ValueError("non-final rank day must keep daily_return null")
            status = slot["status"]
            if status == "CLOSED":
                expected = float(slot["pnl"]["net_return_on_allocated"])
                if item["state"] != "CLOSED" or not is_final or not math.isclose(float(value), expected):
                    raise ValueError("CLOSED slot and rank_daily return disagree")
                exit_payload = slot.get("exit") or {}
                actual = exit_payload.get("actual_exit_date") or exit_payload.get("actual_exit_at")
                expected_date = _event_date(actual) if actual else day["planned_exit_date"]
                if return_date != expected_date:
                    raise ValueError("CLOSED return must be attributed to its actual exit date")
            elif status in FINAL_CASH_STATUSES:
                if item["state"] != "CASH" or not is_final or float(value) != 0.0:
                    raise ValueError("final cash slot must have a numeric zero return")
                if return_date != day["planned_exit_date"]:
                    raise ValueError("cash result must remain on the planned exit date")
            elif item["state"] != status or is_final or value is not None:
                raise ValueError("pending slot and rank_daily state disagree")
            elif return_date != day["planned_exit_date"]:
                raise ValueError("pending result must remain on its planned exit date")

    for rank in tracked:
        key = str(rank)
        running_equity = 1.0
        ordered_rows = sorted(
            payload["rank_daily"],
            key=lambda row: (row["ranks"][key]["return_date"], row["decision_date"]),
        )
        for row in ordered_rows:
            item = row["ranks"][key]
            if item["is_final"]:
                running_equity *= 1.0 + float(item["daily_return"])
            if not _finite(item.get("equity_index")) or not math.isclose(
                float(item["equity_index"]),
                running_equity,
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("rank equity index does not match compounded final returns")

    rank_metrics = payload.get("rank_metrics")
    if not isinstance(rank_metrics, dict) or set(rank_metrics) != expected_rank_keys:
        raise ValueError("rank_metrics must contain exactly the fixed ranks")
    for rank in tracked:
        key = str(rank)
        values: list[float] = []
        closed_values: list[float] = []
        pending_days = 0
        ordered_rows = sorted(
            payload["rank_daily"],
            key=lambda row: (row["ranks"][key]["return_date"], row["decision_date"]),
        )
        for row in ordered_rows:
            item = row["ranks"][key]
            if item["is_final"]:
                values.append(float(item["daily_return"]))
            else:
                pending_days += 1
            if item["state"] == "CLOSED":
                closed_values.append(float(item["daily_return"]))
        metric = rank_metrics[key]
        expected_cumulative = _compound(values) if values else None
        actual_cumulative = metric.get("cumulative_return")
        if expected_cumulative is None:
            cumulative_matches = actual_cumulative is None
        else:
            cumulative_matches = _finite(actual_cumulative) and math.isclose(
                float(actual_cumulative),
                expected_cumulative,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        expected_win_rate = (
            sum(value > 0 for value in closed_values) / len(closed_values)
            if closed_values
            else None
        )
        actual_win_rate = metric.get("win_rate")
        if expected_win_rate is None:
            win_rate_matches = actual_win_rate is None
        else:
            win_rate_matches = _finite(actual_win_rate) and math.isclose(
                float(actual_win_rate),
                expected_win_rate,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        if not cumulative_matches:
            raise ValueError("rank cumulative return does not match final observations")
        if metric.get("final_days") != len(values) or metric.get("pending_days") != pending_days:
            raise ValueError("rank final and pending counts do not match observations")
        if metric.get("is_provisional") is not bool(values and pending_days):
            raise ValueError("rank provisional state does not match observations")
        if metric.get("closed_trades") != len(closed_values) or not win_rate_matches:
            raise ValueError("rank win rate must use CLOSED observations only")

    expected_months = sorted(
        {
            item["return_date"][:7]
            for row in payload["rank_daily"]
            for item in row["ranks"].values()
        },
        reverse=True,
    )
    if payload.get("available_months") != expected_months:
        raise ValueError("available_months does not match rank_daily")

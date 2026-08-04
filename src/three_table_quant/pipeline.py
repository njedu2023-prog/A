from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .dashboard import build_dashboard, validate_dashboard
from .domain import Signal, SourceIssue
from .http import HttpClient
from .ledger import (
    add_signal,
    ensure_shadow_trades,
    load_json,
    load_state,
    migrate_signal_candidates_to_shadow,
    save_json,
    settle_trades,
)
from .market import ResilientMarketData
from .scoring import score_candidates
from .sources import (
    SourceLoader,
    diagnose_source_quality,
    freeze_gate_issue,
    source_snapshot_changes,
    strict_intersection,
    validate_timeline,
)


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _signal_id(decision_date: str, snapshots: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256(json.dumps(snapshots, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{decision_date}-{digest}"


def _source_snapshots(tables: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": table.source_id,
            "url": table.url,
            "decision_date": table.decision_date,
            "content_sha256": table.content_sha256,
            "repository_commit_sha": table.remote_blob_sha,
            "generated_at": table.generated_at,
            "row_count": len(table.rows),
        }
        for table in tables
    ]


def _append_market_fallback_issue(
    source_issues: list[SourceIssue],
    market: Any,
) -> None:
    events = list(getattr(market, "fallback_events", []))
    if not events or any(item.code == "MARKET_DATA_PROVIDER_FALLBACK" for item in source_issues):
        return
    affected_codes = sorted(
        {
            str(item.get("ts_code"))
            for item in events
            if item.get("ts_code")
        }
    )
    request_types = sorted(
        {
            str(item.get("request_type"))
            for item in events
            if item.get("request_type")
        }
    )
    successful = sum(item.get("fallback_status") == "SUCCESS" for item in events)
    source_issues.append(
        SourceIssue(
            "MARKET_DATA_PROVIDER_FALLBACK",
            "warning",
            "market_data",
            f"Eastmoney 行情不可用或已跳闸；{len(affected_codes)} 支股票已聚合切换至腾讯只读行情",
            {
                "primary_provider": "EASTMONEY",
                "fallback_provider": "TENCENT",
                "affected_codes": affected_codes,
                "request_types": request_types,
                "event_count": len(events),
                "successful_event_count": successful,
                "failed_event_count": len(events) - successful,
                "circuit_open": bool(getattr(market, "circuit_open", False)),
            },
        )
    )


def _market_data_provenance(bars_by_code: dict[str, list[Any]]) -> dict[str, Any]:
    return {
        "daily_features": {
            ts_code: {
                "providers": sorted(
                    {str(getattr(bar, "provider", "UNSPECIFIED")).upper() for bar in bars}
                ),
                "price_adjustments": sorted(
                    {
                        str(getattr(bar, "price_adjustment", "UNSPECIFIED")).upper()
                        for bar in bars
                    }
                ),
                "bar_count": len(bars),
            }
            for ts_code, bars in sorted(bars_by_code.items())
        }
    }


def _append_frozen_market_fallback_issue(
    source_issues: list[SourceIssue],
    signal: dict[str, Any] | None,
) -> None:
    if signal is None or any(item.code == "MARKET_DATA_PROVIDER_FALLBACK" for item in source_issues):
        return
    daily = signal.get("market_data_provenance", {}).get("daily_features", {})
    affected_codes = sorted(
        ts_code
        for ts_code, item in daily.items()
        if "TENCENT" in item.get("providers", [])
    )
    if not affected_codes:
        return
    source_issues.append(
        SourceIssue(
            "MARKET_DATA_PROVIDER_FALLBACK",
            "warning",
            "market_data",
            "冻结信号的D日特征使用腾讯只读行情回退；本次重跑未改写特征",
            {
                "primary_provider": "EASTMONEY",
                "fallback_provider": "TENCENT",
                "affected_codes": affected_codes,
                "persisted_with_frozen_signal": True,
            },
        )
    )


def _ensure_all_candidate_shadow_ledger(
    state: dict[str, Any],
    tracked_ranks: list[int],
) -> None:
    """Migrate and backfill every frozen signal before execution settlement."""

    for signal in state.get("signals", []):
        migrate_signal_candidates_to_shadow(signal)
        ensure_shadow_trades(state, signal, tracked_ranks)


def run_pipeline(config_path: str | Path = "config/system.json") -> dict[str, Any]:
    config = load_config(config_path)
    paths = config["paths"]
    state = load_state(paths["state"])
    truth = load_json(
        paths["execution_truth"],
        {"schema_version": "execution_truth_v1", "auctions": {}},
    )
    http = HttpClient()
    market = ResilientMarketData(http)
    active_signal: dict[str, Any] | None = None

    # Pending historical positions are processed even when today's source set is blocked.
    _ensure_all_candidate_shadow_ledger(state, list(config["tracked_ranks"]))
    settle_trades(state, truth, market, config["execution"])

    loader = SourceLoader(config, http)
    tables, source_issues = loader.load()
    source_issues.extend(
        validate_timeline(
            tables,
            config["input_contract"]["trading_calendar_path"],
        )
    )
    source_issues.extend(diagnose_source_quality(tables))
    current_run: dict[str, Any] = {
        "status": "INPUT_BLOCKED" if any(item.severity == "error" for item in source_issues) else "READY",
        "message": "源数据契约阻断；禁止跨日拼接或回退旧榜单" if any(item.severity == "error" for item in source_issues) else "三源日期链已对齐",
        "source_table_counts": {table.source_id: len(table.rows) for table in tables},
        "source_dates": {
            table.source_id: {
                "D": table.decision_date,
                "T": table.buy_date,
                "T1": table.exit_date or None,
            }
            for table in tables
        },
        "intersection_count": None,
    }

    if not any(item.severity == "error" for item in source_issues):
        candidates = strict_intersection(tables)
        current_run["intersection_count"] = len(candidates)
        by_id = {table.source_id: table for table in tables}
        decision_date = tables[0].decision_date
        buy_date = tables[0].buy_date
        exit_date = by_id["premium_top10"].exit_date
        existing_signal = next(
            (item for item in state["signals"] if item.get("decision_date") == decision_date),
            None,
        )
        if existing_signal is None:
            gate_issue = freeze_gate_issue(
                decision_date,
                config["input_contract"]["freeze_after_local_time"],
            )
            if gate_issue is not None:
                source_issues.append(gate_issue)
                current_run["status"] = "INPUT_BLOCKED"
                current_run["message"] = gate_issue.message
        if existing_signal is not None:
            active_signal = existing_signal
            frozen_codes = {item["ts_code"] for item in existing_signal.get("candidates", [])}
            live_codes = {item.ts_code for item in candidates}
            changed_sources = source_snapshot_changes(
                existing_signal.get("source_snapshots", []),
                _source_snapshots(tables),
            )
            if frozen_codes != live_codes or changed_sources:
                source_issues.append(
                    SourceIssue(
                        "SOURCE_REVISION_AFTER_FREEZE",
                        "warning",
                        "aggregate",
                        "同一D日的源表内容或固定仓库提交在冻结后发生变化；既有冻结排名未被改写",
                        {
                            "frozen_codes": sorted(frozen_codes),
                            "live_codes": sorted(live_codes),
                            "changed_sources": changed_sources,
                        },
                    )
                )
            current_run["status"] = existing_signal["status"]
            current_run["message"] = f"D日信号已冻结；保留{len(frozen_codes)}支候选且不事后改写"
        elif not any(item.severity == "error" for item in source_issues):
            bars_by_code: dict[str, list[Any]] = {}
            for candidate in candidates:
                try:
                    bars_by_code[candidate.ts_code] = market.daily_bars(candidate.ts_code, decision_date, limit=100)
                except Exception as exc:
                    bars_by_code[candidate.ts_code] = []
                    source_issues.append(
                        SourceIssue(
                            "MARKET_FEATURES_UNAVAILABLE",
                            "warning",
                            "market_data",
                            f"{candidate.ts_code} 的D日前行情特征不可用：{exc}",
                        )
                    )
            scored = score_candidates(
                candidates,
                bars_by_code,
                {table.source_id: len(table.rows) for table in tables},
                config,
            )
            snapshots = _source_snapshots(tables)
            signal = Signal(
                signal_id=_signal_id(decision_date, snapshots),
                decision_date=decision_date,
                buy_date=buy_date,
                exit_date=exit_date,
                generated_at=_now(),
                source_snapshots=snapshots,
                candidates=scored,
                model_version=config["ranking"]["version"],
                status="RANKED" if scored else "NO_CANDIDATE",
                market_data_provenance=_market_data_provenance(bars_by_code),
            ).to_dict()
            active_signal = signal
            add_signal(state, signal)
            current_run["status"] = signal["status"]
            current_run["message"] = (
                f"三表严格交集实际保留{len(scored)}支并完成重新排序"
                if scored
                else "三表交集为0；合法空选且不补票"
            )
        # This is intentionally independent of add_signal(): a frozen legacy
        # signal can be missing rank 4+ shadow trades and must be repaired
        # idempotently before any settlement attempt.
        _ensure_all_candidate_shadow_ledger(state, list(config["tracked_ranks"]))
        settle_trades(state, truth, market, config["execution"])

    _append_market_fallback_issue(source_issues, market)
    _append_frozen_market_fallback_issue(source_issues, active_signal)
    generated_at = _now()
    issue_payload = {
        "schema_version": "source_issues_v1",
        "generated_at": generated_at,
        "issues": [item.to_dict() for item in source_issues],
    }
    dashboard = build_dashboard(
        state,
        issue_payload["issues"],
        generated_at,
        current_run,
        list(config["tracked_ranks"]),
    )
    validate_dashboard(dashboard)
    save_json(paths["state"], state)
    save_json(paths["source_issues"], issue_payload)
    save_json(paths["dashboard"], dashboard)
    return dashboard

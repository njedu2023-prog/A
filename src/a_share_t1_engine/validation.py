from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import yaml


HISTORY_FIELDS = [
    "prediction_id",
    "validated_at",
    "rank",
    "code",
    "name",
    "top_n",
    "route",
    "height",
    "probability",
    "sas",
    "sentiment_bucket",
    "auction_buy_date",
    "expected_sell_date",
    "continued",
]


def validate_prediction_file(
    prediction_path: Path,
    actuals_path: Path,
    output_path: Path,
    history_path: Path,
) -> dict[str, Any]:
    prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
    actuals = _load_actuals(actuals_path)
    validated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trade_dates = prediction.get("trade_dates", {})
    rows = [_validation_row(candidate, actuals, prediction["prediction_id"], validated_at, trade_dates) for candidate in prediction["candidates"]]
    _upsert_history(history_path, rows)
    history = _read_history(history_path)
    stats = _build_stats(history)
    report = _render_validation_html(prediction, rows, stats, history_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    return stats


def _load_actuals(path: Path) -> dict[str, bool]:
    if path.suffix.lower() == ".csv":
        return _load_actuals_csv(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    actuals: dict[str, bool] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                actuals[str(item["code"])] = _continued_value(item)
        return actuals
    for code, value in raw.items():
        actuals[str(code)] = _continued_value(value)
    return actuals


def _load_actuals_csv(path: Path) -> dict[str, bool]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    actuals: dict[str, bool] = {}
    for row in rows:
        code = _first_value(row, ("code", "股票代码", "证券代码", "代码"))
        if not code:
            continue
        continued = _first_value(row, ("continued", "hit", "是否连板", "连板", "晋级", "结果", "状态"))
        actuals[_normalize_code(code)] = _continued_value(continued)
    return actuals


def _first_value(row: dict[str, str], keys: tuple[str, ...]) -> str:
    normalized = {key.strip().lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row[key]:
            return row[key]
        lowered = key.lower()
        if lowered in normalized and normalized[lowered]:
            return normalized[lowered]
    return ""


def _normalize_code(value: str) -> str:
    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _continued_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return bool(value.get("continued") or value.get("hit") or value.get("连板"))
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"false", "no", "n", "0", "miss", "未连板", "未晋级", "否", "失败"}:
            return False
        return normalized in {"true", "yes", "y", "1", "hit", "continued", "连板", "晋级", "是", "成功"}
    return bool(value)


def _validation_row(
    candidate: dict[str, Any],
    actuals: dict[str, bool],
    prediction_id: str,
    validated_at: str,
    trade_dates: dict[str, Any],
) -> dict[str, str]:
    code = str(candidate["code"])
    return {
        "prediction_id": prediction_id,
        "validated_at": validated_at,
        "rank": str(candidate["rank"]),
        "code": code,
        "name": str(candidate["name"]),
        "top_n": "1" if candidate.get("top_n") else "0",
        "route": str(candidate["route"]),
        "height": str(candidate["height"]),
        "probability": f"{float(candidate['probability']):.1f}",
        "sas": f"{float(candidate.get('sas', 50.0)):.1f}",
        "sentiment_bucket": str(candidate.get("sentiment_bucket", "medium")),
        "auction_buy_date": str(trade_dates.get("auction_buy_date_iso") or trade_dates.get("auction_buy_date") or ""),
        "expected_sell_date": str(trade_dates.get("expected_sell_date") or ""),
        "continued": "1" if actuals.get(code, False) else "0",
    }


def _upsert_history(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_history(path)
    merged = {(row["prediction_id"], row["code"]): row for row in existing}
    for row in rows:
        merged[(row["prediction_id"], row["code"])] = row
    ordered = sorted(merged.values(), key=lambda row: (row["prediction_id"], int(row["rank"])))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(ordered)


def _read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _build_stats(history: list[dict[str, str]]) -> dict[str, Any]:
    top_rows = [row for row in history if row["top_n"] == "1"]
    return {
        "overall_top_n": _hit_stats(top_rows),
        "by_rank": _group_stats(top_rows, "rank"),
        "by_route": _group_stats(top_rows, "route"),
        "by_sentiment_bucket": _group_stats(top_rows, "sentiment_bucket"),
        "all_candidates": _hit_stats(history),
    }


def _group_stats(rows: list[dict[str, str]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key, "unknown")].append(row)
    return [{"bucket": bucket, **_hit_stats(items)} for bucket, items in sorted(grouped.items())]


def _hit_stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    total = len(rows)
    hits = sum(1 for row in rows if row["continued"] == "1")
    return {"total": total, "hits": hits, "hit_rate": round(hits / total * 100.0, 1) if total else 0.0}


def _render_validation_html(
    prediction: dict[str, Any],
    rows: list[dict[str, str]],
    stats: dict[str, Any],
    history_path: Path,
) -> str:
    top_rows = [row for row in rows if row["top_n"] == "1"]
    summary = stats["overall_top_n"]
    trade_dates = prediction.get("trade_dates", {})
    auction_buy_date = str(trade_dates.get("auction_buy_date_iso") or trade_dates.get("auction_buy_date") or "")
    expected_sell_date = str(trade_dates.get("expected_sell_date") or "")
    row_html = "\n".join(_candidate_html(row) for row in top_rows)
    route_rows = "\n".join(_stats_html(row["bucket"], row) for row in stats["by_route"])
    rank_rows = "\n".join(_stats_html(f"第{row['bucket']}名", row) for row in stats["by_rank"])
    sentiment_rows = "\n".join(_stats_html(row["bucket"], row) for row in stats["by_sentiment_bucket"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A股T+1验证报告</title>
  <style>
    body {{ margin: 0; background: #f5f5f7; color: #1d1d1f; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif; }}
    .nav {{ height: 44px; display: flex; align-items: center; justify-content: center; gap: 28px; background: rgba(251,251,253,.78); backdrop-filter: saturate(180%) blur(20px); border-bottom: 1px solid rgba(0,0,0,.08); font-size: 12px; }}
    .nav a {{ color: #1d1d1f; text-decoration: none; opacity: .78; }}
    .nav a:hover {{ opacity: 1; }}
    header {{ background: #fff; padding: 54px 28px 34px; text-align: center; margin-bottom: 12px; }}
    main {{ max-width: 1180px; margin: 0 auto 40px; padding: 0 12px; }}
    section {{ background: #fff; padding: 36px 28px; margin: 12px 0; }}
    h1 {{ margin: 0 0 8px; font-size: 46px; line-height: 1.08; }}
    h2 {{ margin: 0 0 18px; font-size: 28px; line-height: 1.14; text-align: center; }}
    .muted {{ color: #6e6e73; font-size: 14px; }}
    .metric {{ display: inline-block; min-width: 148px; margin: 12px 8px 0; padding: 18px 20px; background: #f5f5f7; text-align: center; }}
    .metric b {{ display: block; color: #1d1d1f; font-size: 34px; line-height: 1.05; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #d2d2d7; padding: 11px 12px; text-align: left; font-size: 14px; }}
    th {{ color: #6e6e73; font-weight: 600; }}
    .hit {{ color: #0071e3; font-weight: 700; }}
    .miss {{ color: #6e6e73; }}
  </style>
</head>
<body>
  <nav class="nav"><a href="#summary">命中率</a><a href="#current">本次验证</a><a href="#route">路径累计</a><a href="#sentiment">搜索强度</a><a href="#rank">排名累计</a></nav>
  <header>
    <h1>A股T+1验证报告</h1>
    <div class="muted">预测批次：{escape(prediction["prediction_id"])}</div>
    <div class="muted">竞价买入日：{escape(auction_buy_date or "-")}；应验证卖出日：{escape(expected_sell_date or "-")}</div>
    <div class="muted">累计文件：{escape(str(history_path))}</div>
  </header>
  <main>
    <section id="summary">
      <h2>Top{escape(str(prediction["top_n"]))}累计命中率</h2>
      <div class="metric">样本 <b>{summary["total"]}</b></div>
      <div class="metric">命中 <b>{summary["hits"]}</b></div>
      <div class="metric">命中率 <b>{summary["hit_rate"]:.1f}%</b></div>
    </section>
    <section id="current">
      <h2>本次候选四票验证</h2>
      <table><thead><tr><th>排名</th><th>代码</th><th>名称</th><th>概率</th><th>SAS</th><th>路径</th><th>应验证卖出日</th><th>是否连板</th></tr></thead><tbody>{row_html}</tbody></table>
    </section>
    <section id="route">
      <h2>按路径累计</h2>
      <table><thead><tr><th>路径</th><th>样本</th><th>命中</th><th>命中率</th></tr></thead><tbody>{route_rows}</tbody></table>
    </section>
    <section id="rank">
      <h2>按排名累计</h2>
      <table><thead><tr><th>排名</th><th>样本</th><th>命中</th><th>命中率</th></tr></thead><tbody>{rank_rows}</tbody></table>
    </section>
    <section id="sentiment">
      <h2>按搜索强度累计</h2>
      <table><thead><tr><th>搜索强度</th><th>样本</th><th>命中</th><th>命中率</th></tr></thead><tbody>{sentiment_rows}</tbody></table>
    </section>
  </main>
</body>
</html>
"""


def _candidate_html(row: dict[str, str]) -> str:
    css = "hit" if row["continued"] == "1" else "miss"
    label = "连板" if row["continued"] == "1" else "未连板"
    return (
        f"<tr><td>{escape(row['rank'])}</td><td>{escape(row['code'])}</td><td>{escape(row['name'])}</td>"
        f"<td>{escape(row['probability'])}%</td><td>{escape(row.get('sas', '50.0'))}</td>"
        f"<td>{escape(row['route'])}</td><td>{escape(row.get('expected_sell_date', ''))}</td><td class=\"{css}\">{label}</td></tr>"
    )


def _stats_html(label: str, stats: dict[str, Any]) -> str:
    return f"<tr><td>{escape(label)}</td><td>{stats['total']}</td><td>{stats['hits']}</td><td>{stats['hit_rate']:.1f}%</td></tr>"

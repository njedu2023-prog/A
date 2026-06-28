from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml

from .models import SectorRotation, StockRecord
from .parser import calculate_brs, infer_board_quality_from_limit_times


def merge_ths_data(
    records: list[StockRecord],
    sectors: list[SectorRotation],
    path: str | Path | None,
    config: dict[str, Any],
) -> list[SectorRotation]:
    if path is None:
        return sectors
    payload = _load_payload(Path(path))
    stock_rows, sector_rows = _normalize_payload(payload)
    _merge_stock_rows(records, stock_rows, config)
    return _merge_sector_rows(sectors, sector_rows, config)


def _load_payload(path: Path) -> Any:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _normalize_payload(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(payload, dict):
        stocks = payload.get("stocks") or payload.get("个股") or payload.get("stock_rows") or []
        sectors = payload.get("sectors") or payload.get("板块") or payload.get("sector_rows") or []
        intraday = payload.get("intraday") or payload.get("竞价开盘") or []
        return _ensure_rows(stocks) + _ensure_rows(intraday), _ensure_rows(sectors)
    if isinstance(payload, list):
        stock_rows: list[dict[str, Any]] = []
        sector_rows: list[dict[str, Any]] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            if _first_value(row, ("板块名称", "板块", "sector", "sector_name")):
                sector_rows.append(row)
            else:
                stock_rows.append(row)
        return stock_rows, sector_rows
    return [], []


def _ensure_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            if isinstance(item, dict):
                row = {"code": key, **item}
            else:
                row = {"code": key, "value": item}
            rows.append(row)
        return rows
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _merge_stock_rows(records: list[StockRecord], rows: list[dict[str, Any]], config: dict[str, Any]) -> None:
    by_code = {record.code: record for record in records}
    for row in rows:
        code = _normalize_code(_first_value(row, ("code", "股票代码", "证券代码", "代码")))
        if not code:
            continue
        record = by_code.get(code)
        if record is None:
            continue
        _apply_stock_row(record, row, config)


def _apply_stock_row(record: StockRecord, row: dict[str, Any], config: dict[str, Any]) -> None:
    record.board_quality = str(_first_value(row, ("board_quality", "封板质量")) or record.board_quality)
    record.first_limit_up_time = str(_first_value(row, ("first_limit_up_time", "首次涨停时间", "首封时间")) or record.first_limit_up_time)
    record.final_limit_up_time = str(_first_value(row, ("final_limit_up_time", "最终涨停时间", "终封时间", "最后涨停时间")) or record.final_limit_up_time)
    if record.board_quality == "未知" and record.first_limit_up_time and record.final_limit_up_time:
        record.board_quality = infer_board_quality_from_limit_times(record.first_limit_up_time, record.final_limit_up_time, config)

    _assign_float(record, "order_to_turnover_pct", row, ("order_to_turnover_pct", "封单占成交", "封成比"))
    _assign_amount_yi(record, "max_seal_order_yi", row, ("max_seal_order_yi", "最高封单", "涨停封单额", "封单额"))
    _assign_amount_yi(record, "limit_up_amount_yi", row, ("limit_up_amount_yi", "涨停成交额"))
    _assign_float(record, "turnover_rate_pct", row, ("turnover_rate_pct", "换手率", "换手%"))
    height = _float_value(_first_value(row, ("height", "连板高度", "连续涨停天数", "连板数")))
    if height > 0:
        record.height = max(1, int(height))
    record.theme = str(_first_value(row, ("theme", "题材", "涨停原因")) or record.theme)
    record.industry = str(_first_value(row, ("industry", "行业", "所属行业")) or record.industry)

    _assign_float(record, "search_abnormal_ratio", row, ("search_abnormal_ratio", "搜索异常倍数"))
    _assign_float(record, "discussion_abnormal_ratio", row, ("discussion_abnormal_ratio", "讨论异常倍数"))
    sentiment = _first_value(row, ("sentiment_direction", "sentiment", "情绪方向"))
    if sentiment:
        record.sentiment_direction = str(sentiment).lower()
    _assign_float(record, "source_credibility", row, ("source_credibility", "来源可信度"))

    _assign_float(record, "auction_change_pct", row, ("auction_change_pct", "竞价涨幅", "集合竞价涨幅"))
    _assign_amount_yi(record, "auction_amount_yi", row, ("auction_amount_yi", "竞价成交额", "集合竞价成交额"))
    _assign_amount_yi(record, "auction_seal_order_yi", row, ("auction_seal_order_yi", "竞价封单", "竞价封单额"))
    _assign_amount_yi(record, "opening_5m_amount_yi", row, ("opening_5m_amount_yi", "开盘5分钟成交额", "开盘五分钟成交额"))
    reseal = _first_value(row, ("fast_reseal", "快速回封"))
    if reseal:
        record.fast_reseal = str(reseal).strip().lower() in {"1", "true", "yes", "y", "是", "有", "回封", "快速回封"}


def _merge_sector_rows(sectors: list[SectorRotation], rows: list[dict[str, Any]], config: dict[str, Any]) -> list[SectorRotation]:
    merged = {sector.name: sector for sector in sectors}
    for row in rows:
        name = str(_first_value(row, ("name", "sector", "sector_name", "板块名称", "板块"))).strip()
        if not name:
            continue
        current = merged.get(name)
        net_flow = _amount_yi_value(_first_value(row, ("net_flow_amount_yi", "资金净流入", "主力净流入", "昨日板块主力净流入")))
        limit_count_raw = _first_value(row, ("limit_up_count", "涨停家数", "涨停数", "涨停数今"))
        limit_count = int(_float_value(limit_count_raw)) if limit_count_raw not in (None, "") else (current.limit_up_count if current else 0)
        heat_value = _heat_value(_first_value(row, ("heat_value", "热度")))
        if heat_value == 0.0 and current:
            heat_value = current.heat_value
        heat_token = str(_first_value(row, ("heat_token", "热度等级", "强弱")) or (current.heat_token if current else _heat_token(limit_count, heat_value)))
        merged[name] = SectorRotation(
            name=name,
            net_flow_amount_yi=net_flow,
            limit_up_count=limit_count,
            heat_token=heat_token,
            brs=calculate_brs(net_flow, limit_count, heat_token, config),
            heat_value=heat_value,
        )
    return list(merged.values())


def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    normalized = {str(key).strip().lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        lowered = key.lower()
        if lowered in normalized and normalized[lowered] not in (None, ""):
            return normalized[lowered]
    return ""


def _normalize_code(value: Any) -> str:
    digits = "".join(char for char in str(value) if char.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits


def _assign_float(record: StockRecord, attr: str, row: dict[str, Any], keys: tuple[str, ...]) -> None:
    value = _float_value(_first_value(row, keys))
    if value > 0:
        setattr(record, attr, value)


def _assign_amount_yi(record: StockRecord, attr: str, row: dict[str, Any], keys: tuple[str, ...]) -> None:
    value = _amount_yi_value(_first_value(row, keys))
    if value > 0:
        setattr(record, attr, value)


def _amount_yi_value(value: Any) -> float:
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    number = _float_value(text.rstrip("亿元万"))
    if "万" in text and "亿" not in text:
        return number / 10000.0
    return number


def _heat_value(value: Any) -> float:
    text = str(value).strip().replace(",", "")
    if not text:
        return 0.0
    number = _float_value(text.rstrip("热度万"))
    return number * 10000.0 if "万" in text else number


def _heat_token(limit_count: int, heat_value: float) -> str:
    if limit_count >= 7 or heat_value >= 20000:
        return "极强"
    if limit_count >= 5 or heat_value >= 10000:
        return "强"
    if limit_count >= 3 or heat_value >= 5000:
        return "活跃"
    return "中性"


def _float_value(value: Any) -> float:
    try:
        return float(str(value).strip().replace("%", "").replace("：", "."))
    except (TypeError, ValueError):
        return 0.0

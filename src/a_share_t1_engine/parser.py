from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .models import SectorRotation, StockRecord

CODE_RE = re.compile(r"(?P<code>\d{6})(?:\.(?:SH|SZ|BJ))?\s*(?P<name>[\u4e00-\u9fa5A-Za-z0-9*]+)?")


def parse_report_text(text: str, config: dict[str, Any]) -> tuple[list[StockRecord], list[SectorRotation]]:
    records: dict[str, StockRecord] = {}
    lines = _clean_lines(text)
    _parse_lists(lines, records, config)
    _parse_stock_details(lines, records)
    _parse_sensitive_events(lines, records, config)
    sectors = _parse_sector_rotation(lines, config)
    return sorted(records.values(), key=lambda item: item.code), sectors


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "\n").splitlines() if line.strip()]


def _parse_lists(lines: list[str], records: dict[str, StockRecord], config: dict[str, Any]) -> None:
    aliases = config["parsing"]["list_section_aliases"]
    display = config["list_sources"]
    active_key: str | None = None
    for line in lines:
        section_key = _match_section(line, aliases)
        if section_key:
            active_key = section_key
            _add_codes_from_line(line, records, display[section_key])
            continue
        if active_key and _looks_like_new_section(line):
            active_key = None
        if active_key:
            _add_codes_from_line(line, records, display[active_key])


def _parse_stock_details(lines: list[str], records: dict[str, StockRecord]) -> None:
    current: StockRecord | None = None
    for line in lines:
        match = CODE_RE.search(line)
        if match and _line_has_detail_hint(line):
            current = _get_or_create(records, match.group("code"), match.group("name") or "")
        if current is None:
            continue
        _apply_detail_line(current, line)


def _parse_sensitive_events(lines: list[str], records: dict[str, StockRecord], config: dict[str, Any]) -> None:
    aliases = config["parsing"]["sensitive_event_aliases"]
    in_section = False
    for line in lines:
        if any(token in line for token in ("敏感舆情", "公告", "事件")):
            in_section = True
        elif in_section and _looks_like_new_section(line):
            in_section = False
        if not in_section:
            continue
        for match in CODE_RE.finditer(line):
            record = _get_or_create(records, match.group("code"), match.group("name") or "")
            for event_key, event_aliases in aliases.items():
                if any(alias in line for alias in event_aliases) and event_key not in record.sensitive_events:
                    record.sensitive_events.append(event_key)


def _parse_sector_rotation(lines: list[str], config: dict[str, Any]) -> list[SectorRotation]:
    sectors: list[SectorRotation] = []
    in_section = False
    for line in lines:
        if "热门板块资金轮动" in line or "板块资金轮动" in line:
            in_section = True
            continue
        if in_section and _looks_like_new_section(line):
            break
        if not in_section or not re.search(r"[\u4e00-\u9fa5A-Za-z]", line):
            continue
        sector = _parse_sector_line(line, config)
        if sector:
            sectors.append(sector)
    return sectors


def _parse_sector_line(line: str, config: dict[str, Any]) -> SectorRotation | None:
    name_match = re.match(r"[-*•\d.、\s]*(?P<name>[\u4e00-\u9fa5A-Za-z0-9+/_-]{2,20})", line)
    if not name_match:
        return None
    name = name_match.group("name").strip("：:")
    net_flow = _extract_number(line, (r"净流入[:：]?\s*([-+]?\d+(?:\.\d+)?)\s*亿", r"资金[:：]?\s*([-+]?\d+(?:\.\d+)?)\s*亿"))
    limit_count = int(_extract_number(line, (r"涨停[:：]?\s*(\d+)", r"涨停家数[:：]?\s*(\d+)")))
    heat_token = _first_matching_token(line, config["brs"]["heat_tokens"].keys())
    brs = calculate_brs(net_flow, limit_count, heat_token, config)
    return SectorRotation(name=name, net_flow_amount_yi=net_flow, limit_up_count=limit_count, heat_token=heat_token, brs=brs)


def calculate_brs(net_flow: float, limit_count: int, heat_token: str, config: dict[str, Any]) -> float:
    weights = config["brs"]["weights"]
    norm = config["brs"]["normalization"]
    heat_scores = config["brs"]["heat_tokens"]
    flow_score = _cap_score(net_flow, norm["net_flow_amount_cap_yi"])
    limit_score = _cap_score(float(limit_count), norm["limit_up_count_cap"])
    heat_score = float(heat_scores.get(heat_token, heat_scores.get("中性", 50.0)))
    return round(
        flow_score * weights["net_flow_amount"]
        + limit_score * weights["limit_up_count"]
        + heat_score * weights["heat_token"],
        1,
    )


def _add_codes_from_line(line: str, records: dict[str, StockRecord], source: str) -> None:
    for match in CODE_RE.finditer(line):
        record = _get_or_create(records, match.group("code"), match.group("name") or "")
        record.list_sources.add(source)


def _get_or_create(records: dict[str, StockRecord], code: str, name: str) -> StockRecord:
    record = records.setdefault(code, StockRecord(code=code))
    if name and not record.name:
        record.name = name
    return record


def _apply_detail_line(record: StockRecord, line: str) -> None:
    if "封板质量" in line:
        record.board_quality = _extract_text_value(line, "封板质量") or record.board_quality
    if "封单占成交" in line:
        record.order_to_turnover_pct = _extract_number(line, (r"封单占成交[:：]?\s*([\d.]+)%?",))
    if "最高封单" in line:
        record.max_seal_order_yi = _extract_number(line, (r"最高封单[:：]?\s*([\d.]+)\s*亿?",))
    if "涨停成交额" in line:
        record.limit_up_amount_yi = _extract_number(line, (r"涨停成交额[:：]?\s*([\d.]+)\s*亿?",))
    if "换手率" in line:
        record.turnover_rate_pct = _extract_number(line, (r"换手率[:：]?\s*([\d.]+)%?",))
    if "连板高度" in line or "高度" in line:
        height = _extract_number(line, (r"连板高度[:：]?\s*(\d+)", r"高度[:：]?\s*(\d+)"))
        record.height = max(1, int(height))
    if "题材" in line:
        record.theme = _extract_text_value(line, "题材") or record.theme
    if "行业" in line:
        record.industry = _extract_text_value(line, "行业") or record.industry


def _match_section(line: str, aliases: dict[str, list[str]]) -> str | None:
    for key, names in aliases.items():
        if any(name in line for name in names):
            return key
    return None


def _looks_like_new_section(line: str) -> bool:
    return bool(re.match(r"^(#{1,6}\s*)?(固定名单|数据口径|热门板块|敏感舆情|已纳入模型|排序|题材聚合|个股执行|最终结论|逐股详细)", line))


def _line_has_detail_hint(line: str) -> bool:
    hints = ("封板质量", "封单占成交", "最高封单", "涨停成交额", "换手率", "连板高度", "题材", "行业")
    return any(hint in line for hint in hints)


def _extract_number(line: str, patterns: Iterable[str]) -> float:
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return float(match.group(1))
    return 0.0


def _extract_text_value(line: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}[:：]\s*([^；;，,\n]+)", line)
    return match.group(1).strip() if match else ""


def _first_matching_token(line: str, tokens: Iterable[str]) -> str:
    return next((token for token in sorted(tokens, key=len, reverse=True) if token in line), "中性")


def _cap_score(value: float, cap: float) -> float:
    if cap <= 0:
        return 0.0
    return max(0.0, min(100.0, value / cap * 100.0))

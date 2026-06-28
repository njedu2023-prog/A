from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .models import ReportMetadata, SectorRotation, StockRecord

CODE_RE = re.compile(r"(?P<code>(?:00|30|60|68|43|83|87)\d{4})(?:\.(?:SH|SZ|BJ))?\s*(?P<name>[\u4e00-\u9fa5A-Za-z0-9*]+)?")


def parse_report_text(text: str, config: dict[str, Any]) -> tuple[list[StockRecord], list[SectorRotation]]:
    records: dict[str, StockRecord] = {}
    lines = _clean_lines(text)
    _parse_lists(lines, records, config)
    _parse_stock_details(lines, records)
    _parse_ths_limit_table_signals(lines, records, config)
    _parse_ocr_crop_details(text, records, config)
    _parse_ocr_list_themes(lines, records)
    _parse_sensitive_events(lines, records, config)
    sectors = _parse_sector_rotation(lines, config)
    sectors.extend(_parse_ocr_sector_rotation(lines, config))
    return sorted(records.values(), key=lambda item: item.code), _dedupe_sectors(sectors)


def parse_report_metadata(text: str) -> ReportMetadata:
    lines = _clean_lines(text)
    auction_buy_date = _parse_auction_buy_date(lines)
    return ReportMetadata(auction_buy_date=auction_buy_date)


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r", "\n").splitlines() if line.strip()]


def _parse_auction_buy_date(lines: list[str]) -> str:
    patterns = (
        r"(?:竞价|竟价)\s*买入\s*时间\s*[:：]?\s*(?P<value>(?:20\d{2}[/-])?\d{1,2}[/-]\d{1,2}(?:\s+\d{1,2}:\d{2})?)",
        r"(?:竞价|竟价)\s*买入\s*日\s*[:：]?\s*(?P<value>(?:20\d{2}[/-])?\d{1,2}[/-]\d{1,2})",
        r"(?:竞价|竟价).*?买入.*?(?P<value>(?:20\d{2}[/-])?\d{1,2}[/-]\d{1,2})",
    )
    for line in lines[:80]:
        for pattern in patterns:
            match = re.search(pattern, line)
            if match:
                return match.group("value").replace("/", "-")
    return ""


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


def _parse_ocr_crop_details(text: str, records: dict[str, StockRecord], config: dict[str, Any]) -> None:
    blocks = re.findall(r"\n--- CROP .+? ---\n(.*?)(?=\n--- CROP |\Z)", text, flags=re.S)
    for block in blocks:
        lines = _clean_lines(block)
        code_index = _first_code_line_index(lines)
        if code_index is None:
            continue
        code_match = CODE_RE.search(lines[code_index])
        if not code_match:
            continue
        record = _get_or_create(records, code_match.group("code"), _name_before_code(lines, code_index))
        _apply_ocr_crop_block(record, lines, config)


def _parse_ocr_list_themes(lines: list[str], records: dict[str, StockRecord]) -> None:
    for index, line in enumerate(lines):
        match = CODE_RE.search(line)
        if not match:
            continue
        record = records.get(match.group("code"))
        if not record or record.theme:
            continue
        candidates = [line, *lines[index + 1 : index + 14]]
        for candidate in candidates:
            theme = _theme_candidate_from_list_line(candidate)
            if theme:
                record.theme = theme
                break


def _parse_sensitive_events(lines: list[str], records: dict[str, StockRecord], config: dict[str, Any]) -> None:
    aliases = config["parsing"]["sensitive_event_aliases"]
    in_section = False
    for line in lines:
        if any(token in line for token in ("敏感舆情", "已纳入模型的敏感舆情", "公告模块")):
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


def _parse_ocr_sector_rotation(lines: list[str], config: dict[str, Any]) -> list[SectorRotation]:
    sectors: dict[str, SectorRotation] = {}
    for index, line in enumerate(lines):
        candidate = _parse_ths_sector_candidate(lines, index, config)
        if candidate is None:
            continue
        if candidate.name in sectors:
            continue
        sectors[candidate.name] = candidate
    return list(sectors.values())


def _parse_ths_sector_candidate(lines: list[str], index: int, config: dict[str, Any]) -> SectorRotation | None:
    line = lines[index]
    if _is_noise_sector_line(line):
        return None
    window = " ".join(lines[index : index + 5])
    name = _extract_ths_sector_name(line)
    if not name:
        return None
    limit_count = int(_extract_number(window, (r"(\d+)\s*家涨停",)))
    heat_value = _extract_heat_value(window)
    if limit_count == 0 and heat_value == 0.0 and "上榜" not in window:
        return None
    heat_token = _heat_token_from_evidence(limit_count, heat_value)
    return SectorRotation(
        name=name,
        net_flow_amount_yi=0.0,
        limit_up_count=limit_count,
        heat_token=heat_token,
        brs=calculate_brs(0.0, limit_count, heat_token, config),
        heat_value=heat_value,
    )


def _extract_ths_sector_name(line: str) -> str:
    text = line.strip()
    patterns = (
        r"^\d{1,2}\s*(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()]+)[［\[\(（].*(?:上榜|家涨停)",
        r"^\d{1,2}\s*(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()]+?)(?:\d+天\d+次上榜|连续\d+天上榜|首次上榜|天上榜|次上榜)",
        r"^(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()]+)[［\[\(（].*(?:上榜|家涨停)",
        r"^(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()]+?)(?:\d+天\d+次上榜|连续\d+天上榜|首次上榜|天上榜|次上榜)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _clean_sector_name(match.group("name"))
    return ""


def _clean_sector_name(name: str) -> str:
    cleaned = re.sub(r"^\d+", "", name).strip("[]［］()（） 〉>")
    cleaned = re.sub(r"(?:连续\d+天上榜|\d+天\d+次上榜|首次上榜|\d+家涨停).*$", "", cleaned)
    return cleaned.strip()


def _extract_heat_value(text: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(万)?\s*热度", text)
    if not match:
        return 0.0
    value = float(match.group(1))
    return value * 10000.0 if match.group(2) else value


def _heat_token_from_evidence(limit_count: int, heat_value: float) -> str:
    token_scores = {"弱": 0, "中性": 1, "活跃": 2, "强": 3, "极强": 4}
    by_limit = "强" if limit_count >= 7 else "活跃" if limit_count >= 3 else "中性"
    by_heat = "极强" if heat_value >= 20000 else "强" if heat_value >= 10000 else "活跃" if heat_value >= 5000 else "中性"
    return max((by_limit, by_heat), key=lambda token: token_scores[token])


def _is_noise_sector_line(line: str) -> bool:
    noise_tokens = (
        "同花顺",
        "全球",
        "A股",
        "港股",
        "美股",
        "期货",
        "ETF",
        "大盘",
        "板块",
        "个股",
        "看盘广场",
        "概念板块",
        "行业板块",
        "指数板块",
        "排名",
        "涨幅",
        "热度",
        "更多",
        "首页",
        "行情",
        "自选",
        "交易",
        "资讯",
        "理财",
    )
    return line.strip() in noise_tokens


def _dedupe_sectors(sectors: list[SectorRotation]) -> list[SectorRotation]:
    merged: dict[str, SectorRotation] = {}
    for sector in sectors:
        current = merged.get(sector.name)
        if current is None or _sector_evidence_rank(sector) > _sector_evidence_rank(current):
            merged[sector.name] = sector
    return list(merged.values())


def _sector_evidence_rank(sector: SectorRotation) -> tuple[bool, bool, float, str]:
    return (sector.limit_up_count > 0, sector.heat_value > 0, sector.brs, sector.name)


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
        name = match.group("name") or ""
        if not _is_valid_stock_name(name):
            continue
        record = _get_or_create(records, match.group("code"), name)
        record.list_sources.add(source)


def _get_or_create(records: dict[str, StockRecord], code: str, name: str) -> StockRecord:
    record = records.setdefault(code, StockRecord(code=code))
    if name and not record.name:
        record.name = name
    return record


def _is_valid_stock_name(name: str) -> bool:
    if not name:
        return False
    upper_name = name.upper()
    return "ETF" not in upper_name and not any(token in name for token in ("指数", "期货", "可转债"))


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


def _parse_ths_limit_table_signals(lines: list[str], records: dict[str, StockRecord], config: dict[str, Any]) -> None:
    for index, line in enumerate(lines):
        match = CODE_RE.search(line)
        if not match:
            continue
        record = _get_or_create(records, match.group("code"), match.group("name") or "")
        window = lines[index + 1 : index + 12]
        times = _limit_times_from_window(window)
        if len(times) < 2:
            continue
        record.first_limit_up_time = record.first_limit_up_time or times[0]
        record.final_limit_up_time = record.final_limit_up_time or times[1]
        if record.board_quality == "未知":
            record.board_quality = infer_board_quality_from_limit_times(times[0], times[1], config)


def _limit_times_from_window(lines: list[str]) -> list[str]:
    times: list[str] = []
    for line in lines:
        for match in re.finditer(r"\b\d{2}:\d{2}(?::\d{2})?\b", line):
            times.append(_normalize_time(match.group(0)))
            if len(times) >= 2:
                return times
    return times


def infer_board_quality_from_limit_times(first_time: str, final_time: str, config: dict[str, Any]) -> str:
    inference = config["iqs"]["board_quality_inference"]
    first = _time_to_seconds(first_time)
    final = _time_to_seconds(final_time)
    gap = abs(final - first)
    if gap <= int(inference["same_time_gap_seconds"]):
        labels = inference["same_time_labels"]
        if first <= _time_to_seconds(inference["one_word_cutoff"]):
            return labels["one_word"]
        if first <= _time_to_seconds(inference["strong_cutoff"]):
            return labels["strong"]
        if first <= _time_to_seconds(inference["high_cutoff"]):
            return labels["high"]
        if first <= _time_to_seconds(inference["medium_cutoff"]):
            return labels["medium"]
        return labels["weak"]
    labels = inference["reseal_labels"]
    if final > first and gap <= int(inference["short_reseal_gap_seconds"]):
        return labels["short"]
    return labels["long"]


def _normalize_time(value: str) -> str:
    parts = value.split(":")
    if len(parts) == 2:
        return f"{parts[0]}:{parts[1]}:00"
    return value


def _time_to_seconds(value: str) -> int:
    normalized = _normalize_time(str(value).strip())
    parts = normalized.split(":")
    if len(parts) != 3:
        return 0
    hour, minute, second = (int(float(part)) for part in parts)
    return hour * 3600 + minute * 60 + second


def _apply_ocr_crop_block(record: StockRecord, lines: list[str], config: dict[str, Any]) -> None:
    if not record.name:
        code_index = _first_code_line_index(lines)
        if code_index is not None:
            record.name = _name_before_code(lines, code_index)
    record.turnover_rate_pct = _number_after_label(lines, "换", percent=True) or record.turnover_rate_pct
    record.order_to_turnover_pct = _number_after_label(lines, "封单占成交", percent=True) or record.order_to_turnover_pct
    record.max_seal_order_yi = _amount_yi_after_label(lines, "最高封单额") or record.max_seal_order_yi
    record.limit_up_amount_yi = _amount_yi_after_label(lines, "涨停成交额") or record.limit_up_amount_yi
    height = _height_from_lines(lines)
    if height:
        record.height = height
    if _is_n_high_pattern(lines):
        record.route_override = config["height_routes"]["default"]
    theme = _theme_from_lines(lines)
    if theme:
        record.theme = theme
    _apply_sensitive_aliases(record, " ".join(lines), config)


def _match_section(line: str, aliases: dict[str, list[str]]) -> str | None:
    normalized_line = _normalize_label(line)
    for key, names in aliases.items():
        if any(_normalize_label(name) in normalized_line for name in names):
            return key
    return None


def _looks_like_new_section(line: str) -> bool:
    return bool(re.match(r"^(#{1,6}\s*)?(--- PAGE|固定名单|数据口径|热门板块|敏感舆情|已纳入模型|排序|题材聚合|个股执行|最终结论|逐股详细|每支股票)", line))


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


def _normalize_label(value: str) -> str:
    return re.sub(r"[\s\-_]", "", value).lower()


def _first_code_line_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if CODE_RE.search(line):
            return index
    return None


def _name_before_code(lines: list[str], code_index: int) -> str:
    noise = ("同花顺", "App", "國", "四", "自选", "全球")
    for line in reversed(lines[:code_index]):
        cleaned = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9*]", "", line)
        if len(cleaned) >= 2 and re.search(r"[\u4e00-\u9fa5]{2,}", cleaned) and not any(token in cleaned for token in noise):
            return cleaned
    return ""


def _number_after_label(lines: list[str], label: str, percent: bool = False) -> float:
    value = _raw_value_after_label(lines, label)
    if not value:
        return 0.0
    pattern = r"([-+]?\d+(?:[.,]\d+)?)\s*[％%]?" if percent else r"([-+]?\d+(?:[.,]\d+)?)"
    match = re.search(pattern, value)
    return float(match.group(1).replace(",", ".")) if match else 0.0


def _amount_yi_after_label(lines: list[str], label: str) -> float:
    value = _raw_value_after_label(lines, label)
    if not value:
        return 0.0
    return _amount_to_yi(value)


def _raw_value_after_label(lines: list[str], label: str) -> str:
    for index, line in enumerate(lines):
        if label not in line:
            continue
        rest = line.split(label, 1)[1].strip(" ：:%％")
        if re.search(r"\d", rest):
            return rest
        for candidate in lines[index + 1 : index + 4]:
            if re.search(r"\d", candidate):
                return candidate
    return ""


def _amount_to_yi(value: str) -> float:
    match = re.search(r"([-+]?\d+(?:[.,]\d+)?)\s*([万亿])?", value)
    if not match:
        return 0.0
    amount = float(match.group(1).replace(",", "."))
    unit = match.group(2)
    if unit == "万":
        return round(amount / 10000.0, 4)
    return amount


def _height_from_lines(lines: list[str]) -> int:
    for line in lines:
        if "首板" in line:
            return 1
        match = re.search(r"(\d+)\s*连板", line)
        if match:
            return int(match.group(1))
        match = re.search(r"\d+\s*天\s*(\d+)\s*板", line)
        if match:
            return int(match.group(1))
    return 0


def _is_n_high_pattern(lines: list[str]) -> bool:
    for line in lines:
        match = re.search(r"(\d+)\s*天\s*(\d+)\s*板", line)
        if match and int(match.group(1)) > int(match.group(2)) and int(match.group(2)) >= 4:
            return True
    return False


def _theme_from_lines(lines: list[str]) -> str:
    for line in lines:
        if "异动解读" not in line:
            continue
        theme = re.sub(r"^.*?异动解读[:：]", "", line)
        theme = re.split(r"[。；;]|1[、.．]|据20", theme, maxsplit=1)[0]
        return theme.strip(" •⋯…×")
    return ""


def _theme_candidate_from_list_line(line: str) -> str:
    if not re.search(r"[\u4e00-\u9fa5]", line) or not re.search(r"[+＋、]", line):
        return ""
    if any(token in line for token in ("涨停", "时间", "涨幅", "代码", "主力", "封成比")):
        return ""
    text = re.sub(r"^.*?(?:万|亿)\s*", "", line).strip()
    text = re.sub(r"^[\d.+％%\s:-]+", "", text).strip()
    text = text.strip(" •⋯…")
    if len(text) < 4 or not re.search(r"[\u4e00-\u9fa5]", text):
        return ""
    return text


def _apply_sensitive_aliases(record: StockRecord, text: str, config: dict[str, Any]) -> None:
    for event_key, aliases in config["parsing"]["sensitive_event_aliases"].items():
        if any(alias in text for alias in aliases) and event_key not in record.sensitive_events:
            record.sensitive_events.append(event_key)

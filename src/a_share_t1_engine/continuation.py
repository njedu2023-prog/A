from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from .models import ReportMetadata, ScoredStock, SectorRotation


@dataclass(frozen=True)
class ContinuationScore:
    item: ScoredStock
    value_score: int
    t_day_probability: float
    t1_probability: float
    conclusion: str
    s1_limit_quality: float
    s2_auction_acceptance: float
    s3_theme_position: float
    s4_sentiment_cycle: float
    s5_chip_structure: float
    s6_seat_funds: float
    s7_catalyst: float
    s8_risk_penalty: float
    t1_sellability: float
    core_bull: str
    core_risk: str
    auction_condition: str
    sell_plan: str


def score_continuation(scored: list[ScoredStock], sectors: list[SectorRotation], config: dict[str, Any]) -> list[ContinuationScore]:
    rows = [_score_one(item, sectors, config) for item in scored]
    return sorted(rows, key=lambda row: (-row.value_score, -row.t_day_probability, -row.t1_probability, row.item.stock.code))


def render_continuation_html(
    rows: list[ContinuationScore],
    sectors: list[SectorRotation],
    config: dict[str, Any],
    metadata: ReportMetadata | None = None,
) -> str:
    metadata = metadata or ReportMetadata()
    body = [
        _section("排序表", _ranking_table(rows)),
        _section("简短结论", _short_conclusion(rows)),
        _section("时间轴说明", _timeline_html(metadata)),
        _section("数据缺失说明", _missing_data_html(rows)),
        _section("三组重叠标注", _overlap_html(rows)),
        _section("热门板块资金轮动结论", _sector_summary_html(sectors)),
    ]
    return "".join(body)


def _score_one(item: ScoredStock, sectors: list[SectorRotation], config: dict[str, Any]) -> ContinuationScore:
    cfg = config["continuation_v1"]
    stock = item.stock
    s1 = item.iqs
    s2 = _auction_score(item, config)
    s3 = item.tss
    s4 = _sentiment_cycle_score(item)
    s5 = _chip_score(item)
    s6 = _seat_funds_score(item)
    s7 = _catalyst_score(item)
    s8 = _risk_penalty(item, config)
    sellability = _sellability_score(item, s8)
    weights = cfg["weights"]
    raw = (
        s1 * float(weights["s1_limit_quality"])
        + s2 * float(weights["s2_auction_acceptance"])
        + s3 * float(weights["s3_theme_position"])
        + s4 * float(weights["s4_sentiment_cycle"])
        + s5 * float(weights["s5_chip_structure"])
        + s6 * float(weights["s6_seat_funds"])
        + s7 * float(weights["s7_catalyst"])
        + sellability * float(weights["t1_sellability"])
        - s8 * float(weights["s8_risk_penalty"])
    )
    if not _has_auction_data(item):
        raw -= float(cfg["missing_data_confidence_penalty"])
    value_score = int(round(max(0.0, min(100.0, raw))))
    t_day = _probability(value_score, s8, "t_day", config)
    t1 = _probability(value_score, s8, "t1", config)
    conclusion = _conclusion(value_score, s8, config)
    return ContinuationScore(
        item=item,
        value_score=value_score,
        t_day_probability=t_day,
        t1_probability=t1,
        conclusion=conclusion,
        s1_limit_quality=s1,
        s2_auction_acceptance=s2,
        s3_theme_position=s3,
        s4_sentiment_cycle=s4,
        s5_chip_structure=s5,
        s6_seat_funds=s6,
        s7_catalyst=s7,
        s8_risk_penalty=s8,
        t1_sellability=sellability,
        core_bull=_core_bull(item),
        core_risk=_core_risk(item, s8),
        auction_condition=_auction_condition(item, s8),
        sell_plan=_sell_plan(item),
    )


def _auction_score(item: ScoredStock, config: dict[str, Any]) -> float:
    stock = item.stock
    if _has_auction_data(item):
        score = 50.0 + stock.auction_change_pct * 2.0 + min(25.0, stock.auction_amount_yi * 8.0) + min(15.0, stock.auction_seal_order_yi * 6.0)
        if stock.auction_change_pct > 8:
            score -= 18.0
        return round(max(0.0, min(100.0, score)), 1)
    cfg = config["continuation_v1"]["static_auction"]
    score = (
        item.iqs * float(cfg["iqs_weight"])
        + item.tss * float(cfg["tss_weight"])
        + _board_quality_value(stock.board_quality) * float(cfg["board_quality_weight"])
        + _turnover_static_value(stock.turnover_rate_pct) * float(cfg["turnover_weight"])
    )
    return round(max(0.0, min(100.0, score)), 1)


def _sentiment_cycle_score(item: ScoredStock) -> float:
    base = item.tss * 0.62 + item.sas * 0.28 + item.probability * 0.10
    if item.stock.height >= 5:
        base -= 12.0
    elif item.stock.height <= 2:
        base += 5.0
    return round(max(0.0, min(100.0, base)), 1)


def _chip_score(item: ScoredStock) -> float:
    turnover = item.stock.turnover_rate_pct
    if turnover <= 0:
        return 45.0
    if turnover <= 8:
        score = 72.0
    elif turnover <= 15:
        score = 62.0
    elif turnover <= 22:
        score = 42.0
    else:
        score = 28.0
    if item.stock.order_to_turnover_pct >= 20:
        score += 8.0
    if item.stock.height >= 5:
        score -= 12.0
    return round(max(0.0, min(100.0, score)), 1)


def _seat_funds_score(item: ScoredStock) -> float:
    if item.stock.auction_amount_yi <= 0 and item.stock.opening_5m_amount_yi <= 0:
        return 45.0
    return round(max(0.0, min(100.0, 45.0 + item.stock.auction_amount_yi * 8.0 + item.stock.opening_5m_amount_yi * 4.0)), 1)


def _catalyst_score(item: ScoredStock) -> float:
    score = 48.0 + item.event_score * 5.0
    if item.tss >= 60:
        score += 12.0
    if item.stock.sentiment_direction == "positive":
        score += 8.0
    elif item.stock.sentiment_direction == "negative":
        score -= 12.0
    return round(max(0.0, min(100.0, score)), 1)


def _risk_penalty(item: ScoredStock, config: dict[str, Any]) -> float:
    stock = item.stock
    cfg = config["continuation_v1"]["risk"]
    risk = 12.0
    if stock.turnover_rate_pct > float(cfg["high_turnover_pct"]):
        risk += 18.0
    if stock.turnover_rate_pct > float(cfg["extreme_turnover_pct"]):
        risk += 15.0
    if stock.order_to_turnover_pct and stock.order_to_turnover_pct < float(cfg["weak_order_pct"]):
        risk += 18.0
    if stock.height >= int(cfg["high_height"]):
        risk += 18.0
    if any(stock.code.startswith(prefix) for prefix in cfg["high_volatility_prefixes"]):
        risk += 14.0
    if any(token in stock.board_quality for token in ("弱", "炸", "分歧")):
        risk += 12.0
    if item.event_score < 0:
        risk += abs(item.event_score) * 6.0
    if item.tss < 40:
        risk += 10.0
    return round(max(0.0, min(100.0, risk)), 1)


def _sellability_score(item: ScoredStock, risk: float) -> float:
    score = 72.0 - risk * 0.45 + (item.tss - 50.0) * 0.25
    if item.stock.limit_up_amount_yi > 8:
        score -= 8.0
    if item.stock.height <= 2:
        score += 6.0
    return round(max(0.0, min(100.0, score)), 1)


def _probability(score: int, risk: float, kind: str, config: dict[str, Any]) -> float:
    cfg = config["continuation_v1"]["probability"]
    if kind == "t_day":
        value = float(cfg["t_day_base"]) + score * float(cfg["t_day_score_weight"]) - risk * float(cfg["risk_weight"])
    else:
        value = float(cfg["t1_base"]) + score * float(cfg["t1_score_weight"]) - risk * float(cfg["risk_weight"])
    return round(max(3.0, min(88.0, value)), 0)


def _conclusion(score: int, risk: float, config: dict[str, Any]) -> str:
    if risk >= 80:
        return "剔除"
    thresholds = config["continuation_v1"]["conclusion_thresholds"]
    if score >= int(thresholds["strong"]):
        return "强"
    if score >= int(thresholds["medium_strong"]):
        return "中偏强"
    if score >= int(thresholds["medium"]):
        return "中"
    if score >= int(thresholds["medium_weak"]):
        return "中偏弱"
    if score >= int(thresholds["weak"]):
        return "弱"
    return "剔除"


def _has_auction_data(item: ScoredStock) -> bool:
    stock = item.stock
    return any(value > 0 for value in (stock.auction_amount_yi, stock.auction_seal_order_yi, stock.opening_5m_amount_yi)) or stock.auction_change_pct != 0


def _board_quality_value(value: str) -> float:
    if "一字" in value:
        return 82.0
    if "强" in value or "高" in value:
        return 78.0
    if "中" in value:
        return 58.0
    if "弱" in value or "分歧" in value:
        return 34.0
    if "炸" in value:
        return 18.0
    return 50.0


def _turnover_static_value(value: float) -> float:
    if value <= 0:
        return 45.0
    if value <= 8:
        return 75.0
    if value <= 15:
        return 62.0
    if value <= 22:
        return 42.0
    return 25.0


def _core_bull(item: ScoredStock) -> str:
    stock = item.stock
    parts = [f"封板{stock.board_quality}", f"题材:{stock.theme or '数据缺失'}", f"TSS {item.tss:.1f}"]
    if stock.overlap_flag == "是":
        parts.append(f"【{'+'.join(sorted(stock.list_sources))}重叠】")
    return "；".join(parts)


def _core_risk(item: ScoredStock, risk: float) -> str:
    stock = item.stock
    risks = [f"风险分{risk:.1f}"]
    if not _has_auction_data(item):
        risks.append("缺少T日真实竞价")
    if stock.height >= 4:
        risks.append("高位连板")
    if stock.turnover_rate_pct > 15:
        risks.append("高换手")
    if stock.order_to_turnover_pct and stock.order_to_turnover_pct < 5:
        risks.append("封成比弱")
    if item.event_score < 0:
        risks.append("敏感信息扣分")
    return "；".join(risks)


def _auction_condition(item: ScoredStock, risk: float) -> str:
    if risk >= 70:
        return "只接受低于预期不过热且真实承接强；高开爆量取消"
    if item.stock.height >= 4:
        return "高位票只看强承接，不接受竞价跳水"
    return "9:20后真实买盘稳定，竞价成交不异常放大"


def _sell_plan(item: ScoredStock) -> str:
    if item.stock.height >= 4:
        return "T+1只看强封，炸板不回封直接卖"
    if item.tss >= 60:
        return "高开强承接可看冲板，冲板失败卖"
    if item.stock.turnover_rate_pct > 15:
        return "高开爆量弱承接优先卖"
    return "平开拉升等第一波冲高，弱反抽卖"


def _section(title: str, body: str) -> str:
    return f'<section class="system-section"><h2>{escape(title)}</h2>{body}</section>'


def _table(headers: list[str], rows: list[list[str]], top_rows: int = 0) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    if not rows:
        rows = [["-" for _ in headers]]
    body = "".join(
        f'<tr{_row_class(index, top_rows)}>'
        + "".join(f'<td{_cell_class(headers[index])}>{cell}</td>' for index, cell in enumerate(row))
        + "</tr>"
        for index, row in enumerate(rows)
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _row_class(index: int, top_rows: int) -> str:
    return ' class="top-row"' if index < top_rows else ""


def _cell_class(header: str) -> str:
    return ' class="stock-name-cell"' if header in {"名称", "股票名称"} else ""


def _timeline_html(metadata: ReportMetadata) -> str:
    if metadata.auction_buy_date:
        sell_text = f"T+1卖出/验证日期为 {escape(metadata.t1_sell_date)}。" if metadata.t1_sell_date else "T+1为下一个A股交易日卖出窗口。"
        return (
            f'<p class="muted">本次按最新上传文档处理。PDF写明：竞价买入时间 {escape(metadata.auction_buy_date)}。'
            f'D1为候选涨停日，{escape(metadata.auction_buy_date)} 为竞价/开盘买入日，{sell_text}'
            "</p>"
        )
    return "<p class=\"muted\">本次按最新上传文档处理。D1为候选涨停日，T为竞价/开盘买入日，T+1为下一个A股交易日卖出窗口。交易日历和涨跌幅规则将在外部数据层补齐。</p>"


def _missing_data_html(rows: list[ContinuationScore]) -> str:
    missing_auction = sum(1 for row in rows if not _has_auction_data(row.item))
    return f"<p class=\"callout\">当前缺少T日真实竞价数据、龙虎榜席位数据和完整T+1行情，以下为静态排序。竞价缺失股票数：{missing_auction}。</p>"


def _overlap_html(rows: list[ContinuationScore]) -> str:
    groups = {
        "Top10 ∩ Top-Decision": [],
        "Top10 ∩ Premium": [],
        "Top-Decision ∩ Premium": [],
        "三组共同重叠": [],
    }
    for row in rows:
        sources = row.item.stock.list_sources
        label = f"{row.item.stock.name}({row.item.stock.code})"
        if {"Top 10", "Top-Decision", "Premium"}.issubset(sources):
            groups["三组共同重叠"].append(label)
        elif {"Top 10", "Top-Decision"}.issubset(sources):
            groups["Top10 ∩ Top-Decision"].append(label)
        elif {"Top 10", "Premium"}.issubset(sources):
            groups["Top10 ∩ Premium"].append(label)
        elif {"Top-Decision", "Premium"}.issubset(sources):
            groups["Top-Decision ∩ Premium"].append(label)
    lines = "".join(f"<p><strong>{escape(name)}：</strong>{escape('、'.join(values) if values else '无明显重叠')}。重叠只标注，不额外加权。</p>" for name, values in groups.items())
    return lines


def _sector_summary_html(sectors: list[SectorRotation]) -> str:
    ordered = sorted(sectors, key=lambda sector: (-sector.brs, sector.name))
    strong = "、".join(sector.name for sector in ordered[:5]) or "数据缺失"
    weak = "、".join(sector.name for sector in sorted(sectors, key=lambda sector: (sector.brs, sector.name))[:5]) or "数据缺失"
    return f"<p>本批优先方向：{escape(strong)}。</p><p>弱化方向：{escape(weak)}。</p>"


def _ranking_table(rows: list[ContinuationScore]) -> str:
    table_rows = []
    for index, row in enumerate(rows, start=1):
        stock = row.item.stock
        table_rows.append([
            str(index),
            escape(stock.code),
            escape(stock.name),
            str(row.value_score),
            f"{row.t_day_probability:.0f}%",
            f"{row.t1_probability:.0f}%",
            escape(row.core_bull),
            escape(row.core_risk),
            escape(row.auction_condition),
            escape(row.sell_plan),
            escape(row.conclusion),
        ])
    return _table(["排名", "代码", "名称", "连续大涨价值分", "T日大涨概率", "T+1继续概率", "核心看多原因", "核心风险", "竞价确认条件", "T+1卖出计划", "结论"], table_rows, top_rows=4)


def _short_conclusion(rows: list[ContinuationScore]) -> str:
    primary = [row for row in rows if row.conclusion in {"强", "中偏强"}][:3]
    secondary = [row for row in rows if row.conclusion == "中"][:3]
    weak = [row for row in rows if row.conclusion in {"中偏弱", "弱", "剔除"}][:5]
    return (
        f"<p><strong>主观察：</strong>{escape(_names(primary))}。</p>"
        f"<p><strong>次级观察：</strong>{escape(_names(secondary))}。</p>"
        f"<p><strong>弱化/剔除：</strong>{escape(_names(weak))}。</p>"
        "<p class=\"muted\">本批核心打法应围绕竞价真实承接确认，弱化高位、高换手、弱封和外部敏感信息未核清的标的。</p>"
    )


def _names(rows: list[ContinuationScore]) -> str:
    return "、".join(f"{row.item.stock.name}({row.item.stock.code})" for row in rows) or "-"

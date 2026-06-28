from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from .models import ReportMetadata, ScoredStock, SectorRotation


@dataclass(frozen=True)
class OvernightScore:
    item: ScoredStock
    buy_too_high_risk: float
    t1_liquidity_risk: float
    fracture_risk: float
    acceptance_score: float
    p_fill: float
    p_up: float
    e_gain: float
    e_loss: float
    ev: float
    market_grade: str
    theme_grade: str
    position_grade: str
    buyability: str
    t1_expectation: str
    overnight_conclusion: str
    core_basis: str
    order_line: str
    no_chase_line: str


def score_overnight(scored: list[ScoredStock], sectors: list[SectorRotation], config: dict[str, Any]) -> list[OvernightScore]:
    results = [_score_one(item, sectors, config) for item in scored]
    return sorted(results, key=lambda row: (row.fracture_risk, -row.acceptance_score, -row.ev, row.item.stock.code))


def render_overnight_html(
    rows: list[OvernightScore],
    sectors: list[SectorRotation],
    config: dict[str, Any],
    metadata: ReportMetadata | None = None,
) -> str:
    ev_rows = sorted(rows, key=lambda row: (-row.ev, row.fracture_risk, row.item.stock.code))
    metadata = metadata or ReportMetadata()
    sections = [
        _section("隔夜单 / EV单前4名单", _top4_summary_html(rows, ev_rows)),
        _section("隔夜单", _overnight_table(rows)),
        _section("隔夜市价单分析说明", _overnight_detail_html(rows[:10])),
        _section("EV 排名总表", _ev_table(ev_rows)),
        _section("EV 分表依据分析说明", _ev_detail_html(ev_rows[:10])),
        _section("最终执行结论", _final_execution_html(rows)),
        _section("文档核对与时间轴", _timeline_html(metadata)),
        _section("热门板块资金轮动重点结论", _sector_summary_html(sectors)),
        _section("舆情及敏感信息纳入", _event_html(rows)),
        _section("三组名单核对", _list_html(rows)),
        _section("三组重叠票说明", _overlap_html(rows)),
        _section("股票与热门板块资金轮动映射", _theme_mapping_html(rows)),
    ]
    return "".join(sections)


def _score_one(item: ScoredStock, sectors: list[SectorRotation], config: dict[str, Any]) -> OvernightScore:
    cfg = config["overnight_v35"]
    buy_risk = _buy_too_high_risk(item, config)
    liquidity_risk = _t1_liquidity_risk(item, config)
    fracture_risk = round(
        buy_risk * float(cfg["risk_weights"]["buy_too_high"])
        + liquidity_risk * float(cfg["risk_weights"]["t1_liquidity"]),
        1,
    )
    acceptance = _acceptance_score(item, fracture_risk, config)
    p_fill = _p_fill(item, fracture_risk, config)
    p_up = _p_up(item, config)
    e_gain = _e_gain(item, config)
    e_loss = _e_loss(fracture_risk, config)
    ev_cfg = cfg["ev"]
    ev = round(
        (p_fill / 100.0) * ((p_up / 100.0) * e_gain - (1.0 - p_up / 100.0) * e_loss)
        - fracture_risk * float(ev_cfg["fracture_penalty_weight"]),
        2,
    )
    return OvernightScore(
        item=item,
        buy_too_high_risk=buy_risk,
        t1_liquidity_risk=liquidity_risk,
        fracture_risk=fracture_risk,
        acceptance_score=acceptance,
        p_fill=p_fill,
        p_up=p_up,
        e_gain=e_gain,
        e_loss=e_loss,
        ev=ev,
        market_grade=_market_grade(item),
        theme_grade=_theme_grade(item),
        position_grade=_position_grade(item),
        buyability=_buyability(fracture_risk),
        t1_expectation=_t1_expectation(liquidity_risk),
        overnight_conclusion=_overnight_conclusion(fracture_risk),
        core_basis=_core_basis(item, fracture_risk),
        order_line=_order_line(fracture_risk, config),
        no_chase_line=_no_chase_line(fracture_risk, config),
    )


def _buy_too_high_risk(item: ScoredStock, config: dict[str, Any]) -> float:
    stock = item.stock
    cfg = config["overnight_v35"]["buy_risk"]
    penalties = cfg["penalties"]
    risk = 18.0
    if stock.turnover_rate_pct > float(cfg["turnover_10"]):
        risk += float(penalties["turnover_10"])
    if stock.turnover_rate_pct > float(cfg["turnover_15"]):
        risk += float(penalties["turnover_15"])
    if stock.turnover_rate_pct > float(cfg["turnover_20"]):
        risk += float(penalties["turnover_20"])
    if stock.order_to_turnover_pct and stock.order_to_turnover_pct < float(cfg["weak_order_pct"]):
        risk += float(penalties["weak_order"])
    if stock.height >= int(cfg["high_height"]):
        risk += float(penalties["high_height"])
    if stock.height >= int(cfg["extreme_height"]):
        risk += float(penalties["extreme_height"])
    if any(token in stock.board_quality for token in ("弱", "炸", "分歧")):
        risk += float(penalties["weak_board"])
    if "一字" in stock.board_quality:
        risk += float(penalties["one_word_buy_chase"])
    if _is_high_volatility_board(stock.code):
        risk += float(penalties["high_volatility_board"])
    return round(min(100.0, risk), 1)


def _t1_liquidity_risk(item: ScoredStock, config: dict[str, Any]) -> float:
    stock = item.stock
    cfg = config["overnight_v35"]["liquidity_risk"]
    penalties = cfg["penalties"]
    risk = 16.0
    if item.tss < float(cfg["low_tss"]):
        risk += float(penalties["low_tss"])
    if stock.order_to_turnover_pct and stock.order_to_turnover_pct < float(cfg["weak_order_pct"]):
        risk += float(penalties["weak_order"])
    if stock.turnover_rate_pct > float(cfg["high_turnover_pct"]):
        risk += float(penalties["high_turnover"])
    if stock.limit_up_amount_yi > float(cfg["high_amount_yi"]):
        risk += float(penalties["high_amount"])
    if stock.height >= 4:
        risk += float(penalties["high_height"])
    if item.event_score < 0:
        risk += abs(item.event_score) * float(penalties["negative_event_unit"])
    if _is_high_volatility_board(stock.code):
        risk += float(penalties["high_volatility_board"])
    return round(min(100.0, risk), 1)


def _acceptance_score(item: ScoredStock, fracture_risk: float, config: dict[str, Any]) -> float:
    weights = config["overnight_v35"]["two_day_acceptance"]
    stock = item.stock
    auction_quality = max(0.0, min(100.0, item.iqs - fracture_risk * 0.25))
    t_day = max(0.0, min(100.0, (item.iqs * 0.45 + item.tss * 0.35 + item.sas * 0.20)))
    t1 = max(0.0, min(100.0, (item.tss * 0.50 + item.iqs * 0.25 + item.probability)))
    structure = max(0.0, min(100.0, item.iqs + (8 if stock.height <= 2 else -8 if stock.height >= 4 else 0)))
    sector_switch = item.tss
    score = (
        auction_quality * float(weights["auction_quality_weight"])
        + t_day * float(weights["t_day_acceptance_weight"])
        + t1 * float(weights["t1_acceptance_weight"])
        + structure * float(weights["limit_structure_weight"])
        + sector_switch * float(weights["sector_switch_weight"])
        - fracture_risk * float(weights["fracture_penalty_weight"])
        + min(0.0, item.event_score) * float(weights["event_penalty_weight"])
    )
    return round(max(0.0, min(100.0, score)), 1)


def _p_fill(item: ScoredStock, fracture_risk: float, config: dict[str, Any]) -> float:
    cfg = config["overnight_v35"]["ev"]
    value = (
        float(cfg["fill_base"])
        + (item.iqs - 50.0) * float(cfg["fill_iqs_weight"])
        - fracture_risk * float(cfg["fill_risk_weight"])
    )
    return round(max(5.0, min(95.0, value)), 1)


def _p_up(item: ScoredStock, config: dict[str, Any]) -> float:
    cfg = config["overnight_v35"]["ev"]
    value = (
        float(cfg["up_base"])
        + item.probability * float(cfg["up_probability_weight"])
        + (item.tss - 50.0) * float(cfg["up_tss_weight"])
        + item.event_score
    )
    return round(max(5.0, min(90.0, value)), 1)


def _e_gain(item: ScoredStock, config: dict[str, Any]) -> float:
    cfg = config["overnight_v35"]["ev"]
    return round(max(1.0, float(cfg["gain_base_pct"]) + (item.tss - 50.0) * float(cfg["gain_tss_weight"])), 1)


def _e_loss(fracture_risk: float, config: dict[str, Any]) -> float:
    cfg = config["overnight_v35"]["ev"]
    return round(float(cfg["loss_base_pct"]) + fracture_risk * float(cfg["loss_risk_weight"]), 1)


def _is_high_volatility_board(code: str) -> bool:
    return code.startswith(("30", "68", "43", "83", "87", "92"))


def _market_grade(item: ScoredStock) -> str:
    return "高" if item.tss >= 65 else "中" if item.tss >= 45 else "低"


def _theme_grade(item: ScoredStock) -> str:
    return "主线" if item.tss >= 65 else "活跃" if item.tss >= 50 else "弱化"


def _position_grade(item: ScoredStock) -> str:
    if item.stock.height <= 2 and item.iqs >= 55:
        return "低位前排"
    if item.stock.height >= 4:
        return "高位辨识"
    return "普通候选"


def _buyability(fracture_risk: float) -> str:
    if fracture_risk >= 80:
        return "不适合普通竞价买入"
    if fracture_risk >= 60:
        return "只适合控价小仓"
    return "可进入隔夜候选"


def _t1_expectation(risk: float) -> str:
    if risk >= 75:
        return "承接断层风险高"
    if risk >= 55:
        return "承接需竞价确认"
    return "承接相对稳定"


def _overnight_conclusion(fracture_risk: float) -> str:
    if fracture_risk >= 90:
        return "回避"
    if fracture_risk >= 80:
        return "EV观察"
    if fracture_risk >= 70:
        return "高弹性极小仓"
    if fracture_risk >= 60:
        return "小仓控价"
    return "可优先观察"


def _core_basis(item: ScoredStock, fracture_risk: float) -> str:
    stock = item.stock
    parts = [
        f"封板{stock.board_quality}",
        f"高度{stock.height}",
        f"封成比{stock.order_to_turnover_pct:.1f}%",
        f"TSS {item.tss:.1f}",
        f"断层风险{fracture_risk:.1f}",
    ]
    if stock.overlap_flag == "是":
        parts.append(f"重叠:{'/'.join(sorted(stock.list_sources))}")
    return "；".join(parts)


def _order_line(fracture_risk: float, config: dict[str, Any]) -> str:
    premium = _premium(fracture_risk, config)
    return f"D日涨停价上方约 {premium:.1f}% 内，且T日竞价承接不弱时才考虑"


def _no_chase_line(fracture_risk: float, config: dict[str, Any]) -> str:
    premium = _premium(fracture_risk, config)
    return f"高于D日涨停价约 {premium:.1f}% 视为买贵，取消或大幅降仓"


def _premium(fracture_risk: float, config: dict[str, Any]) -> float:
    cfg = config["overnight_v35"]["price_lines"]
    if fracture_risk >= 70:
        return float(cfg["high_risk_premium_pct"])
    if fracture_risk >= 55:
        return float(cfg["medium_risk_premium_pct"])
    return float(cfg["low_risk_premium_pct"])


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
        sell_date = metadata.t1_sell_date or "待交易日历确认"
        rows = [
            ["PDF竞价买入时间", escape(metadata.auction_buy_date)],
            ["预测基准", "D日收盘后预测"],
            ["竞价/开盘买入日", escape(metadata.auction_buy_date)],
            ["T+1卖出/验证日期", escape(sell_date)],
            ["验证口径", "按A股交易日历取买入日后的下一个交易日，并用该日真实行情回填验证"],
        ]
    else:
        rows = [
            ["PDF竞价买入时间", "未识别"],
            ["预测基准", "D日收盘后预测"],
            ["竞价/开盘买入日", "待交易日历确认"],
            ["T+1卖出/验证日期", "待交易日历确认"],
            ["验证口径", "PDF未识别到竞价买入时间时，按静态T+1框架输出，待接入交易日历后自动推导具体日期"],
        ]
    return _table(["项目", "内容"], rows)


def _top4_summary_html(overnight_rows: list[OvernightScore], ev_rows: list[OvernightScore]) -> str:
    return (
        '<div class="cards two-cards">'
        f'<article class="card"><div class="card-title">隔夜单前4</div>{_top4_stock_list(overnight_rows[:4], "overnight")}'
        '<p class="muted">按买贵断层风险、隔夜适配度和赔率稳定性排序。</p></article>'
        f'<article class="card"><div class="card-title">EV单前4</div>{_top4_stock_list(ev_rows[:4], "ev")}'
        '<p class="muted">按成交概率、上涨概率、预期收益和断层风险排序。</p></article>'
        '</div>'
    )


def _top4_stock_list(rows: list[OvernightScore], mode: str) -> str:
    chips = []
    for rank, row in enumerate(rows, start=1):
        if mode == "ev":
            metric = f"EV {row.ev:.2f}"
        else:
            metric = row.overnight_conclusion
        chips.append(_stock_chip(rank, row.item.stock.name, row.item.stock.code, metric))
    return '<div class="stock-list">' + "".join(chips) + "</div>"


def _stock_chip(rank: int, name: str, code: str, metric: str) -> str:
    return (
        '<div class="stock-chip">'
        f'<span class="stock-rank">{rank}</span>'
        f'<span class="stock-main"><span class="stock-title">{escape(name)}</span><span class="stock-code">{escape(code)}</span></span>'
        f'<span class="stock-metric">{escape(metric)}</span>'
        "</div>"
    )


def _sector_summary_html(sectors: list[SectorRotation]) -> str:
    rows = [
        [escape(sector.name), f"{sector.net_flow_amount_yi:.1f}", str(sector.limit_up_count), escape(sector.heat_token), f"{sector.brs:.1f}"]
        for sector in sorted(sectors, key=lambda sector: (-sector.brs, sector.name))[:12]
    ]
    return _table(["板块", "资金净流入(亿)", "涨停家数", "热度", "BRS"], rows)


def _event_html(rows: list[OvernightScore]) -> str:
    table_rows = []
    for row in rows:
        events = "、".join(row.item.stock.sensitive_events) if row.item.stock.sensitive_events else "无"
        table_rows.append([escape(row.item.stock.code), escape(row.item.stock.name), escape(events), f"{row.item.event_score:.1f}"])
    return _table(["代码", "名称", "敏感信息", "事件分"], table_rows)


def _list_html(rows: list[OvernightScore]) -> str:
    return _table(
        ["代码", "名称", "名单来源", "重叠"],
        [[escape(row.item.stock.code), escape(row.item.stock.name), escape("/".join(sorted(row.item.stock.list_sources))), escape(row.item.stock.overlap_flag)] for row in rows],
    )


def _overlap_html(rows: list[OvernightScore]) -> str:
    groups = {
        "Top10 ∩ Top-Decision": [],
        "Top10 ∩ Premium": [],
        "Top-Decision ∩ Premium": [],
        "Top10 ∩ Top-Decision ∩ Premium": [],
    }
    for row in rows:
        sources = row.item.stock.list_sources
        label = f"{row.item.stock.name}({row.item.stock.code})"
        if {"Top 10", "Top-Decision", "Premium"}.issubset(sources):
            groups["Top10 ∩ Top-Decision ∩ Premium"].append(label)
        elif {"Top 10", "Top-Decision"}.issubset(sources):
            groups["Top10 ∩ Top-Decision"].append(label)
        elif {"Top 10", "Premium"}.issubset(sources):
            groups["Top10 ∩ Premium"].append(label)
        elif {"Top-Decision", "Premium"}.issubset(sources):
            groups["Top-Decision ∩ Premium"].append(label)
    return _table(["重叠关系", "重叠股票", "处理"], [[escape(k), escape("、".join(v) if v else "无明显重叠"), "只标注，不额外加权"] for k, v in groups.items()])


def _theme_mapping_html(rows: list[OvernightScore]) -> str:
    return _table(
        ["代码", "名称", "题材", "行业", "TSS", "板块判断"],
        [[escape(row.item.stock.code), escape(row.item.stock.name), escape(row.item.stock.theme), escape(row.item.stock.industry), f"{row.item.tss:.1f}", escape(row.theme_grade)] for row in rows],
    )


def _overnight_table(rows: list[OvernightScore]) -> str:
    table_rows = []
    for index, row in enumerate(rows, start=1):
        table_rows.append([
            str(index),
            escape(row.item.stock.code),
            escape(row.item.stock.name),
            f"{row.acceptance_score:.1f}",
            f"{row.p_up:.1f}%",
            f"{row.buy_too_high_risk:.1f}",
            f"{100.0 - row.t1_liquidity_risk:.1f}",
            escape(row.overnight_conclusion),
            escape(row.core_basis),
        ])
    return _table(["隔夜排序", "ts_code", "名称", "隔夜适配度", "自动买入后赚钱概率", "高开失真风险", "次日承接稳定性", "隔夜市价单结论", "核心依据"], table_rows, top_rows=4)


def _overnight_detail_html(rows: list[OvernightScore]) -> str:
    chunks = []
    for row in rows:
        stock = row.item.stock
        detail_rows = [
            ["基础数据核验", f"高度{stock.height}，封板{stock.board_quality}", f"换手率{stock.turnover_rate_pct:.1f}%，最高封单{stock.max_seal_order_yi:.1f}亿，涨停成交额{stock.limit_up_amount_yi:.1f}亿，封成比{stock.order_to_turnover_pct:.1f}%"],
            ["公司基本面", "静态题材核验", stock.theme or "数据缺失"],
            ["T日大盘预测", "数据缺失", "当前未接入T日实时竞价与市场情绪，只做D日静态判断"],
            ["行业资金热度", row.theme_grade, f"TSS {row.item.tss:.1f}"],
            ["涨停结构", stock.board_quality, f"连板高度{stock.height}，高位连板自动提高买贵风险"],
            ["买贵断层模型", f"断层风险{row.fracture_risk:.1f}", f"买贵风险{row.buy_too_high_risk:.1f}，T+1承接断层风险{row.t1_liquidity_risk:.1f}"],
            ["隔夜挂单模型", row.order_line, row.no_chase_line],
            ["赔率与风险", f"P_up_cycle {row.p_up:.1f}%", f"E_gain_cycle +{row.e_gain:.1f}%，E_loss_cycle -{row.e_loss:.1f}%"],
        ]
        chunks.append(f"<h3>{escape(stock.name)}({escape(stock.code)})</h3>{_table(['指标', '评估', '依据'], [[escape(a), escape(b), escape(c)] for a, b, c in detail_rows])}")
    return "".join(chunks)


def _ev_table(rows: list[OvernightScore]) -> str:
    table_rows = []
    for index, row in enumerate(rows, start=1):
        table_rows.append([
            str(index),
            escape(row.item.stock.code),
            escape(row.item.stock.name),
            escape(row.market_grade),
            escape(row.theme_grade),
            escape(row.position_grade),
            escape(row.buyability),
            escape(row.t1_expectation),
            f"{row.p_fill:.1f}%",
            f"{row.p_up:.1f}%",
            f"+{row.e_gain:.1f}% / -{row.e_loss:.1f}%",
        ])
    return _table(["EV 排位", "ts_code", "名称", "市场可做度", "题材级别", "个股地位", "次日可买性", "次日承接预期", "P_fill", "P_up", "E_gain / E_loss"], table_rows, top_rows=4)


def _ev_detail_html(rows: list[OvernightScore]) -> str:
    chunks = []
    for index, row in enumerate(rows, start=1):
        stock = row.item.stock
        detail = (
            f"<h3>{escape(stock.name)}({escape(stock.code)})</h3>"
            f"<p><strong>简介：</strong>{escape(stock.theme or '数据缺失')}</p>"
            f"<p><strong>公司基本面：</strong>需由同花顺F10/公告补齐主营与题材真实性。</p>"
            f"<p><strong>T日大盘预测：</strong>当前未接入实时竞价，静态输出。</p>"
            f"<p><strong>行业资金热度：</strong>{escape(row.theme_grade)}，TSS {row.item.tss:.1f}。</p>"
            f"<p><strong>其它：</strong>断层风险 {row.fracture_risk:.1f}。</p>"
            f"{_table(['指标', '评估'], [['P_fill', f'{row.p_fill:.1f}%'], ['P_up', f'{row.p_up:.1f}%'], ['E_gain / E_loss', f'+{row.e_gain:.1f}% / -{row.e_loss:.1f}%'], ['EV 结论', f'EV第{index}']])}"
            f"<p><strong>核心依据：</strong>{escape(row.core_basis)}</p>"
        )
        chunks.append(detail)
    return "".join(chunks)


def _final_execution_html(rows: list[OvernightScore]) -> str:
    buckets = {
        "第一优先": rows[:1],
        "第二优先": rows[1:3],
        "第三优先": rows[3:5],
        "小仓观察": [row for row in rows if 60 <= row.fracture_risk < 70],
        "题材增强": [row for row in rows if row.item.tss >= 60][:3],
        "EV观察": [row for row in rows if row.overnight_conclusion == "EV观察"][:5],
        "不优先": [row for row in rows if row.overnight_conclusion in {"高弹性极小仓", "小仓控价"}][:5],
        "回避": [row for row in rows if row.overnight_conclusion == "回避"][:5],
        "禁止普通隔夜主攻": [row for row in rows if row.fracture_risk >= 80][:8],
    }
    table_rows = []
    for name, bucket in buckets.items():
        stocks = "、".join(f"{row.item.stock.name}({row.item.stock.code})" for row in bucket) or "-"
        table_rows.append([escape(name), escape(stocks), "按不买贵价线执行；超过价线取消或降仓"])
    primary = "、".join(row.item.stock.name for row in rows[:3]) or "-"
    second = "、".join(row.item.stock.name for row in rows[3:6]) or "-"
    ev_only = "、".join(row.item.stock.name for row in buckets["禁止普通隔夜主攻"]) or "-"
    return _table(["优先级", "股票", "执行逻辑"], table_rows) + (
        f"<p class=\"callout\">主攻：{escape(primary)}。第二梯队：{escape(second)}。"
        f"禁止普通隔夜主攻：{escape(ev_only)}。若T日竞价高于各自不买贵价线，即使排序靠前，也必须取消挂单或大幅降仓。</p>"
    )

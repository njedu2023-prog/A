from __future__ import annotations

from collections import defaultdict
from typing import Any

from .models import ScoredStock, SectorRotation


EVENT_LABELS = {
    "unusual_announcement": "异动公告",
    "share_reduction": "减持",
    "loss": "亏损",
    "regulatory_warning": "监管警示",
    "concept_clarification": "概念澄清",
    "earnings_forecast": "业绩预告",
    "order": "订单",
    "restructuring": "重组",
    "control_change": "控制权变更",
}


def render_report(scored: list[ScoredStock], sectors: list[SectorRotation], config: dict[str, Any]) -> str:
    sections = [
        "# A股T+1连板概率模型输出",
        _model_header(config),
        "## 固定名单识别表",
        _fixed_list_table(scored),
        "## 数据口径与模型输入",
        _input_table(scored),
        "## 热门板块资金轮动特别分析",
        _sector_table(sectors),
        "## 已纳入模型的敏感舆情",
        _event_table(scored),
        "## 固定名单连板概率总排序",
        _ranking_table(scored),
        "## 首板进2板排序表",
        _ranking_table([item for item in scored if item.stock.height == 1]),
        "## 2板进3板排序表",
        _ranking_table([item for item in scored if item.stock.height == 2]),
        "## 3板进4板排序表",
        _ranking_table([item for item in scored if item.stock.height == 3]),
        "## N字高标续强表",
        _ranking_table([item for item in scored if item.route == config["height_routes"]["default"]]),
        "## 题材聚合表",
        _theme_table(scored, config),
        "## 个股执行确认表",
        _execution_table(scored),
        "## 最终结论",
        _conclusion(scored, config),
    ]
    return "\n\n".join(sections).rstrip() + "\n"


def _model_header(config: dict[str, Any]) -> str:
    engine = config["engine"]
    return f"模型版本：{engine['probability_model_version']} / {engine['policy_model_version']}"


def _fixed_list_table(scored: list[ScoredStock]) -> str:
    rows = ["| 股票代码 | 股票名称 | 名单来源 | 重叠 |", "|---|---|---|---|"]
    for item in scored:
        stock = item.stock
        rows.append(f"| {stock.code} | {stock.name} | {'/'.join(sorted(stock.list_sources))} | {stock.overlap_flag} |")
    return "\n".join(rows)


def _input_table(scored: list[ScoredStock]) -> str:
    rows = [
        "| 股票代码 | 封板质量 | 封单占成交 | 最高封单(亿) | 涨停成交额(亿) | 换手率 | 连板高度 | 题材 | 行业 | IQS | TSS |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|",
    ]
    for item in scored:
        stock = item.stock
        rows.append(
            f"| {stock.code} | {stock.board_quality} | {stock.order_to_turnover_pct:.1f}% | "
            f"{stock.max_seal_order_yi:.1f} | {stock.limit_up_amount_yi:.1f} | {stock.turnover_rate_pct:.1f}% | "
            f"{stock.height} | {stock.theme} | {stock.industry} | {item.iqs:.1f} | {item.tss:.1f} |"
        )
    return "\n".join(rows)


def _sector_table(sectors: list[SectorRotation]) -> str:
    rows = ["| 板块 | 资金净流入(亿) | 涨停家数 | 热度 | BRS |", "|---|---:|---:|---|---:|"]
    for sector in sorted(sectors, key=lambda item: (-item.brs, item.name)):
        rows.append(f"| {sector.name} | {sector.net_flow_amount_yi:.1f} | {sector.limit_up_count} | {sector.heat_token} | {sector.brs:.1f} |")
    return "\n".join(rows)


def _event_table(scored: list[ScoredStock]) -> str:
    rows = ["| 股票代码 | 股票名称 | 敏感舆情 | 事件分 |", "|---|---|---|---:|"]
    for item in scored:
        labels = [EVENT_LABELS.get(event, event) for event in item.stock.sensitive_events]
        rows.append(f"| {item.stock.code} | {item.stock.name} | {'、'.join(labels) if labels else '无'} | {item.event_score:.1f} |")
    return "\n".join(rows)


def _ranking_table(scored: list[ScoredStock]) -> str:
    rows = ["| 排名 | 股票代码 | 股票名称 | 当前高度 | ECS等级 | IQS | TSS | 概率 | 名单来源 |", "|---:|---|---|---:|---|---:|---:|---:|---|"]
    for index, item in enumerate(scored, start=1):
        stock = item.stock
        rows.append(
            f"| {index} | {stock.code} | {stock.name} | {stock.height} | {item.ecs_grade} | "
            f"{item.iqs:.1f} | {item.tss:.1f} | {item.probability:.1f}% | {'/'.join(sorted(stock.list_sources))} |"
        )
    if len(rows) == 2:
        rows.append("| - | - | - | - | - | - | - | - | - |")
    return "\n".join(rows)


def _theme_table(scored: list[ScoredStock], config: dict[str, Any]) -> str:
    grouped: dict[str, list[ScoredStock]] = defaultdict(list)
    delimiters = config["tss"]["delimiters"]
    for item in scored:
        themes = [item.stock.theme]
        for delimiter in delimiters:
            themes = [part for theme in themes for part in theme.split(delimiter)]
        for theme in {part.strip() for part in themes if part.strip()}:
            grouped[theme].append(item)
    rows = ["| 题材 | 股票数 | 平均概率 | 最高TSS | 股票 |", "|---|---:|---:|---:|---|"]
    for theme, items in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        avg_prob = sum(item.probability for item in items) / len(items)
        max_tss = max(item.tss for item in items)
        names = "、".join(f"{item.stock.name}({item.stock.code})" for item in sorted(items, key=lambda item: item.stock.code))
        rows.append(f"| {theme} | {len(items)} | {avg_prob:.1f}% | {max_tss:.1f} | {names} |")
    return "\n".join(rows)


def _execution_table(scored: list[ScoredStock]) -> str:
    rows = ["| 股票代码 | 股票名称 | 执行确认 | 关注点 |", "|---|---|---|---|"]
    for item in scored:
        focus = "重叠标的" if item.stock.overlap_flag == "是" else "单名单标的"
        if item.stock.sensitive_events:
            focus += "；有敏感舆情"
        rows.append(f"| {item.stock.code} | {item.stock.name} | 已计算 | {focus} |")
    return "\n".join(rows)


def _conclusion(scored: list[ScoredStock], config: dict[str, Any]) -> str:
    if not scored:
        return "未识别到固定名单股票。"
    top_n = min(int(config["output"]["top_n"]), len(scored))
    leaders = scored[:top_n]
    leader_text = "、".join(
        f"{item.stock.name}({item.stock.code}) {item.probability:.1f}%/{item.ecs_grade}" for item in leaders
    )
    return f"连板概率最高{top_n}票为：{leader_text}。"

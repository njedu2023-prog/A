from __future__ import annotations

import re
from html import escape
from pathlib import Path
from typing import Any

from .continuation import ContinuationScore, render_continuation_html, score_continuation
from .models import ReportMetadata, ScoredStock, SectorRotation
from .overnight import OvernightScore, render_overnight_html, score_overnight
from .report import render_html_report


def build_system_bundle(scored: list[ScoredStock], sectors: list[SectorRotation], config: dict[str, Any]) -> dict[str, Any]:
    overnight = score_overnight(scored, sectors, config)
    continuation = score_continuation(scored, sectors, config)
    return {
        "scored": scored,
        "sectors": sectors,
        "config": config,
        "overnight": overnight,
        "continuation": continuation,
    }


def render_report_pack(
    scored: list[ScoredStock],
    sectors: list[SectorRotation],
    config: dict[str, Any],
    input_pdf: str | Path | None = None,
    metadata: ReportMetadata | None = None,
) -> str:
    return render_report_pack_pages(scored, sectors, config, input_pdf, "latest", metadata)["latest.html"]


def render_report_pack_pages(
    scored: list[ScoredStock],
    sectors: list[SectorRotation],
    config: dict[str, Any],
    input_pdf: str | Path | None = None,
    base_name: str = "latest",
    metadata: ReportMetadata | None = None,
) -> dict[str, str]:
    bundle = build_system_bundle(scored, sectors, config)
    first_html = _extract_report_body(render_html_report(scored, sectors, config))
    metadata = metadata or ReportMetadata()
    overnight_html = render_overnight_html(bundle["overnight"], sectors, config, metadata)
    continuation_html = render_continuation_html(bundle["continuation"], sectors, config, metadata)
    dashboard = _dashboard_html(scored, bundle["overnight"], bundle["continuation"], input_pdf)
    validation = _validation_html(metadata)
    filenames = _page_filenames(base_name)
    return {
        filenames["home"]: _page_html("T+1 Maya v1.0", "home", _home_page_html(scored, bundle["overnight"], bundle["continuation"], filenames), filenames, metadata),
        filenames["dashboard"]: _page_html("综合看板", "dashboard", f'<section class="system-root"><h2>综合看板</h2>{dashboard}</section>', filenames, metadata),
        filenames["limit"]: _page_html("连板概率", "limit", f'<section class="system-root"><h2>连板概率</h2>{first_html}</section>', filenames, metadata),
        filenames["overnight"]: _page_html("隔夜单 / EV单", "overnight", f'<section class="system-root">{overnight_html}</section>', filenames, metadata),
        filenames["continuation"]: _page_html("最终承接", "continuation", f'<section class="system-root">{continuation_html}</section>', filenames, metadata),
        filenames["validation"]: _page_html("验证复盘", "validation", f'<section class="system-root"><h2>验证复盘</h2>{validation}</section>', filenames, metadata),
    }


def _page_filenames(base_name: str) -> dict[str, str]:
    return {
        "home": f"{base_name}.html",
        "dashboard": f"{base_name}_dashboard.html",
        "limit": f"{base_name}_limit.html",
        "overnight": f"{base_name}_overnight.html",
        "continuation": f"{base_name}_continuation.html",
        "validation": f"{base_name}_validation.html",
    }


def _page_html(title: str, active: str, body: str, filenames: dict[str, str], metadata: ReportMetadata | None = None) -> str:
    metadata = metadata or ReportMetadata()
    buy_time = metadata.auction_buy_date_iso or metadata.auction_buy_date or "待确认"
    sell_time = metadata.t1_sell_date or "待交易日历推算"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} - A股T+1三系统报告包</title>
  <style>
    :root {{
      --bg: #f5f5f7;
      --panel: #fff;
      --panel-alt: #fbfbfd;
      --text: #1d1d1f;
      --muted: #6e6e73;
      --line: #e8e8ed;
      --blue: #0071e3;
      --soft: #f5f9ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }}
    .topnav {{
      position: sticky;
      top: 0;
      z-index: 10;
      height: 46px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 24px;
      background: rgba(251,251,253,.82);
      backdrop-filter: saturate(180%) blur(20px);
      border-bottom: 1px solid rgba(0,0,0,.04);
    }}
    .topnav a {{ color: var(--text); text-decoration: none; font-size: 13px; opacity: .78; white-space: nowrap; padding: 4px 2px; }}
    .topnav a:hover {{ opacity: 1; }}
    .topnav a.active {{ opacity: 1; font-weight: 600; }}
    body > header {{ background: var(--panel); padding: 42px 24px 34px; text-align: center; margin-bottom: 10px; }}
    body > header h1 {{ margin: 0; font-size: 56px; line-height: 1.03; letter-spacing: 0; font-weight: 700; }}
    .brand-muted {{ color: rgba(29,29,31,.30); }}
    .system-dates {{ margin-top: 14px; display: flex; justify-content: center; gap: 10px; flex-wrap: wrap; color: var(--muted); font-size: 14px; }}
    .system-date-pill {{ display: inline-flex; align-items: center; min-height: 30px; padding: 5px 12px; border-radius: 999px; background: var(--soft); color: var(--text); }}
    main h1 {{ margin: 0 0 10px; font-size: 30px; line-height: 1.12; letter-spacing: 0; text-align: center; }}
    h2 {{ margin: 0 0 18px; font-size: 24px; text-align: center; line-height: 1.16; letter-spacing: 0; }}
    h3 {{ margin: 26px 0 12px; font-size: 19px; line-height: 1.22; letter-spacing: 0; }}
    main {{ max-width: 1380px; margin: 0 auto 42px; padding: 0 18px; }}
    .system-root {{ background: var(--panel); margin: 10px 0; padding: 26px 28px; }}
    .system-root > h2 {{ font-size: 24px; margin-bottom: 22px; }}
    .system-root > h3:first-child {{ margin-top: 0; }}
    .system-root > section,
    .system-section {{ background: var(--panel); margin: 0 0 12px; padding: 28px 24px; }}
    .system-root > section:first-child,
    .system-section:first-child {{ padding-top: 20px; }}
    .system-root > section:last-child,
    .system-section:last-child {{ margin-bottom: 0; }}
    .cards {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 18px auto 0; align-items: stretch; width: 100%; }}
    .cards.two-cards {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .card {{ background: var(--panel-alt); padding: 22px; min-height: 0; height: 100%; display: flex; flex-direction: column; gap: 14px; }}
    a.card {{ color: inherit; text-decoration: none; }}
    .home-card:hover {{ background: #f5f9ff; }}
    .home-title {{ margin: 0; color: var(--text); font-size: 18px; line-height: 1.25; font-weight: 700; }}
    .home-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 24px; }}
    .home-nav-card {{ min-height: 172px; background: var(--panel-alt); color: inherit; text-decoration: none; padding: 24px 22px; display: flex; flex-direction: column; justify-content: space-between; }}
    .home-nav-card:hover {{ background: #f5f9ff; }}
    .home-nav-index {{ width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; background: #e8f2ff; color: #06c; font-size: 13px; font-weight: 700; }}
    .home-nav-title {{ margin-top: 18px; font-size: 20px; line-height: 1.2; font-weight: 700; }}
    .home-nav-desc {{ margin: 8px 0 0; color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .home-nav-action {{ margin-top: 20px; color: var(--blue); font-size: 13px; font-weight: 600; }}
    .core-cards {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .core-title {{ text-align: center; }}
    .card-title {{ font-size: 15px; color: var(--muted); font-weight: 600; }}
    .stock-list {{ display: grid; gap: 8px; align-content: start; flex: 1; }}
    .stock-chip {{
      display: grid;
      grid-template-columns: 28px minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
      min-height: 44px;
      padding: 10px 0;
      border-top: 1px solid rgba(210,210,215,.36);
    }}
    .stock-chip:first-child {{ border-top: 0; padding-top: 0; }}
    .stock-rank {{ width: 28px; height: 28px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; background: #e8f2ff; color: #06c; font-size: 13px; font-weight: 700; }}
    .stock-main {{ min-width: 0; }}
    .stock-title, .stock-code {{ font-size: 16px; line-height: 1.18; font-weight: 700; overflow-wrap: anywhere; }}
    .stock-title {{ color: var(--text); }}
    .stock-code {{ color: rgba(29, 29, 31, .3); }}
    .stock-code {{ margin-left: 2px; font-variant-numeric: tabular-nums; }}
    .stock-metric {{ color: var(--text); font-size: 14px; font-weight: 700; white-space: nowrap; }}
    .card p {{ margin: 0; }}
    .card > .muted {{ margin-top: auto; }}
    .overlap-panel {{
      margin-top: 12px;
      background: var(--soft);
      padding: 24px;
    }}
    .overlap-panel h3 {{ margin-top: 0; text-align: center; font-size: 24px; }}
    .overlap-panel .muted {{ text-align: center; margin: -4px 0 18px; font-size: 15px; }}
    .overlap-panel th {{ font-size: 14px; }}
    .overlap-panel td {{ color: var(--text); font-size: 15px; font-weight: 700; }}
    .scope-panel {{ margin-top: 12px; background: var(--panel); border: 1px solid var(--line); padding: 22px 24px; }}
    .scope-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 16px 0; }}
    .scope-item {{ background: var(--panel-alt); padding: 16px; }}
    .scope-label {{ color: var(--muted); font-size: 12px; font-weight: 600; }}
    .scope-value {{ margin-top: 6px; font-size: 24px; line-height: 1.1; font-weight: 700; }}
    .scope-detail {{ color: var(--muted); font-size: 13px; line-height: 1.55; }}
    .scope-table table {{ min-width: 0; }}
    .scope-table td:first-child {{ width: 160px; color: var(--muted); font-weight: 600; white-space: nowrap; }}
    .muted {{ color: var(--muted); font-size: 14px; }}
    .callout {{ background: var(--soft); padding: 14px 16px; font-size: 14px; }}
    .table-wrap {{ overflow-x: auto; width: 100%; }}
    table {{ width: 100%; border-collapse: collapse; min-width: 880px; }}
    th, td {{ border-bottom: 1px solid var(--line); padding: 11px 12px; text-align: left; vertical-align: top; font-size: 13px; line-height: 1.45; }}
    th {{ color: var(--muted); font-weight: 600; white-space: nowrap; background: var(--panel); }}
    .stock-name-cell {{ white-space: nowrap; word-break: keep-all; }}
    tbody tr:nth-child(odd) td {{ background: rgba(245,245,247,.36); }}
    tbody tr.top-row td {{ background: var(--soft); color: var(--text); font-weight: 400; }}
    .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px; background: #f5f5f7; }}
    .route-pill {{ display: inline-block; margin: 6px 0; padding: 3px 9px; border-radius: 999px; background: #e8f2ff; color: #06c; font-size: 12px; font-weight: 600; }}
    .leaders, .conclusion-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .leader, .conclusion-card {{ background: var(--panel-alt); padding: 22px 18px; text-align: center; min-height: 150px; }}
    .prob, .conclusion-prob {{ font-size: 28px; line-height: 1.08; font-weight: 700; }}
    .model, .link-row {{ font-size: 14px; line-height: 1.45; }}
    .link-row {{ margin-top: 8px; color: var(--blue); }}
    .leader-code {{ margin-top: 8px; color: var(--text); font-size: 18px; line-height: 1.14; font-weight: 700; font-variant-numeric: tabular-nums; }}
    .name, .conclusion-name {{ font-size: 20px; line-height: 1.18; font-weight: 700; }}
    .rank, .code, .conclusion-rank, .conclusion-code, .conclusion-meta {{ color: var(--muted); font-size: 12px; }}
    .basis-note, .conclusion-kicker {{ color: var(--muted); text-align: center; }}
    .table-actions {{ display: flex; justify-content: center; margin: 0 0 12px; }}
    .sort-button {{ border: 1px solid var(--line); border-radius: 999px; background: var(--panel); color: var(--text); padding: 8px 14px; cursor: pointer; }}
    .sort-button:hover {{ background: var(--soft); }}
    .site-disclaimer {{
      max-width: 1180px;
      margin: -18px auto 34px;
      padding: 0 18px;
      color: rgba(29,29,31,.45);
      font-size: 12px;
      line-height: 1.7;
      text-align: center;
    }}
    @media (max-width: 900px) {{
      body > header {{ padding: 34px 18px 28px; }}
      body > header h1 {{ font-size: 38px; }}
      .system-dates {{ font-size: 12px; }}
      main h1 {{ font-size: 25px; }}
      h2 {{ font-size: 24px; }}
      .topnav {{ justify-content: flex-start; overflow-x: auto; padding: 0 14px; gap: 18px; }}
      .cards, .core-cards, .home-grid, .leaders, .conclusion-grid {{ grid-template-columns: 1fr; }}
      .stock-title, .stock-code {{ font-size: 16px; }}
      .stock-chip {{ grid-template-columns: 26px minmax(0, 1fr) auto; gap: 8px; }}
      .stock-rank {{ width: 26px; height: 26px; }}
      .scope-grid {{ grid-template-columns: 1fr 1fr; }}
      main {{ padding: 0 10px; }}
      .system-root {{ padding: 20px 12px; }}
      .system-root > section,
      .system-section {{ padding: 24px 12px; }}
      th, td {{ font-size: 12px; padding: 9px 10px; }}
    }}
  </style>
</head>
<body>
  <nav class="topnav">
    {_nav_link("首页", filenames["home"], active == "home")}
    {_nav_link("综合看板", filenames["dashboard"], active == "dashboard")}
    {_nav_link("连板概率", filenames["limit"], active == "limit")}
    {_nav_link("隔夜单 / EV单", filenames["overnight"], active == "overnight")}
    {_nav_link("最终承接", filenames["continuation"], active == "continuation")}
    {_nav_link("验证复盘", filenames["validation"], active == "validation")}
  </nav>
  <header>
    <h1>T+1 <span class="brand-muted">Maya v1.0</span></h1>
    <div class="system-dates">
      <span class="system-date-pill">Buy：{escape(buy_time)}</span>
      <span class="system-date-pill">Sell：{escape(sell_time)}</span>
    </div>
  </header>
  <main>
    {body}
  </main>
  <footer class="site-disclaimer">重要提示：本系统输出内容仅为量化研究与信息整理结果，不构成投资建议或任何形式的买卖依据。模型概率、排序、评分及结论均不代表未来收益保证，任何基于本系统内容作出的投资决策，均由使用者独立判断并自行承担风险。</footer>
  <script>
    (() => {{
      const button = document.querySelector('[data-theme-prob-sort]');
      const table = document.querySelector('[data-theme-table]');
      if (!button || !table) return;
      const tbody = table.tBodies[0];
      Array.from(tbody.rows).forEach((row, index) => row.dataset.originalIndex = String(index));
      button.addEventListener('click', () => {{
        const nextOrder = button.dataset.order === 'desc' ? 'asc' : 'desc';
        const direction = nextOrder === 'asc' ? 1 : -1;
        const rows = Array.from(tbody.rows);
        rows.sort((left, right) => {{
          const leftValue = Number.parseFloat(left.cells[2]?.textContent.replace('%', '') || '0');
          const rightValue = Number.parseFloat(right.cells[2]?.textContent.replace('%', '') || '0');
          if (leftValue !== rightValue) return (leftValue - rightValue) * direction;
          return Number(left.dataset.originalIndex || '0') - Number(right.dataset.originalIndex || '0');
        }});
        tbody.append(...rows);
        button.dataset.order = nextOrder;
        button.textContent = nextOrder === 'asc' ? '平均概率升序' : '平均概率降序';
      }});
    }})();
  </script>
</body>
</html>
"""


def _nav_link(label: str, href: str, active: bool) -> str:
    cls = ' class="active"' if active else ""
    return f'<a{cls} href="{escape(href)}">{escape(label)}</a>'


def _home_page_html(
    scored: list[ScoredStock],
    overnight: list[OvernightScore],
    continuation: list[ContinuationScore],
    filenames: dict[str, str],
) -> str:
    ev_rows = sorted(overnight, key=lambda row: (-row.ev, row.fracture_risk, row.item.stock.code))
    return (
        '<section class="system-root">'
        '<h3 class="core-title">核心名单</h3>'
        '<div class="cards core-cards">'
        f'<article class="card"><div class="card-title">连板概率 Top4</div>{_limit_stock_list(scored[:4])}</article>'
        f'<article class="card"><div class="card-title">隔夜单 Top4</div>{_overnight_stock_list(overnight[:4])}</article>'
        f'<article class="card"><div class="card-title">EV单 Top4</div>{_ev_stock_list(ev_rows[:4])}</article>'
        f'<article class="card"><div class="card-title">最终承接 Top4</div>{_continuation_stock_list(continuation[:4])}</article>'
        '</div>'
        '<h3 class="core-title">系统共振</h3>'
        + _resonance_html(scored[:4], overnight[:4], ev_rows[:4], continuation[:4])
        + '</section>'
    )


def _home_nav_card(index: str, title: str, desc: str, href: str) -> str:
    return (
        f'<a class="home-nav-card" href="{escape(href)}">'
        '<div>'
        f'<span class="home-nav-index">{escape(index)}</span>'
        f'<div class="home-nav-title">{escape(title)}</div>'
        f'<p class="home-nav-desc">{escape(desc)}</p>'
        '</div>'
        '<div class="home-nav-action">打开</div>'
        '</a>'
    )


def _extract_report_body(html: str) -> str:
    header = _extract_between(html, "<header>", "</header>")
    main = _extract_between(html, "<main>", "</main>")
    return header + main if header or main else html


def _extract_between(text: str, start: str, end: str) -> str:
    match = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, flags=re.S)
    return match.group(1) if match else ""


def _dashboard_html(
    scored: list[ScoredStock],
    overnight: list[OvernightScore],
    continuation: list[ContinuationScore],
    input_pdf: str | Path | None,
) -> str:
    top_limit = scored[:4]
    top_overnight = overnight[:4]
    top_continuation = continuation[:4]
    overlap = _three_system_overlap_table(top_limit, top_overnight, top_continuation)
    return (
        '<div class="cards">'
        f'<article class="card"><div class="card-title">连板概率最高</div>{_limit_stock_list(top_limit)}<p class="muted">目标：哪4票更可能晋级连板。</p></article>'
        f'<article class="card"><div class="card-title">隔夜更稳</div>{_overnight_stock_list(top_overnight)}<p class="muted">目标：先排除买贵和T+1承接断层。</p></article>'
        f'<article class="card"><div class="card-title">最终承接靠前</div>{_continuation_stock_list(top_continuation)}<p class="muted">目标：T买入到T+1卖出的可兑现收益排序。</p></article>'
        '</div>'
        + overlap
        + _data_scope_html(scored, input_pdf)
        + _cross_table(scored, overnight, continuation)
    )


def _three_system_overlap_table(
    top_limit: list[ScoredStock],
    top_overnight: list[OvernightScore],
    top_continuation: list[ContinuationScore],
) -> str:
    limit_by_code = {item.stock.code: (rank, item) for rank, item in enumerate(top_limit, start=1)}
    overnight_by_code = {row.item.stock.code: (rank, row) for rank, row in enumerate(top_overnight, start=1)}
    continuation_by_code = {row.item.stock.code: (rank, row) for rank, row in enumerate(top_continuation, start=1)}
    overlap_codes = sorted(set(limit_by_code) & set(overnight_by_code) & set(continuation_by_code))
    rows = []
    for code in overlap_codes:
        limit_rank, limit_item = limit_by_code[code]
        overnight_rank, overnight_row = overnight_by_code[code]
        continuation_rank, continuation_row = continuation_by_code[code]
        rows.append(
            [
                escape(code),
                escape(limit_item.stock.name),
                str(limit_rank),
                f"{limit_item.probability:.1f}%",
                str(overnight_rank),
                escape(overnight_row.overnight_conclusion),
                str(continuation_rank),
                escape(continuation_row.conclusion),
            ]
        )
    if not rows:
        rows = [["-", "无三系统Top推荐重叠", "-", "-", "-", "-", "-", "-"]]
    return (
        '<div class="overlap-panel">'
        '<h3>三系统Top推荐重叠</h3>'
        '<p class="muted">只展示同时进入三套系统首屏推荐名单的股票；这是交叉提示，不额外加权。</p>'
        + _table(["代码", "名称", "连板排名", "连板概率", "隔夜排名", "隔夜结论", "承接排名", "承接结论"], rows)
        + "</div>"
    )


def _data_scope_html(scored: list[ScoredStock], input_pdf: str | Path | None) -> str:
    source_counts = _source_counts(scored)
    total_entries = sum(source_counts.values())
    unique_count = len(scored)
    overlap_rows = [item for item in scored if len(item.stock.list_sources) > 1]
    relation_counts = _overlap_relation_counts(scored)
    source_text = "；".join(f"{name} {count}只" for name, count in source_counts.items())
    relation_text = "；".join(f"{name} {count}只" for name, count in relation_counts.items())
    detail_rows = [
        ["PDF", escape(str(input_pdf) if input_pdf else "未记录")],
        ["名单分布", escape(source_text or "数据缺失")],
        ["重叠关系", f"{escape(relation_text or '无明显重叠')}。重叠只标注，不额外加权；实际参与计算为去重后的 {unique_count} 只。"],
        ["数据增强", "PDF只是基础入口，同花顺和网络证据将在数据增强层补齐。"],
    ]
    return (
        '<div class="scope-panel">'
        '<h3>数据口径摘要</h3>'
        '<div class="scope-grid">'
        f'<div class="scope-item"><div class="scope-label">三组名单原始条数</div><div class="scope-value">{total_entries}</div></div>'
        f'<div class="scope-item"><div class="scope-label">去重后计算股票</div><div class="scope-value">{unique_count}</div></div>'
        f'<div class="scope-item"><div class="scope-label">重叠股票</div><div class="scope-value">{len(overlap_rows)}</div></div>'
        f'<div class="scope-item"><div class="scope-label">输入PDF</div><div class="scope-value">1</div></div>'
        '</div>'
        '<div class="scope-table">'
        + _table(["项目", "内容"], detail_rows)
        + '</div>'
        '</div>'
    )


def _source_counts(scored: list[ScoredStock]) -> dict[str, int]:
    counts = {"Top 10": 0, "Top-Decision": 0, "Premium": 0}
    for item in scored:
        for source in item.stock.list_sources:
            counts[source] = counts.get(source, 0) + 1
    return counts


def _overlap_relation_counts(scored: list[ScoredStock]) -> dict[str, int]:
    counts = {
        "Top10∩Top-Decision": 0,
        "Top10∩Premium": 0,
        "Top-Decision∩Premium": 0,
        "三组共同": 0,
    }
    for item in scored:
        sources = item.stock.list_sources
        if {"Top 10", "Top-Decision", "Premium"}.issubset(sources):
            counts["三组共同"] += 1
        elif {"Top 10", "Top-Decision"}.issubset(sources):
            counts["Top10∩Top-Decision"] += 1
        elif {"Top 10", "Premium"}.issubset(sources):
            counts["Top10∩Premium"] += 1
        elif {"Top-Decision", "Premium"}.issubset(sources):
            counts["Top-Decision∩Premium"] += 1
    return counts


def _cross_table(scored: list[ScoredStock], overnight: list[OvernightScore], continuation: list[ContinuationScore]) -> str:
    overnight_by_code = {row.item.stock.code: (rank, row) for rank, row in enumerate(overnight, start=1)}
    continuation_by_code = {row.item.stock.code: (rank, row) for rank, row in enumerate(continuation, start=1)}
    headers = ["连板排名", "代码", "名称", "连板概率", "隔夜排名", "隔夜结论", "承接排名", "承接结论", "交叉提示"]
    body_rows = []
    for rank, item in enumerate(scored, start=1):
        overnight_rank, overnight_row = overnight_by_code.get(item.stock.code, (0, None))
        continuation_rank, continuation_row = continuation_by_code.get(item.stock.code, (0, None))
        cells = [
            str(rank),
            escape(item.stock.code),
            escape(item.stock.name),
            f"{item.probability:.1f}%",
            str(overnight_rank or "-"),
            escape(overnight_row.overnight_conclusion if overnight_row else "-"),
            str(continuation_rank or "-"),
            escape(continuation_row.conclusion if continuation_row else "-"),
            escape(_risk_label(overnight_row, continuation_row)),
        ]
        row_class = ' class="top-row"' if rank <= 4 else ""
        body_rows.append(
            f"<tr{row_class}>"
            + "".join(f'<td{_cell_class(headers[index])}>{cell}</td>' for index, cell in enumerate(cells))
            + "</tr>"
        )
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    table = f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'
    return (
        '<div class="ranking-panel">'
        '<h3>全名单交叉排序</h3>'
        + table
        + "</div>"
    )


def _risk_label(overnight: OvernightScore | None, continuation: ContinuationScore | None) -> str:
    labels = []
    if overnight and overnight.fracture_risk >= 80:
        labels.append("禁止普通隔夜主攻")
    if continuation and continuation.s8_risk_penalty >= 70:
        labels.append("承接风险高")
    if overnight and continuation and overnight.item.stock.overlap_flag == "是":
        labels.append("名单重叠仅标注")
    return "；".join(labels) or "无强制提示"


def _validation_html(metadata: ReportMetadata) -> str:
    auction_date_iso = metadata.auction_buy_date_iso or "待交易日历确认"
    t1_sell_date = metadata.t1_sell_date or "待交易日历确认"
    rows = [
        ["预测基准", "D日收盘后预测"],
        ["买入完成时间", escape(auction_date_iso)],
        ["T+1卖出/验证日期", escape(t1_sell_date)],
        ["真值来源", "同花顺T/T+1真实行情；后续可接入网络行情源交叉校验"],
        ["验证状态", "已保留预测快照，等待真实行情回填"],
    ]
    note = (
        '<p class="callout">后续验证中心将读取同花顺T/T+1真实行情，累计统计三套模型的命中率、收益率、回撤和执行纪律有效性。'
        "当前日期线已固定，后续验证必须按T+1卖出/验证日期回填真值。</p>"
    )
    return _table(["项目", "内容"], rows) + note


def _table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    if not rows:
        rows = [["-" for _ in headers]]
    body = "".join(
        "<tr>"
        + "".join(f'<td{_cell_class(headers[index])}>{cell}</td>' for index, cell in enumerate(row))
        + "</tr>"
        for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _cell_class(header: str) -> str:
    return ' class="stock-name-cell"' if header in {"名称", "股票名称"} else ""


def _resonance_html(
    limit_rows: list[ScoredStock],
    overnight_rows: list[OvernightScore],
    ev_rows: list[OvernightScore],
    continuation_rows: list[ContinuationScore],
) -> str:
    resonance: dict[str, dict[str, Any]] = {}

    def add(code: str, name: str, system_name: str) -> None:
        row = resonance.setdefault(code, {"code": code, "name": name, "systems": []})
        if system_name not in row["systems"]:
            row["systems"].append(system_name)

    for row in limit_rows:
        add(row.stock.code, row.stock.name, "连板概率")
    for row in overnight_rows:
        add(row.item.stock.code, row.item.stock.name, "隔夜单")
    for row in ev_rows:
        add(row.item.stock.code, row.item.stock.name, "EV单")
    for row in continuation_rows:
        add(row.item.stock.code, row.item.stock.name, "最终承接")

    rows = sorted(
        (row for row in resonance.values() if len(row["systems"]) >= 2),
        key=lambda row: (-len(row["systems"]), row["code"]),
    )
    table_rows = [
        [escape(row["code"]), escape(row["name"]), str(len(row["systems"])), escape(" / ".join(row["systems"]))]
        for row in rows
    ]
    return _table(["代码", "名称", "共振次数", "命中系统"], table_rows)


def _limit_stock_list(rows: list[ScoredStock]) -> str:
    chips = [
        _stock_chip(rank, row.stock.name, row.stock.code, f"{row.probability:.1f}%")
        for rank, row in enumerate(rows, start=1)
    ]
    return '<div class="stock-list">' + "".join(chips) + "</div>"


def _overnight_stock_list(rows: list[OvernightScore]) -> str:
    chips = [
        _stock_chip(rank, row.item.stock.name, row.item.stock.code, row.overnight_conclusion)
        for rank, row in enumerate(rows, start=1)
    ]
    return '<div class="stock-list">' + "".join(chips) + "</div>"


def _ev_stock_list(rows: list[OvernightScore]) -> str:
    chips = [
        _stock_chip(rank, row.item.stock.name, row.item.stock.code, f"EV {row.ev:.2f}")
        for rank, row in enumerate(rows, start=1)
    ]
    return '<div class="stock-list">' + "".join(chips) + "</div>"


def _continuation_stock_list(rows: list[ContinuationScore]) -> str:
    chips = [
        _stock_chip(rank, row.item.stock.name, row.item.stock.code, str(row.value_score))
        for rank, row in enumerate(rows, start=1)
    ]
    return '<div class="stock-list">' + "".join(chips) + "</div>"


def _stock_chip(rank: int, name: str, code: str, metric: str) -> str:
    return (
        '<div class="stock-chip">'
        f'<span class="stock-rank">{rank}</span>'
        f'<span class="stock-main"><span class="stock-title">{escape(name)}</span><span class="stock-code">{escape(code)}</span></span>'
        f'<span class="stock-metric">{escape(metric)}</span>'
        '</div>'
    )

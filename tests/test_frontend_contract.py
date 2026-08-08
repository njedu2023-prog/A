from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


class FrontendContractTests(unittest.TestCase):
    def test_conditional_return_is_between_stock_and_promotion_target(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        match = re.search(
            r"const candidateTable = table\(\[(.*?)\], rows,",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        headers = match.group(1)
        stock = headers.index('{text: "股票"')
        expected_return = headers.index('条件净收益（10–90分位）')
        promotion_target = headers.index('晋级目标')
        self.assertLess(stock, expected_return)
        self.assertLess(expected_return, promotion_target)

    def test_mobile_viewport_and_scroll_regions_are_accessible(self) -> None:
        source = Path("index.html").read_text(encoding="utf-8")
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            source,
        )
        self.assertNotIn("user-scalable=no", source)
        self.assertNotIn("maximum-scale", source)
        self.assertRegex(
            source,
            r'id="candidates"\s+class="table-wrap"\s+tabindex="0"\s+role="region"\s+aria-label="[^"]+横向滑动[^"]+"',
        )

    def test_mobile_css_keeps_daily_summary_visible(self) -> None:
        source = Path("assets/styles.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 720px)", source)
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(0, 1fr));",
            source,
        )
        self.assertIn("overscroll-behavior-inline: contain;", source)
        self.assertIn("-webkit-overflow-scrolling: touch;", source)
        self.assertIn(".table-wrap:focus-visible", source)
        self.assertIn("@media (prefers-reduced-motion: reduce)", source)
        self.assertNotRegex(
            source,
            r"(?s)@media \(max-width: 720px\).*?(?:th|td)\s*\{[^}]*display:\s*none",
        )

    def test_tables_keep_mobile_identity_and_header_semantics(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        self.assertIn("code sticky-code", source)
        self.assertIn("name sticky-name", source)
        self.assertIn('className: "sticky-stock"', source)
        self.assertIn('heading.scope = "col";', source)
        self.assertIn('detailWrap.setAttribute("role", "region");', source)
        self.assertIn(
            'dailyTable.classList.add("daily-detail-table");',
            source,
        )

    def test_formal_model_fields_and_policy_gate_are_visible(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        for heading in (
            "条件净收益（10–90分位）",
            "晋级估计",
            "开盘成交",
            "延迟风险",
            "风险调整值",
            "策略门槛",
            "执行状态",
        ):
            self.assertIn(heading, source)
        self.assertLess(source.index("策略门槛"), source.index("执行状态"))
        self.assertIn("基线运行 · 样本积累中", source)
        self.assertIn("gateText(item)", source)
        self.assertIn("predictionOf(item)", source)

    def test_static_assets_are_cache_busted(self) -> None:
        source = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("styles.css?v=20260808-3", source)
        self.assertIn("app.js?v=20260808-3", source)

    def test_overview_uses_clear_settlement_copy(self) -> None:
        page = Path("index.html").read_text(encoding="utf-8")
        script = Path("assets/app.js").read_text(encoding="utf-8")
        self.assertIn("仅计已结算收益", page)
        self.assertIn("待结算不计入当前收益", page)
        self.assertIn("累计收益仅包含已结算批次", script)
        self.assertIn("已结算 / 待结算", script)
        self.assertIn("历史累计收益", script)
        self.assertIn("月度累计收益", script)
        self.assertNotIn("月度状态", script)
        self.assertNotIn("成立以来", script)
        self.assertNotIn("最终 / 未完成", script)

    def test_overview_separates_rank_and_portfolio_groups(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        styles = Path("assets/styles.css").read_text(encoding="utf-8")
        self.assertIn("固定名次策略", script)
        self.assertIn("全部候选等权组合", script)
        self.assertIn("每日实际交集候选等权统计", script)
        self.assertIn("按实际退出日", script)
        self.assertIn("当前仅含", script)
        self.assertIn(".rank-strategy-grid", styles)
        self.assertIn(".portfolio-strategy-grid", styles)

    def test_overview_uses_comparison_bars_before_five_settled_batches(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        styles = Path("assets/styles.css").read_text(encoding="utf-8")
        self.assertIn("const MIN_TREND_POINTS = 5;", script)
        self.assertIn("const settledMinimum = Math.min", script)
        self.assertIn("if (settledMinimum < MIN_TREND_POINTS)", script)
        self.assertIn("renderComparisonChart(container, rankStats);", script)
        self.assertIn("暂不绘制趋势", script)
        self.assertIn("comparison-track", script)
        self.assertIn("0%", script)
        self.assertIn(".comparison-track::after", styles)

    def test_equity_chart_uses_only_final_points(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        self.assertIn(
            "if (!item || item.is_final !== true || !finite(item.daily_return)) return;",
            script,
        )
        self.assertIn(
            "if (!item || item.is_final !== true || !finite(item.daily_return)) return null;",
            script,
        )
        self.assertNotIn("return started && !gap ? nav : null", script)
        self.assertIn('baseline.setAttribute("class", "baseline");', script)

    def test_overview_remains_collapsed_by_default(self) -> None:
        source = Path("index.html").read_text(encoding="utf-8")
        match = re.search(r'<details class="panel overview-panel"([^>]*)>', source)
        self.assertIsNotNone(match)
        self.assertNotIn("open", match.group(1).split())

    def test_status_metadata_is_split_into_summary_and_source_rows(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        styles = Path("assets/styles.css").read_text(encoding="utf-8")
        self.assertIn("status-meta-row status-meta-summary", script)
        self.assertIn("status-meta-row status-meta-automation", script)
        self.assertIn("status-meta-row status-meta-sources", script)
        self.assertIn("meta.append(summaryRow, automationRow, sourceRow);", script)
        self.assertIn('run.scheduled_local_time || "21:30"', script)
        self.assertIn('run.scheduled_local_time || "19:00"', script)
        self.assertIn("outputAutomationCopy(outputRun, outputAt)", script)
        self.assertIn("validationAutomationCopy(validationRun)", script)
        self.assertIn('`到期 ${run.due}`', script)
        self.assertIn('`完成 ${run.final}`', script)
        self.assertIn('`待数据 ${run.pending_data}`', script)
        self.assertIn('`延期 ${run.delayed}`', script)
        self.assertIn('`失败 ${run.failed}`', script)
        self.assertIn("SUCCESS_NO_DUE", script)
        self.assertIn(".automation-meta-text.degraded", styles)
        self.assertIn("white-space: nowrap;", styles)
        self.assertIn(".status-meta-row", styles)

    def test_open_fill_and_compact_gate_copy_are_visible(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        for text in (
            "开盘买入日",
            "开盘成交",
            "开盘价",
            "成交率低",
            "收益≤0",
            "下界≤0",
            "效用≤0",
        ):
            self.assertIn(text, source)
        self.assertIn('if (prediction.gate_decision === "TRADE") return "通过";', source)
        self.assertIn('if (!reasons.length) return "未过";', source)
        self.assertNotIn("不交易 · ${first}", source)

    def test_zero_candidate_run_is_explicitly_visible_as_completed(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        self.assertIn("D日名单筛选已执行", source)
        self.assertIn("严格交集0支", source)
        self.assertIn("本次执行 ${formatUpdatedAt(run.completed_at)}", source)
        self.assertIn('ledgerFact("已验证", count ? `${finalCount} / ${count}` : "无候选")', source)
        self.assertIn('count ? resultText(day.result, day.is_final) : "合法空选"', source)

    def test_workflow_has_separate_validation_and_output_batches(self) -> None:
        source = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('cron: "20 3 * * 1-5"', source)
        self.assertEqual(source.count("cron:"), 2)
        self.assertIn('cron: "0 11 * * 1-5"', source)
        self.assertIn('cron: "30 13 * * 1-5"', source)
        self.assertIn("timeout-minutes: 150", source)
        self.assertIn("three_table_quant.scheduled_validation", source)
        self.assertIn("python -m three_table_quant.readiness", source)
        self.assertIn("--attempts 25", source)
        self.assertIn("--interval-seconds 300", source)
        self.assertIn("three_table_quant.schedule_guard", source)
        self.assertIn("three_table_quant.batch_result", source)
        self.assertNotIn("github.event_name == 'push' ||", source)
        self.assertIn("github.event_name != 'push'", source)
        self.assertIn("--started-at", source)
        self.assertIn("--market-date", source)
        self.assertIn("SOURCE_TARGET_DATE_NOT_READY", Path("src/three_table_quant/readiness.py").read_text(encoding="utf-8"))
        self.assertIn("--target-decision-date", source)
        self.assertIn("cancel-in-progress: false", source)

    def test_validation_schedule_is_after_t_close_gate(self) -> None:
        workflow = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        config = Path("config/system.json").read_text(encoding="utf-8")
        self.assertIn('"t_validation_after_local_time": "15:10"', config)
        self.assertIn('cron: "0 11 * * 1-5"', workflow)
        self.assertIn("19:00 Asia/Shanghai", workflow)
        shanghai = ZoneInfo("Asia/Shanghai")
        self.assertEqual(
            datetime(2026, 8, 10, 11, 0, tzinfo=timezone.utc)
            .astimezone(shanghai)
            .strftime("%H:%M"),
            "19:00",
        )
        self.assertEqual(
            datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)
            .astimezone(shanghai)
            .strftime("%H:%M"),
            "21:30",
        )

    def test_workflow_keeps_one_curated_pages_publication_path(self) -> None:
        source = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python scripts/verify_pages_source.py", source)
        self.assertEqual(source.count("actions/configure-pages"), 1)
        self.assertEqual(source.count("actions/upload-pages-artifact"), 1)
        self.assertEqual(source.count("actions/deploy-pages"), 1)
        self.assertIn("uses: actions/deploy-pages@v5", source)
        self.assertIn("pages: write", source)
        self.assertIn("id-token: write", source)

        artifact_block = source.split("- name: Prepare Pages artifact", 1)[1]
        artifact_block = artifact_block.split("- name: Configure GitHub Pages", 1)[0]
        self.assertIn("data/dashboard.v1.json", artifact_block)
        self.assertIn("data/source_issues.v1.json", artifact_block)
        self.assertNotIn("data/state.v1.json", artifact_block)
        self.assertNotIn("data/execution_truth.v1.json", artifact_block)
        self.assertNotIn("data/model_registry.v1.json", artifact_block)
        self.assertNotIn("cp -R data", artifact_block)

    def test_status_title_is_rendered_once(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        block = re.search(
            r"function renderStatus\(\) \{(.*?)\n\}",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(block)
        self.assertEqual(
            block.group(1).count(
                'copy.append(node("h2", title, "status-title"));'
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()

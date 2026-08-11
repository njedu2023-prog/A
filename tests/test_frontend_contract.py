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
        self.assertIn("styles.css?v=20260811-2", source)
        self.assertIn("app.js?v=20260811-2", source)

    def test_active_transparent_model_uses_shadow_baseline_display_name(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        self.assertIn(
            'if (text === "transparent_shadow_champion_v2") return "影子基线 V2";',
            source,
        )
        self.assertNotIn('return "透明冠军 V2";', source)

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
        self.assertIn('ledgerFact("收益验证", count ? `${finalCount} / ${count}` : "无候选")', source)
        self.assertIn('if (!count) return "合法空选";', source)

    def test_exit_status_copy_never_claims_unfinished_validation_succeeded(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        self.assertIn('EXIT_UNVERIFIABLE: "退出证据待补"', source)
        self.assertIn('EXIT_DELAYED: "退出验证中"', source)
        self.assertIn('CLOSED: "收益验证成功"', source)
        self.assertNotIn('EXIT_UNVERIFIABLE: "退出待核验"', source)
        self.assertNotIn('EXIT_DELAYED: "延迟退出"', source)
        self.assertNotIn('CLOSED: "已验证"', source)
        self.assertIn('ledgerFact("收益验证", count ? `${finalCount} / ${count}` : "无候选")', source)
        self.assertIn('const netReturn = item.is_final === true && finite(item.net_return)', source)
        self.assertIn('{text: pct(netReturn), className: tone(netReturn)}', source)
        self.assertIn('"收益验证"', source)

    def test_final_daily_result_shows_directional_return(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        styles = Path("assets/styles.css").read_text(encoding="utf-8")
        self.assertIn("function dailyResultText(day, candidateCount)", source)
        self.assertIn('if (day?.is_final !== true) return "待验证";', source)
        self.assertIn('if (!finite(day?.portfolio_return)) return "待补证";', source)
        self.assertIn('if (value > 0) return `↑ ${pct(value)}`;', source)
        self.assertIn('if (value < 0) return `↓ ${pct(value)}`;', source)
        self.assertIn('return "0.00%";', source)
        self.assertIn('ledgerFact("当日结果", dailyResultText(day, count), `daily-result ${tone(returnValue)}`)', source)
        self.assertIn(".ledger-fact strong.daily-result { font-weight: 700; }", styles)
        self.assertIn(".ledger-fact strong.daily-result.positive { color: var(--red); }", styles)
        self.assertIn(".ledger-fact strong.daily-result.negative { color: var(--green-deep); }", styles)

    def test_workflow_has_separate_validation_and_output_batches(self) -> None:
        source = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('cron: "20 3 * * 1-5"', source)
        self.assertNotIn('cron: "0 11 * * 1-5"', source)
        self.assertNotIn('cron: "30 13 * * 1-5"', source)
        self.assertEqual(source.count("- cron:"), 6)
        for cron in (
            'cron: "40 10 * * 1-5"',
            'cron: "55 10 * * 1-5"',
            'cron: "10 11 * * 1-5"',
            'cron: "10 13 * * 1-5"',
            'cron: "25 13 * * 1-5"',
            'cron: "40 13 * * 1-5"',
        ):
            self.assertIn(cron, source)
        self.assertIn("timeout-minutes: 180", source)
        self.assertIn("three_table_quant.scheduled_validation", source)
        self.assertIn("python -m three_table_quant.readiness", source)
        self.assertIn("--attempts 25", source)
        self.assertIn("--interval-seconds 300", source)
        self.assertIn("id: batch-mode", source)
        self.assertIn("three_table_quant.schedule_guard", source)
        self.assertIn("if: ${{ github.event_name != 'push' }}", source)
        self.assertIn("python -m three_table_quant.automation_gate", source)
        self.assertIn("--dashboard data/dashboard.v1.json", source)
        self.assertIn('args+=(--force)', source)
        self.assertIn("python -m three_table_quant.schedule_clock", source)
        self.assertIn("--timezone Asia/Shanghai", source)
        self.assertIn("--max-wait-seconds 1500", source)
        self.assertLess(
            source.index("three_table_quant.schedule_guard"),
            source.index("three_table_quant.automation_gate"),
        )
        self.assertLess(
            source.index("three_table_quant.automation_gate"),
            source.index("three_table_quant.schedule_clock"),
        )
        self.assertIn(
            "SHOULD_DEPLOY: ${{ github.event_name == 'push' || steps.automation-gate.outputs.should_run == 'true' }}",
            source,
        )
        self.assertIn(
            "if: ${{ github.event_name == 'push' || steps.automation-gate.outputs.should_run == 'true' }}",
            source,
        )
        self.assertIn(
            "steps.automation-gate.outputs.should_run == 'true' && steps.batch-mode.outputs.mode == 'validation'",
            source,
        )

    def test_manual_recovery_date_is_explicit_and_never_conflated_with_force(self) -> None:
        source = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("recovery_date:", source)
        self.assertIn("recovery_date is only supported for validation", source)
        self.assertIn('args+=(--date "$RECOVERY_DATE")', source)
        self.assertIn('DATE_RELATION: ${{ steps.market-day.outputs.date_relation }}', source)
        self.assertIn('if [[ "$DATE_RELATION" == "FUTURE" ]]', source)
        self.assertIn('if [[ "$DATE_RELATION" == "PAST" ]]', source)
        self.assertIn('if [[ "$IS_OPEN" != "true" ]]', source)
        self.assertIn("is_recovery=true", source)
        self.assertIn('echo "is_recovery=$is_recovery"', source)
        clock_start = source.index("- name: Wait until the exact batch time")
        clock_end = source.index("- name: Resolve publication policy", clock_start)
        clock = source[clock_start:clock_end]
        self.assertIn(
            "steps.recovery-mode.outputs.is_recovery != 'true'",
            clock,
        )
        self.assertNotIn("force", clock.lower())
        self.assertIn(
            '--market-date "${{ steps.market-day.outputs.market_date }}"',
            source,
        )
        self.assertGreaterEqual(
            source.count("steps.market-day.outputs.market_date"),
            4,
        )
        self.assertIn(
            "steps.automation-gate.outputs.should_run == 'true' && steps.batch-mode.outputs.mode == 'output'",
            source,
        )
        self.assertIn(
            "TARGET_DECISION_DATE: ${{ steps.market-day.outputs.market_date }}",
            source,
        )
        self.assertIn("three_table_quant.batch_result", source)
        self.assertIn("github.event_name != 'push'", source)
        self.assertIn("type: boolean", source)
        self.assertIn("FORCE_BATCH:", source)
        self.assertIn("--started-at", source)
        self.assertIn("--market-date", source)
        self.assertIn("SOURCE_TARGET_DATE_NOT_READY", Path("src/three_table_quant/readiness.py").read_text(encoding="utf-8"))
        self.assertIn("--target-decision-date", source)
        self.assertIn(
            "--include-dir data/single_stock_minute_archive",
            source,
        )
        self.assertIn('data/security_master.v1.json', source)

    def test_workflow_state_and_pages_concurrency_are_independent(self) -> None:
        source = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        header, jobs = source.split("\njobs:\n", 1)
        build, deploy = jobs.split("\n  deploy:\n", 1)

        self.assertNotIn("\nconcurrency:\n", header)
        self.assertIn("group: three-table-state", build)
        self.assertIn("cancel-in-progress: false", build)
        self.assertIn("uses: actions/checkout@v6", build)
        self.assertLess(
            build.index("group: three-table-state"),
            build.index("uses: actions/checkout@v6"),
        )
        self.assertNotIn("group: github-pages", build)
        self.assertIn("group: github-pages", deploy)
        self.assertIn("cancel-in-progress: false", deploy)
        self.assertNotIn("group: three-table-state", deploy)

        publish = build.split("- name: Publish generated data", 1)[1]
        publish = publish.split("- name: Prepare Pages artifact", 1)[0]
        self.assertIn(
            "steps.automation-gate.outputs.should_run == 'true'", publish
        )

    def test_validation_schedule_is_after_t_close_gate(self) -> None:
        workflow = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        config = Path("config/system.json").read_text(encoding="utf-8")
        self.assertIn('"t_validation_after_local_time": "15:10"', config)
        self.assertIn('echo "not_before=19:00"', workflow)
        self.assertIn('echo "not_before=21:30"', workflow)
        shanghai = ZoneInfo("Asia/Shanghai")
        validation_wakes = ((10, 40), (10, 55), (11, 10))
        output_wakes = ((13, 10), (13, 25), (13, 40))
        self.assertEqual(
            [
                datetime(2026, 8, 10, hour, minute, tzinfo=timezone.utc)
                .astimezone(shanghai)
                .strftime("%H:%M")
                for hour, minute in validation_wakes
            ],
            ["18:40", "18:55", "19:10"],
        )
        self.assertEqual(
            [
                datetime(2026, 8, 10, hour, minute, tzinfo=timezone.utc)
                .astimezone(shanghai)
                .strftime("%H:%M")
                for hour, minute in output_wakes
            ],
            ["21:10", "21:25", "21:40"],
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
        self.assertNotIn("single_stock_minute_archive", artifact_block)
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

    def test_frontend_selects_newest_valid_pages_or_main_snapshot(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        page = Path("index.html").read_text(encoding="utf-8")
        self.assertIn('dashboard: "./data/dashboard.v1.json"', script)
        self.assertIn(
            'dashboard: "https://raw.githubusercontent.com/njedu2023-prog/A/main/data/dashboard.v1.json"',
            script,
        )
        self.assertIn('sourceIssues: "./data/source_issues.v1.json"', script)
        self.assertIn(
            'sourceIssues: "https://raw.githubusercontent.com/njedu2023-prog/A/main/data/source_issues.v1.json"',
            script,
        )
        self.assertIn("Promise.allSettled", script)
        self.assertIn('cache: "no-store"', script)
        self.assertIn("DATA_FETCH_TIMEOUT_MS = 8000", script)
        self.assertIn("signal: controller.signal", script)
        self.assertIn("timestampedUrl(url, requestTimestamp)", script)
        self.assertIn('schema_version !== "dashboard_v1"', script)
        self.assertIn('schema_version !== "source_issues_v1"', script)
        self.assertIn("right.generatedAt - left.generatedAt", script)
        self.assertIn('return left.source === "pages" ? -1 : 1;', script)
        self.assertIn("chooseCompanionSnapshot(sourceIssueCandidates, selectedDashboard.source)", script)
        self.assertNotIn('id="dataSourceStatus"', page)
        self.assertNotIn("dataSourceCopy", script)
        self.assertNotIn("数据来源 Pages", script)
        self.assertNotIn("数据来源 main", script)

    def test_frontend_loader_keeps_either_source_as_a_valid_fallback(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        chooser = re.search(
            r"function chooseNewestSnapshot\(candidates\) \{(.*?)\n\}",
            script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(chooser)
        self.assertIn("candidate?.ok === true", chooser.group(1))
        self.assertIn("if (!valid.length)", chooser.group(1))
        self.assertNotIn("candidates.pages.ok && candidates.main.ok", chooser.group(1))
        companion = re.search(
            r"function chooseCompanionSnapshot\(candidates, preferredSource\) \{(.*?)\n\}",
            script,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(companion)
        self.assertIn("fallbackSource", companion.group(1))


if __name__ == "__main__":
    unittest.main()

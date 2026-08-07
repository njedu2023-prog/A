from __future__ import annotations

import re
import unittest
from pathlib import Path


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
        self.assertIn("styles.css?v=20260807-1", source)
        self.assertIn("app.js?v=20260807-1", source)

    def test_status_metadata_is_split_into_summary_and_source_rows(self) -> None:
        script = Path("assets/app.js").read_text(encoding="utf-8")
        styles = Path("assets/styles.css").read_text(encoding="utf-8")
        self.assertIn("status-meta-row status-meta-summary", script)
        self.assertIn("status-meta-row status-meta-sources", script)
        self.assertIn("meta.append(summaryRow, sourceRow);", script)
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

    def test_workflow_has_only_the_evening_schedule(self) -> None:
        source = Path(".github/workflows/daily-shadow.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('cron: "20 3 * * 1-5"', source)
        self.assertEqual(source.count("cron:"), 1)
        self.assertIn('cron: "30 13 * * 1-5"', source)
        self.assertIn("timeout-minutes: 150", source)
        self.assertIn("python -m three_table_quant.readiness", source)
        self.assertIn("--attempts 25", source)
        self.assertIn("--interval-seconds 300", source)

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

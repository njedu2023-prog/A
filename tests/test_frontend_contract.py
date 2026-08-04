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
            "9:25成交估计",
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

    def test_static_assets_are_cache_busted_for_v2(self) -> None:
        source = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("styles.css?v=20260805-2", source)
        self.assertIn("app.js?v=20260805-2", source)

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

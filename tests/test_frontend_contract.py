from __future__ import annotations

import re
import unittest
from pathlib import Path


class FrontendContractTests(unittest.TestCase):
    def test_expected_return_is_between_stock_and_promotion_target(self) -> None:
        source = Path("assets/app.js").read_text(encoding="utf-8")
        match = re.search(
            r"const candidateTable = table\(\[(.*?)\], rows,",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        headers = match.group(1)
        stock = headers.index(
            '{text: "股票", align: "left", className: "sticky-name"}'
        )
        expected_return = headers.index('"预期净收益"')
        promotion_target = headers.index('"晋级目标"')
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
        self.assertIn('className: "code sticky-code"', source)
        self.assertIn('className: "name sticky-name"', source)
        self.assertIn('className: "sticky-stock"', source)
        self.assertIn('heading.scope = "col";', source)
        self.assertIn('detailWrap.setAttribute("role", "region");', source)
        self.assertIn(
            'dailyTable.classList.add("daily-detail-table");',
            source,
        )

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

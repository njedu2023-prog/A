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
        stock = headers.index('{text: "股票", align: "left"}')
        expected_return = headers.index('"预期净收益"')
        promotion_target = headers.index('"晋级目标"')
        self.assertLess(stock, expected_return)
        self.assertLess(expected_return, promotion_target)


if __name__ == "__main__":
    unittest.main()

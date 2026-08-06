from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.verify_pages_source import verify_pages_source


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class VerifyPagesSourceTests(unittest.TestCase):
    @patch("scripts.verify_pages_source.urllib.request.urlopen")
    def test_actions_source_is_accepted(self, urlopen: object) -> None:
        urlopen.return_value = _Response(b'{"build_type":"workflow"}')
        self.assertEqual(verify_pages_source("owner/repo", "token"), "workflow")

    @patch("scripts.verify_pages_source.urllib.request.urlopen")
    def test_branch_source_is_rejected(self, urlopen: object) -> None:
        urlopen.return_value = _Response(b'{"build_type":"legacy"}')
        with self.assertRaisesRegex(RuntimeError, "would race"):
            verify_pages_source("owner/repo", "token")


if __name__ == "__main__":
    unittest.main()

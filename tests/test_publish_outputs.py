from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts import publish_outputs


class PublishOutputsTests(unittest.TestCase):
    def test_changed_remote_parent_blocks_before_any_write(self) -> None:
        calls: list[tuple] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            return {"object": {"sha": "new-parent"}}

        with patch.object(publish_outputs.Path, "read_bytes", return_value=b"payload"), patch.object(
            publish_outputs, "request", side_effect=fake_request
        ):
            with self.assertRaisesRegex(RuntimeError, "refusing to publish stale"):
                publish_outputs.publish_files(
                    ["data/dashboard.v1.json"],
                    "main",
                    "checked-out-parent",
                    "test",
                    "token",
                    "owner/repo",
                )
        self.assertEqual([method for method, _, _ in calls], ["GET"])

    def test_identical_files_return_no_changes_without_commit(self) -> None:
        contents = {
            "data/state.v1.json": b"state payload\n",
            "data/dashboard.v1.json": b"dashboard payload\n",
            "data/source_issues.v1.json": b"issues payload\n",
        }
        calls: list[tuple] = []

        def fake_request(method, url, token, payload=None):
            calls.append((method, url, payload))
            if "/git/ref/heads/" in url:
                return {"object": {"sha": "parent"}}
            if "/git/commits/" in url:
                return {"tree": {"sha": "tree"}}
            if "/git/trees/" in url:
                return {
                    "truncated": False,
                    "tree": [
                        {
                            "path": path,
                            "type": "blob",
                            "sha": publish_outputs._git_blob_sha(content),
                        }
                        for path, content in contents.items()
                    ],
                }
            raise AssertionError((method, url, payload))

        files = list(contents)
        with patch.object(publish_outputs.Path, "read_bytes", side_effect=contents.values()), patch.object(
            publish_outputs, "request", side_effect=fake_request
        ):
            result = publish_outputs.publish_files(
                files,
                "main",
                "parent",
                "test",
                "token",
                "owner/repo",
            )
        self.assertEqual(result["status"], "no_changes")
        self.assertIsNone(result["commit"])
        self.assertEqual(result["files"], files)
        self.assertNotIn("POST", [method for method, _, _ in calls])
        self.assertNotIn("PATCH", [method for method, _, _ in calls])

    def test_changed_files_are_published_in_one_tree_and_commit(self) -> None:
        contents = [b"one", b"two", b"three"]
        calls: list[tuple] = []
        blob_index = 0

        def fake_request(method, url, token, payload=None):
            nonlocal blob_index
            calls.append((method, url, payload))
            if method == "GET" and "/git/ref/heads/" in url:
                return {"object": {"sha": "parent"}}
            if method == "GET" and "/git/commits/" in url:
                return {"tree": {"sha": "base-tree"}}
            if method == "GET" and "/git/trees/" in url:
                return {"truncated": False, "tree": []}
            if method == "POST" and url.endswith("/git/blobs"):
                blob_index += 1
                return {"sha": f"blob-{blob_index}"}
            if method == "POST" and url.endswith("/git/trees"):
                return {"sha": "new-tree"}
            if method == "POST" and url.endswith("/git/commits"):
                self.assertEqual(payload["parents"], ["parent"])
                self.assertEqual(payload["tree"], "new-tree")
                return {"sha": "new-commit"}
            if method == "PATCH" and "/git/refs/heads/" in url:
                self.assertEqual(payload, {"sha": "new-commit", "force": False})
                return {"object": {"sha": "new-commit"}}
            raise AssertionError((method, url, payload))

        files = ["data/state.v1.json", "data/dashboard.v1.json", "data/source_issues.v1.json"]
        with patch.object(publish_outputs.Path, "read_bytes", side_effect=contents), patch.object(
            publish_outputs, "request", side_effect=fake_request
        ):
            result = publish_outputs.publish_files(
                files,
                "main",
                "parent",
                "test",
                "token",
                "owner/repo",
            )
        self.assertEqual(result["status"], "published")
        tree_payloads = [payload for method, url, payload in calls if method == "POST" and url.endswith("/git/trees")]
        self.assertEqual(len(tree_payloads), 1)
        self.assertEqual([item["path"] for item in tree_payloads[0]["tree"]], files)
        self.assertEqual(sum(method == "POST" and url.endswith("/git/commits") for method, url, _ in calls), 1)
        self.assertEqual(sum(method == "PATCH" for method, _, _ in calls), 1)


if __name__ == "__main__":
    unittest.main()

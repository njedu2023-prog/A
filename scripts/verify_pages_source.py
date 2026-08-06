#!/usr/bin/env python3
"""Fail closed unless GitHub Pages is published by the controlled workflow."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


API_VERSION = "2022-11-28"
EXPECTED_BUILD_TYPE = "workflow"


def fetch_pages_site(repository: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pages",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "three-table-quant-actions",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub Pages settings lookup failed: HTTP {exc.code}: {body[:300]}"
        ) from exc


def verify_pages_source(repository: str, token: str) -> str:
    build_type = str(fetch_pages_site(repository, token).get("build_type") or "")
    if build_type != EXPECTED_BUILD_TYPE:
        raise RuntimeError(
            "GitHub Pages source must be GitHub Actions "
            f"(build_type={EXPECTED_BUILD_TYPE!r}); observed {build_type!r}. "
            "Branch publishing would race the controlled Pages deployment."
        )
    return build_type


def main() -> None:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repository or not token:
        raise SystemExit("GITHUB_REPOSITORY and GITHUB_TOKEN are required")
    build_type = verify_pages_source(repository, token)
    print(json.dumps({"status": "ok", "pages_build_type": build_type}))


if __name__ == "__main__":
    main()

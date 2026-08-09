#!/usr/bin/env python3
"""Atomically publish generated data through the GitHub Git Data API.

This deliberately avoids an ordinary git push. It creates one tree/commit and
moves the configured branch only when the workflow still owns the observed
head, so concurrent source runs cannot silently overwrite each other.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def request(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "three-table-quant-actions",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url} failed: HTTP {exc.code}: {body[:500]}") from exc


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _safe_repository_path(value: str, *, field_name: str) -> Path:
    path = Path(value)
    if not value or not path.parts or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be relative and contained: {value}")
    return path


def collect_publish_files(
    files: list[str],
    include_dirs: list[str] | None = None,
    *,
    repository_root: str | Path = ".",
) -> list[str]:
    """Expand optional directories without allowing repository escapes.

    Missing optional directories are legitimate (for example a zero-candidate
    day before the minute archive exists). Hidden atomic-write scratch files are
    never publishable artifacts.
    """

    root = Path(repository_root).resolve()
    result: list[str] = []
    seen: set[str] = set()

    def add_file(path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"publish path escapes repository: {path}") from exc
        if not resolved.is_file():
            raise ValueError(f"publish path must be a regular file: {path}")
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"publish path escapes repository: {path}") from exc
        repo_path = relative.as_posix()
        if repo_path not in seen:
            seen.add(repo_path)
            result.append(repo_path)

    for item in files:
        relative = _safe_repository_path(item, field_name="repository path")
        add_file(root / relative)

    for item in include_dirs or []:
        relative = _safe_repository_path(item, field_name="include directory")
        directory = root / relative
        if not directory.exists():
            continue
        try:
            resolved_directory = directory.resolve(strict=True)
            resolved_directory.relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError(f"include directory escapes repository: {item}") from exc
        if not resolved_directory.is_dir():
            raise ValueError(f"include directory must be a directory: {item}")
        for child in sorted(directory.rglob("*"), key=lambda value: value.as_posix()):
            child_relative = child.relative_to(directory)
            try:
                resolved_child = child.resolve(strict=True)
                resolved_child.relative_to(root)
            except (OSError, ValueError) as exc:
                raise ValueError(f"included path escapes repository: {child}") from exc
            if any(part.startswith(".") for part in child_relative.parts):
                continue
            if resolved_child.is_dir():
                continue
            add_file(child)
    return result


def publish_files(
    files: list[str],
    branch: str,
    expected_parent: str,
    message: str,
    token: str,
    repository: str,
) -> dict[str, Any]:
    prepared: list[tuple[str, bytes]] = []
    seen_paths: set[str] = set()
    repository_root = Path.cwd().resolve()
    for item in files:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"repository path must be relative and contained: {item}")
        try:
            path.resolve().relative_to(repository_root)
        except ValueError as exc:
            raise ValueError(f"repository path escapes working tree: {item}") from exc
        repo_path = path.as_posix()
        if repo_path in seen_paths:
            raise ValueError(f"duplicate repository path: {repo_path}")
        seen_paths.add(repo_path)
        prepared.append((repo_path, path.read_bytes()))

    api = f"https://api.github.com/repos/{repository}"
    encoded_branch = urllib.parse.quote(branch, safe="")
    ref_url = f"{api}/git/ref/heads/{encoded_branch}"
    ref = request("GET", ref_url, token)
    observed_parent = ref["object"]["sha"]
    if observed_parent != expected_parent:
        raise RuntimeError(
            "remote branch changed after checkout; refusing to publish stale generated state "
            f"(expected {expected_parent}, observed {observed_parent})"
        )

    parent = request("GET", f"{api}/git/commits/{expected_parent}", token)
    base_tree = parent["tree"]["sha"]
    parent_tree = request("GET", f"{api}/git/trees/{base_tree}?recursive=1", token)
    remote_blobs = {
        item["path"]: item["sha"]
        for item in parent_tree.get("tree", [])
        if item.get("type") == "blob"
    }
    changed = (
        prepared
        if parent_tree.get("truncated", False)
        else [
            (path, content)
            for path, content in prepared
            if remote_blobs.get(path) != _git_blob_sha(content)
        ]
    )
    if not changed:
        latest_ref = request("GET", ref_url, token)
        latest_parent = latest_ref["object"]["sha"]
        if latest_parent != expected_parent:
            raise RuntimeError(
                "remote branch changed during no-change verification; refusing stale result "
                f"(expected {expected_parent}, observed {latest_parent})"
            )
        return {
            "status": "no_changes",
            "parent": expected_parent,
            "commit": None,
            "files": [path for path, _ in prepared],
        }

    elements = []
    for repo_path, content in changed:
        blob = request(
            "POST",
            f"{api}/git/blobs",
            token,
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        elements.append({"path": repo_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = request("POST", f"{api}/git/trees", token, {"base_tree": base_tree, "tree": elements})
    commit = request(
        "POST",
        f"{api}/git/commits",
        token,
        {"message": message, "tree": tree["sha"], "parents": [expected_parent]},
    )
    request(
        "PATCH",
        f"{api}/git/refs/heads/{encoded_branch}",
        token,
        {"sha": commit["sha"], "force": False},
    )
    return {
        "status": "published",
        "parent": expected_parent,
        "commit": commit["sha"],
        "files": [path for path, _ in prepared],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument(
        "--include-dir",
        action="append",
        default=[],
        help="optionally publish every contained non-hidden file under this directory",
    )
    parser.add_argument("--branch", default=os.environ.get("GITHUB_REF_NAME", "main"))
    parser.add_argument("--expected-parent", required=True)
    parser.add_argument("--message", default="Update three-table shadow data")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repository:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY are required")
    result = publish_files(
        collect_publish_files(args.files, args.include_dir),
        args.branch,
        args.expected_parent,
        args.message,
        token,
        repository,
    )
    print(json.dumps(result))


if __name__ == "__main__":
    main()

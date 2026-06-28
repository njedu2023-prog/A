from __future__ import annotations

import re
import shutil
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Callable


def archive_html_report(report: str, input_pdf: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = _safe_stem(input_pdf.stem)
    archive_path = archive_dir / f"{timestamp}_{stem}.html"
    archive_path.write_text(report, encoding="utf-8")
    shutil.copyfile(archive_path, archive_dir / "latest.html")
    _write_index(archive_dir)
    return archive_path


def archive_html_report_pages(page_builder: Callable[[str], dict[str, str]], input_pdf: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = _safe_stem(input_pdf.stem)
    archive_base = f"{timestamp}_{stem}"
    archive_pages = page_builder(archive_base)
    latest_pages = page_builder("latest")
    for filename, html in archive_pages.items():
        (archive_dir / filename).write_text(html, encoding="utf-8")
    for filename, html in latest_pages.items():
        (archive_dir / filename).write_text(html, encoding="utf-8")
    _write_index(archive_dir)
    return archive_dir / f"{archive_base}.html"


def _write_index(archive_dir: Path) -> None:
    reports = sorted(
        (path for path in archive_dir.glob("*.html") if _is_report_home_page(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    rows = "\n".join(
        f"<tr><td>{escape(_display_time(path.name))}</td><td><a href=\"{escape(path.name)}\">{escape(path.name)}</a></td></tr>"
        for path in reports
    )
    if not rows:
        rows = "<tr><td>-</td><td>暂无历史报告</td></tr>"
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A股T+1预测引擎首页</title>
  <style>
    body {{ margin: 0; background: #f5f5f7; color: #1d1d1f; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif; }}
    .nav {{ height: 44px; display: flex; align-items: center; justify-content: center; gap: 28px; background: rgba(251,251,253,.78); backdrop-filter: saturate(180%) blur(20px); border-bottom: 1px solid rgba(0,0,0,.08); font-size: 12px; }}
    main {{ max-width: 1080px; margin: 12px auto 40px; padding: 0 12px; }}
    section {{ background: #fff; padding: 42px 34px; margin-bottom: 12px; }}
    h1 {{ margin: 0 0 8px; font-size: 44px; line-height: 1.08; text-align: center; }}
    h2 {{ margin: 0 0 16px; font-size: 28px; text-align: center; }}
    .subtitle {{ color: #6e6e73; text-align: center; }}
    a {{ color: #06c; text-decoration: none; }}
    a:hover {{ color: #004f9f; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 28px; }}
    th, td {{ border-bottom: 1px solid #d2d2d7; padding: 12px; text-align: left; font-size: 14px; }}
    th {{ color: #6e6e73; font-weight: 600; }}
    .latest, .upload {{ display: block; width: fit-content; margin: 22px auto 0; padding: 10px 18px; border-radius: 999px; background: #0071e3; color: #fff; }}
    .upload {{ background: #f5f5f7; color: #1d1d1f; }}
  </style>
</head>
<body>
  <nav class="nav"><a href="index.html">首页</a><a href="#upload">上传PDF</a><a href="#reports">报告列表</a><a href="latest.html">最新报告</a></nav>
  <main>
    <section>
      <h1>A股T+1预测引擎首页</h1>
      <div class="subtitle">保留每次预测报告，可随时回看历史版本</div>
      <a class="latest" href="latest.html">打开最新报告</a>
    </section>
    <section id="upload">
      <h2>上传 PDF</h2>
      <div class="subtitle">上传入口需要启动本机服务：运行 <code>a-share-t1-web --open-browser</code>，或双击项目里的 macOS 启动器。</div>
      <a class="upload" href="http://127.0.0.1:8765/">打开本机上传首页</a>
    </section>
    <section id="reports">
      <h2>报告列表</h2>
      <table>
        <thead><tr><th>生成时间</th><th>报告文件</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""
    (archive_dir / "index.html").write_text(html, encoding="utf-8")


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "report"


def _is_report_home_page(filename: str) -> bool:
    if filename in {"index.html", "latest.html"} or filename.startswith("latest_"):
        return False
    return not re.search(r"_(dashboard|limit|overnight|continuation|validation)\.html$", filename)


def _display_time(filename: str) -> str:
    match = re.match(r"(\d{8})_(\d{6})_", filename)
    if not match:
        return "-"
    date, time = match.groups()
    return f"{date[:4]}-{date[4:6]}-{date[6:8]} {time[:2]}:{time[2:4]}:{time[4:6]}"

from __future__ import annotations

import argparse
import html
import mimetypes
import re
import shutil
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .archive import archive_html_report_pages
from .pipeline import analyze_pdf
from .report_pack import render_report_pack_pages
from .predictions import save_prediction_snapshot


@dataclass(frozen=True)
class WebSettings:
    host: str
    port: int
    root: Path
    archive_dir: Path
    prediction_dir: Path
    upload_dir: Path
    config_path: Path | None
    events_path: Path | None
    sentiment_search_path: Path | None
    ths_data_path: Path | None
    calibration_path: Path | None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local A-share T+1 report web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--archive-dir", type=Path, default=Path("outputs/html_reports"))
    parser.add_argument("--prediction-dir", type=Path, default=Path("outputs/predictions"))
    parser.add_argument("--upload-dir", type=Path, default=Path("outputs/uploads"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--events", type=Path)
    parser.add_argument("--sentiment-search", type=Path)
    parser.add_argument("--ths-data", type=Path)
    parser.add_argument("--calibration", type=Path, default=Path("outputs/calibration/base_probabilities.yaml"))
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    ths_data_path = _default_ths_data_path(root, args.ths_data)
    settings = WebSettings(
        host=args.host,
        port=args.port,
        root=root,
        archive_dir=_resolve(root, args.archive_dir),
        prediction_dir=_resolve(root, args.prediction_dir),
        upload_dir=_resolve(root, args.upload_dir),
        config_path=_optional_resolve(root, args.config),
        events_path=_optional_resolve(root, args.events),
        sentiment_search_path=_optional_resolve(root, args.sentiment_search),
        ths_data_path=ths_data_path,
        calibration_path=_optional_resolve(root, args.calibration),
    )

    handler = _handler_factory(settings)
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    url = f"http://{settings.host}:{settings.port}/"
    print(f"A-share T+1 local web app: {url}")
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    server.serve_forever()


def _handler_factory(settings: WebSettings) -> type[BaseHTTPRequestHandler]:
    class T1RequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(_home_html(settings))
                return
            if parsed.path == "/reports/index.html":
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.end_headers()
                return
            if parsed.path.startswith("/reports/"):
                name = unquote(parsed.path.removeprefix("/reports/"))
                self._send_file(settings.archive_dir / name)
                return
            if parsed.path == "/latest":
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/reports/latest.html")
                self.end_headers()
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/upload":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return
            try:
                uploaded_pdf = self._save_uploaded_pdf()
                result = analyze_pdf(
                    uploaded_pdf,
                    settings.config_path,
                    settings.events_path,
                    settings.sentiment_search_path,
                    settings.calibration_path,
                    settings.ths_data_path,
                )
                archive_path = archive_html_report_pages(
                    lambda base_name: render_report_pack_pages(result.scored, result.sectors, result.config, uploaded_pdf, base_name, result.metadata),
                    uploaded_pdf,
                    settings.archive_dir,
                )
                save_prediction_snapshot(result.scored, result.config, uploaded_pdf, settings.prediction_dir, result.metadata)
            except Exception as exc:  # pragma: no cover - surfaced in browser during manual use
                self._send_html(_error_html(str(exc)), status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/reports/{quote(archive_path.name)}")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            print(f"{self.address_string()} - {format % args}")

        def _save_uploaded_pdf(self) -> Path:
            content_type = self.headers.get("Content-Type", "")
            boundary = _multipart_boundary(content_type)
            if not boundary:
                raise ValueError("上传请求缺少 multipart boundary")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            filename, payload = _extract_pdf_part(body, boundary)
            if not payload:
                raise ValueError("没有读取到 PDF 内容")
            settings.upload_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _safe_upload_name(filename)
            path = settings.upload_dir / f"{datetime.now():%Y%m%d_%H%M%S}" / safe_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            return path

        def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_file(self, path: Path) -> None:
            if not path.exists() or not path.is_file() or not _is_relative_to(path.resolve(), settings.archive_dir.resolve()):
                self.send_error(HTTPStatus.NOT_FOUND, "Report not found")
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return T1RequestHandler


def _home_html(settings: WebSettings) -> str:
    reports = _report_rows(settings.archive_dir)
    latest_link = '<a class="primary" href="/latest">打开最新报告</a>' if (settings.archive_dir / "latest.html").exists() else ""
    ths_hint = html.escape(str(settings.ths_data_path)) if settings.ths_data_path else "未配置"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>T+1三系统本地引擎</title>
  <style>
    body {{ margin: 0; background: #f5f5f7; color: #1d1d1f; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", "Microsoft YaHei", sans-serif; line-height: 1.55; -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility; }}
    nav {{ position: sticky; top: 0; height: 46px; display: flex; align-items: center; justify-content: center; gap: 26px; background: rgba(251,251,253,.82); backdrop-filter: saturate(180%) blur(20px); border-bottom: 1px solid rgba(0,0,0,.08); z-index: 2; }}
    nav a {{ color: #1d1d1f; text-decoration: none; font-size: 13px; opacity: .78; white-space: nowrap; }}
    nav a:hover {{ opacity: 1; }}
    header {{ background: #fff; padding: 56px 24px 40px; text-align: center; }}
    h1 {{ margin: 0; font-size: 56px; line-height: 1.03; letter-spacing: 0; font-weight: 700; }}
    .subtitle {{ max-width: 760px; margin: 12px auto 0; color: #6e6e73; font-size: 17px; }}
    main {{ max-width: 1120px; margin: 14px auto 44px; padding: 0 14px; }}
    section {{ background: #fff; padding: 38px 30px; margin-bottom: 14px; }}
    h2 {{ margin: 0 0 20px; font-size: 28px; line-height: 1.16; text-align: center; }}
    .actions {{ display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; margin-top: 24px; }}
    .primary, button {{ border: 0; border-radius: 999px; background: #0071e3; color: #fff; padding: 11px 20px; font-size: 14px; text-decoration: none; cursor: pointer; }}
    .secondary {{ border-radius: 999px; background: #f5f5f7; color: #1d1d1f; padding: 11px 20px; font-size: 14px; text-decoration: none; }}
    form {{ max-width: 680px; margin: 0 auto; display: grid; gap: 16px; }}
    .systems {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 24px; }}
    .system-card {{ background: #fbfbfd; padding: 22px; min-height: 150px; display: flex; flex-direction: column; justify-content: space-between; }}
    .system-card strong {{ display: block; font-size: 19px; line-height: 1.2; margin-bottom: 10px; }}
    input[type=file] {{ width: 100%; box-sizing: border-box; padding: 18px; border: 1px solid #d2d2d7; background: #f5f5f7; font-size: 14px; }}
    .hint {{ color: #6e6e73; font-size: 13px; text-align: center; line-height: 1.5; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid #d2d2d7; padding: 11px 12px; text-align: left; font-size: 13px; line-height: 1.45; }}
    th {{ color: #6e6e73; font-weight: 600; }}
    tbody tr:nth-child(odd) td {{ background: rgba(245,245,247,.36); }}
    a {{ color: #06c; text-decoration: none; }}
    @media (max-width: 720px) {{ header {{ padding: 40px 18px 30px; }} h1 {{ font-size: 38px; }} h2 {{ font-size: 24px; }} section {{ padding: 28px 18px; }} nav {{ justify-content: flex-start; overflow-x: auto; padding: 0 14px; gap: 18px; }} .systems {{ grid-template-columns: 1fr; }} th, td {{ font-size: 12px; padding: 9px 10px; }} }}
  </style>
</head>
<body>
  <nav>
    <a href="/">首页</a>
    <a href="#upload">上传PDF</a>
    <a href="#reports">报告列表</a>
    <a href="/latest">最新报告</a>
  </nav>
  <header>
    <h1>T+1三系统本地引擎</h1>
    <div class="subtitle">一份 PDF 输入，生成连板概率、隔夜单/EV、最终承接三套报告</div>
    <div class="actions">{latest_link}<a class="secondary" href="#upload">上传新 PDF</a></div>
  </header>
  <main>
    <section>
      <h2>三套系统</h2>
      <div class="systems">
        <div class="system-card"><strong>连板概率</strong><span class="hint">回答哪四票更可能晋级连板。</span></div>
        <div class="system-card"><strong>隔夜单 / EV单</strong><span class="hint">回答谁适合隔夜，谁容易买贵，谁只能EV观察。</span></div>
        <div class="system-card"><strong>最终承接</strong><span class="hint">回答T买入到T+1卖出的风险调整后可兑现收益排序。</span></div>
      </div>
    </section>
    <section id="upload">
      <h2>上传 PDF</h2>
      <form method="post" action="/upload" enctype="multipart/form-data">
        <input type="file" name="pdf" accept="application/pdf,.pdf" required>
        <button type="submit">生成三系统报告包</button>
        <div class="hint">PDF 只在本机处理；报告写入 {html.escape(str(settings.archive_dir))}；同花顺补充数据：{ths_hint}</div>
      </form>
    </section>
    <section id="reports">
      <h2>报告列表</h2>
      <table>
        <thead><tr><th>生成时间</th><th>报告</th><th>操作</th></tr></thead>
        <tbody>{reports}</tbody>
      </table>
    </section>
  </main>
</body>
</html>
"""


def _error_html(message: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>处理失败</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f5f5f7;color:#1d1d1f;padding:40px;">
<h1>处理失败</h1><p>{html.escape(message)}</p><p><a href="/">返回首页</a></p></body></html>"""


def _report_rows(archive_dir: Path) -> str:
    reports = sorted(
        (path for path in archive_dir.glob("*.html") if path.name not in {"index.html", "latest.html"}),
        key=lambda path: path.name,
        reverse=True,
    )
    if not reports:
        return '<tr><td>-</td><td>暂无历史报告</td><td>-</td></tr>'
    rows = []
    for path in reports:
        name = html.escape(path.name)
        rows.append(
            f'<tr><td>{html.escape(_display_time(path.name))}</td><td>{name}</td>'
            f'<td><a href="/reports/{quote(path.name)}">打开</a></td></tr>'
        )
    return "\n".join(rows)


def _multipart_boundary(content_type: str) -> bytes:
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            value = part.split("=", 1)[1].strip('"')
            return value.encode("utf-8")
    return b""


def _extract_pdf_part(body: bytes, boundary: bytes) -> tuple[str, bytes]:
    marker = b"--" + boundary
    for raw_part in body.split(marker):
        if b'name="pdf"' not in raw_part:
            continue
        header_blob, _, payload = raw_part.partition(b"\r\n\r\n")
        filename = _filename_from_headers(header_blob.decode("utf-8", errors="ignore"))
        payload = payload.rstrip(b"\r\n")
        if payload.endswith(b"--"):
            payload = payload[:-2].rstrip(b"\r\n")
        return filename, payload
    return "upload.pdf", b""


def _filename_from_headers(headers: str) -> str:
    match = re.search(r'filename="([^"]*)"', headers)
    if match:
        return match.group(1) or "upload.pdf"
    match = re.search(r"filename=([^\r\n;]+)", headers)
    if match:
        return match.group(1).strip() or "upload.pdf"
    return "upload.pdf"


def _safe_upload_name(filename: str) -> str:
    name = Path(filename).name
    cleaned = "".join(char if char.isalnum() or char in "._-\u4e00-\u9fa5" else "_" for char in name).strip("._")
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned or "upload.pdf"


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _optional_resolve(root: Path, path: Path | None) -> Path | None:
    if path is None:
        return None
    return _resolve(root, path)


def _default_ths_data_path(root: Path, path: Path | None) -> Path | None:
    if path is not None:
        return _resolve(root, path)
    default_path = root / "data" / "ths_sector_flows_latest.csv"
    return default_path if default_path.exists() else None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _display_time(filename: str) -> str:
    if len(filename) < 15 or filename[8] != "_" or filename[15] != "_":
        return "-"
    date = filename[:8]
    time = filename[9:15]
    return f"{date[:4]}-{date[4:6]}-{date[6:8]} {time[:2]}:{time[2:4]}:{time[4:6]}"


if __name__ == "__main__":
    main()

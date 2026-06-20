#!/usr/bin/env python3
"""Export Kidsnote Notion data directly to a static Cloudflare Pages site."""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGES_FILE_LIMIT = 20_000
PAGES_FILE_SIZE_LIMIT = 25 * 1024 * 1024

REPORT_ID_CANDIDATES = (
    "Report ID", "리포트 ID", "리포트id", "report_id", "보고서 ID",
    "번호", "숫자", "Number", "id", "ID",
)
DATE_CANDIDATES = ("Date", "날짜")


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env_value(env_file_values: dict[str, str], key: str, default: str = "") -> str:
    return os.environ.get(key) or env_file_values.get(key) or default


def require_value(env_file_values: dict[str, str], key: str) -> str:
    value = env_value(env_file_values, key)
    if not value:
        raise SystemExit(f"Missing required setting: {key}")
    return value


def first_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def rich_text_to_html(rich_text: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in rich_text or []:
        plain = item.get("plain_text") or ""
        text = html.escape(plain).replace("\n", "<br>")
        annotations = item.get("annotations") or {}
        href = item.get("href")
        if annotations.get("code"):
            text = f"<code>{text}</code>"
        if annotations.get("bold"):
            text = f"<strong>{text}</strong>"
        if annotations.get("italic"):
            text = f"<em>{text}</em>"
        if annotations.get("strikethrough"):
            text = f"<s>{text}</s>"
        if href:
            text = f'<a href="{html.escape(href, quote=True)}">{text}</a>'
        parts.append(text)
    return "".join(parts)


def plain_text(rich_text: list[dict[str, Any]]) -> str:
    return "".join((item.get("plain_text") or "") for item in rich_text or [])


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def html_excerpt(value: str, size: int = 160) -> str:
    value = strip_html(value)
    if len(value) <= size:
        return value
    return value[:size].rstrip() + "..."


def safe_filename(name: str, fallback: str) -> str:
    name = name.strip() or fallback
    name = re.sub(r"[\\/:*?\"<>|]+", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or fallback


def infer_extension(content_type: str, url: str) -> str:
    path_ext = Path(urlparse(url).path).suffix
    if path_ext:
        return path_ext.split("?")[0]
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return guessed or ".bin"


def post_type(title: str) -> str:
    if "앨범" in title:
        return "album"
    if "알림장" in title:
        return "daily"
    return "announcement"


def post_type_label(value: str) -> str:
    return {
        "daily": "알림장",
        "album": "앨범",
        "announcement": "공지",
    }.get(value, "공지")


def date_parts(date: str | None) -> tuple[str, str]:
    if not date:
        return ("unknown", "unknown")
    try:
        parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(date[:10], "%Y-%m-%d")
        except ValueError:
            return ("unknown", "unknown")
    return (f"{parsed.year:04d}", f"{parsed.month:02d}")


def display_date(date: str | None) -> str:
    if not date:
        return ""
    try:
        parsed = datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(date[:10], "%Y-%m-%d")
        except ValueError:
            return date
    return parsed.strftime("%Y.%m.%d")


@dataclass
class NotionPage:
    page_id: str
    title: str
    report_id: int
    date: str | None
    url: str


class NotionClient:
    def __init__(self, token: str, database_id: str) -> None:
        self.database_id = database_id.replace("-", "")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self.title_prop: str | None = None
        self.report_id_prop: str | None = None
        self.date_prop: str | None = None

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, f"{NOTION_API}{path}", timeout=90, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"Notion API {method} {path} failed: {response.status_code} {response.text[:800]}")
        return response

    def resolve_schema(self) -> None:
        data = self.request("GET", f"/databases/{self.database_id}").json()
        props = data.get("properties") or {}
        for name, prop in props.items():
            ptype = prop.get("type")
            if ptype == "title" and self.title_prop is None:
                self.title_prop = name
            if ptype == "number" and name in REPORT_ID_CANDIDATES and self.report_id_prop is None:
                self.report_id_prop = name
            if ptype == "date" and name in DATE_CANDIDATES and self.date_prop is None:
                self.date_prop = name

        if self.report_id_prop is None:
            number_props = [name for name, prop in props.items() if prop.get("type") == "number"]
            if len(number_props) == 1:
                self.report_id_prop = number_props[0]

        if self.date_prop is None:
            date_props = [name for name, prop in props.items() if prop.get("type") == "date"]
            if len(date_props) == 1:
                self.date_prop = date_props[0]

        if not self.title_prop:
            raise RuntimeError("Could not find a title property in the Notion database.")
        if not self.report_id_prop:
            raise RuntimeError("Could not find a number property for Report ID / 번호.")

    def query_pages(self, limit: int | None) -> list[NotionPage]:
        if self.title_prop is None or self.report_id_prop is None:
            self.resolve_schema()

        body: dict[str, Any] = {"page_size": 100}
        if self.date_prop:
            body["sorts"] = [{"property": self.date_prop, "direction": "descending"}]

        pages: list[NotionPage] = []
        start_cursor: str | None = None
        while True:
            if start_cursor:
                body["start_cursor"] = start_cursor
            data = self.request("POST", f"/databases/{self.database_id}/query", json=body).json()
            for raw in data.get("results") or []:
                props = raw.get("properties") or {}
                report_id = self.extract_report_id(props)
                if report_id is None or report_id < 0:
                    continue
                pages.append(NotionPage(
                    page_id=raw["id"],
                    title=self.extract_title(props) or "Kidsnote",
                    report_id=report_id,
                    date=self.extract_date(props),
                    url=raw.get("url") or "",
                ))
                if limit is not None and len(pages) >= limit:
                    return pages
            if not data.get("has_more"):
                return pages
            start_cursor = data.get("next_cursor")

    def extract_title(self, props: dict[str, Any]) -> str:
        if not self.title_prop:
            return ""
        return plain_text((props.get(self.title_prop) or {}).get("title") or [])

    def extract_report_id(self, props: dict[str, Any]) -> int | None:
        if not self.report_id_prop:
            return None
        number = (props.get(self.report_id_prop) or {}).get("number")
        if number is None:
            return None
        try:
            return int(number)
        except (TypeError, ValueError):
            return None

    def extract_date(self, props: dict[str, Any]) -> str | None:
        if not self.date_prop:
            return None
        date_obj = (props.get(self.date_prop) or {}).get("date") or {}
        return date_obj.get("start")

    def children(self, block_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"/blocks/{block_id}/children", params=params).json()
            out.extend(data.get("results") or [])
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")


class StaticRenderer:
    def __init__(self, notion: NotionClient, out_dir: Path, download_media: bool) -> None:
        self.notion = notion
        self.out_dir = out_dir
        self.download_media = download_media
        self.session = requests.Session()
        self.media_indexes: dict[str, int] = {}
        self.first_image_url = ""
        self.missing_media: list[str] = []
        self.used_media_paths: set[Path] = set()

    def render_page(self, page: NotionPage) -> tuple[str, str]:
        self.media_indexes = {}
        self.first_image_url = ""
        blocks = self.notion.children(page.page_id)
        content = self.render_blocks(blocks, page)
        footer = (
            "<hr><p><small>"
            f"Kidsnote Report ID: {html.escape(str(page.report_id))}"
            + (f' · <a href="{html.escape(page.url, quote=True)}">Original Notion page</a>' if page.url else "")
            + "</small></p>"
        )
        return content + footer, self.first_image_url

    def render_blocks(self, blocks: list[dict[str, Any]], page: NotionPage) -> str:
        html_parts: list[str] = []
        list_buffer: list[str] = []
        list_type: str | None = None

        def flush_list() -> None:
            nonlocal list_buffer, list_type
            if list_buffer and list_type:
                tag = "ol" if list_type == "numbered_list_item" else "ul"
                html_parts.append(f"<{tag}>" + "".join(list_buffer) + f"</{tag}>")
            list_buffer = []
            list_type = None

        for block in blocks:
            btype = block.get("type")
            if btype in {"bulleted_list_item", "numbered_list_item"}:
                if list_type and list_type != btype:
                    flush_list()
                list_type = btype
                list_buffer.append(self.render_list_item(block, page))
                continue
            flush_list()
            rendered = self.render_block(block, page)
            if rendered:
                html_parts.append(rendered)
        flush_list()
        return "\n".join(html_parts)

    def render_list_item(self, block: dict[str, Any], page: NotionPage) -> str:
        data = block.get(block.get("type") or "", {})
        body = rich_text_to_html(data.get("rich_text") or [])
        if block.get("has_children"):
            body += self.render_blocks(self.notion.children(block["id"]), page)
        return f"<li>{body}</li>"

    def render_block(self, block: dict[str, Any], page: NotionPage) -> str:
        btype = block.get("type")
        data = block.get(btype or "", {})
        if btype == "paragraph":
            text = rich_text_to_html(data.get("rich_text") or [])
            return f"<p>{text}</p>" if text else ""
        if btype in {"heading_1", "heading_2", "heading_3"}:
            tag = {"heading_1": "h2", "heading_2": "h3", "heading_3": "h4"}[btype]
            return f"<{tag}>{rich_text_to_html(data.get('rich_text') or [])}</{tag}>"
        if btype == "quote":
            body = rich_text_to_html(data.get("rich_text") or [])
            if block.get("has_children"):
                body += self.render_blocks(self.notion.children(block["id"]), page)
            return f"<blockquote>{body}</blockquote>"
        if btype == "callout":
            body = rich_text_to_html(data.get("rich_text") or [])
            icon = self.icon_text(data.get("icon"))
            if block.get("has_children"):
                body += self.render_blocks(self.notion.children(block["id"]), page)
            return f'<div class="kidsnote-callout"><p><strong>{html.escape(icon)}</strong> {body}</p></div>'
        if btype == "toggle":
            title = rich_text_to_html(data.get("rich_text") or [])
            children = self.render_blocks(self.notion.children(block["id"]), page) if block.get("has_children") else ""
            return f"<details><summary>{title}</summary>{children}</details>"
        if btype == "divider":
            return "<hr>"
        if btype == "code":
            language = html.escape(data.get("language") or "")
            code = html.escape(plain_text(data.get("rich_text") or []))
            return f'<pre><code class="language-{language}">{code}</code></pre>'
        if btype == "image":
            return self.render_file_block("image", data, page)
        if btype in {"file", "video", "pdf"}:
            return self.render_file_block(btype, data, page)
        if btype == "bookmark":
            url = data.get("url") or ""
            caption = plain_text(data.get("caption") or []) or url
            return f'<p><a href="{html.escape(url, quote=True)}">{html.escape(caption)}</a></p>'
        if block.get("has_children"):
            return self.render_blocks(self.notion.children(block["id"]), page)
        return ""

    def render_file_block(self, kind: str, data: dict[str, Any], page: NotionPage) -> str:
        file_obj = first_mapping(data.get("file")) or first_mapping(data.get("external"))
        url = file_obj.get("url") or ""
        if not url:
            return ""
        caption = plain_text(data.get("caption") or [])
        target_url, content_type = self.media_url(url, kind, page)
        if kind == "image":
            if not self.first_image_url:
                self.first_image_url = target_url
            caption_html = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
            return (
                "<figure>"
                f'<img src="{html.escape(target_url, quote=True)}" alt="{html.escape(caption or page.title, quote=True)}">'
                f"{caption_html}</figure>"
            )
        if kind == "video" or content_type.startswith("video/"):
            caption_html = f"<figcaption>{html.escape(caption)}</figcaption>" if caption else ""
            return (
                "<figure>"
                f'<video controls preload="metadata" src="{html.escape(target_url, quote=True)}"></video>'
                f"{caption_html}</figure>"
            )
        label = caption or Path(urlparse(url).path).name or "attachment"
        return f'<p><a href="{html.escape(target_url, quote=True)}">{html.escape(label)}</a></p>'

    def media_url(self, url: str, kind: str, page: NotionPage) -> tuple[str, str]:
        index = self.media_indexes.get(kind, 0)
        self.media_indexes[kind] = index + 1
        suffix = "" if index == 0 else f"-{index}"
        year, month = date_parts(page.date)
        stem = safe_filename(f"kidsnote-{page.report_id}-{kind}{suffix}", f"kidsnote-{page.report_id}-{kind}{suffix}")
        upload_dir = self.out_dir / "wp-content/uploads" / year / month

        cached = self.find_cached_media(upload_dir, stem)
        if cached:
            self.used_media_paths.add(cached.resolve())
            return "/" + cached.relative_to(self.out_dir).as_posix(), mimetypes.guess_type(cached.name)[0] or ""

        if not self.download_media:
            return url, ""

        try:
            response = self.session.get(url, timeout=180)
            response.raise_for_status()
        except Exception as exc:
            self.missing_media.append(f"{page.report_id}:{url[:120]} ({exc})")
            return url, ""

        content_type = response.headers.get("Content-Type") or "application/octet-stream"
        extension = infer_extension(content_type, url)
        target = upload_dir / f"{stem}{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != len(response.content):
            target.write_bytes(response.content)
        self.used_media_paths.add(target.resolve())
        return "/" + target.relative_to(self.out_dir).as_posix(), content_type

    @staticmethod
    def find_cached_media(upload_dir: Path, stem: str) -> Path | None:
        if not upload_dir.exists():
            return None
        candidates = sorted(upload_dir.glob(f"{stem}.*")) + sorted(upload_dir.glob(f"{stem}-rotated.*"))
        return candidates[0] if candidates else None

    @staticmethod
    def icon_text(icon: dict[str, Any] | None) -> str:
        if not icon:
            return ""
        if icon.get("type") == "emoji":
            return icon.get("emoji") or ""
        return ""


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_static_assets(out_dir: Path) -> None:
    write_text(out_dir / "index.html", INDEX_HTML)
    write_text(out_dir / "assets/styles.css", STYLES_CSS)
    write_text(out_dir / "assets/app.js", APP_JS)
    write_text(out_dir / "_headers", HEADERS)


def clean_generated_data(out_dir: Path) -> None:
    for path in (out_dir / "data/posts",):
        if path.exists():
            shutil.rmtree(path)
    for path in (out_dir / "data/posts.json", out_dir / "export-report.json"):
        if path.exists():
            path.unlink()


def prune_unused_media(out_dir: Path, used_media_paths: set[Path]) -> int:
    uploads_dir = out_dir / "wp-content/uploads"
    if not uploads_dir.exists():
        return 0
    deleted = 0
    for path in uploads_dir.rglob("*"):
        if path.is_file() and path.resolve() not in used_media_paths:
            path.unlink()
            deleted += 1
    for path in sorted(uploads_dir.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    return deleted


def file_stats(out_dir: Path) -> dict[str, Any]:
    files = [path for path in out_dir.rglob("*") if path.is_file()]
    largest = max(files, key=lambda path: path.stat().st_size, default=None)
    return {
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "largest_file": str(largest.relative_to(out_dir)) if largest else "",
        "largest_file_bytes": largest.stat().st_size if largest else 0,
        "pages_file_limit_ok": len(files) <= PAGES_FILE_LIMIT,
        "pages_file_size_limit_ok": largest is None or largest.stat().st_size <= PAGES_FILE_SIZE_LIMIT,
    }


def build_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Export Notion Kidsnote pages to a static Cloudflare Pages site.")
    parser.add_argument("--env-file", type=Path, default=script_dir / ".env")
    parser.add_argument("--out-dir", type=Path, default=script_dir / "dist")
    parser.add_argument("--clean", action="store_true", help="Delete dist before exporting.")
    parser.add_argument("--limit", type=int, default=None, help="Export only the first N Notion pages.")
    parser.add_argument("--skip-media", action="store_true", help="Do not download media files.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_file_values = load_env_file(args.env_file)
    out_dir = args.out_dir
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clean_generated_data(out_dir)
    write_static_assets(out_dir)

    notion = NotionClient(
        token=require_value(env_file_values, "NOTION_TOKEN"),
        database_id=require_value(env_file_values, "NOTION_DATABASE_ID"),
    )
    pages = notion.query_pages(args.limit)
    renderer = StaticRenderer(notion, out_dir, download_media=not args.skip_media)

    posts: list[dict[str, Any]] = []
    for index, page in enumerate(pages, start=1):
        print(f"[{index}/{len(pages)}] export {page.report_id}: {page.title}", flush=True)
        content, thumbnail_url = renderer.render_page(page)
        ptype = post_type(page.title)
        summary = {
            "id": page.report_id,
            "title": page.title,
            "date": display_date(page.date),
            "type": ptype,
            "type_label": post_type_label(ptype),
            "summary": html_excerpt(content),
            "thumbnail_url": thumbnail_url,
            "slug": f"kidsnote-{page.report_id}",
        }
        posts.append(summary)

        detail = dict(summary)
        detail["content"] = content
        write_text(out_dir / f"data/posts/{page.report_id}.json", json.dumps(detail, ensure_ascii=False, separators=(",", ":")))

    counts = {"daily": 0, "album": 0, "announcement": 0}
    for post in posts:
        counts[post["type"]] = counts.get(post["type"], 0) + 1

    manifest = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": "notion",
        "count": len(posts),
        "counts": counts,
        "posts": posts,
    }
    write_text(out_dir / "data/posts.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))

    pruned_media_count = prune_unused_media(out_dir, renderer.used_media_paths)
    stats = file_stats(out_dir)
    report = {
        "out_dir": str(out_dir),
        "missing_media_count": len(renderer.missing_media),
        "missing_media": renderer.missing_media[:50],
        "pruned_media_count": pruned_media_count,
        **stats,
    }
    write_text(out_dir / "export-report.json", json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not stats["pages_file_limit_ok"] or not stats["pages_file_size_limit_ok"]:
        return 2
    return 0


INDEX_HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Seoi Kidsnote</title>
  <link rel="stylesheet" href="/assets/styles.css">
</head>
<body>
  <div class="app-shell">
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand">
          <div class="brand-mark">S</div>
          <h1 class="brand-title">Seoi Kidsnote</h1>
        </div>
      </div>
    </header>

    <main class="workspace">
      <nav class="tabs" id="tabs" aria-label="Kidsnote categories"></nav>
      <section class="dashboard-grid">
        <aside class="list-panel">
          <div class="panel-head">
            <h2 class="panel-title" id="listTitle">알림장</h2>
            <span class="panel-meta" id="listCount">0</span>
          </div>
          <div class="entry-list" id="entryList"></div>
        </aside>
        <article class="detail-panel">
          <div class="detail-body" id="detail">
            <div class="empty">선택된 항목이 없습니다.</div>
          </div>
        </article>
      </section>
    </main>
  </div>
  <script src="/assets/app.js"></script>
</body>
</html>
"""


STYLES_CSS = """:root{color-scheme:light;--bg:#f6f7f2;--surface:#fff;--ink:#23251f;--muted:#6a6f63;--line:#dfe4d8;--accent:#276c5b;--accent-soft:#e5f1ec;--album:#b56b2d;--notice:#365f91;--shadow:0 16px 36px rgba(30,36,28,.08)}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:16px;line-height:1.55}button,a{font:inherit}.app-shell{min-height:100vh}.topbar{border-bottom:1px solid var(--line);background:rgba(255,255,255,.9);backdrop-filter:blur(12px);position:sticky;top:0;z-index:10}.topbar-inner{width:min(1180px,calc(100% - 32px));margin:0 auto;min-height:68px;display:flex;align-items:center;gap:16px}.brand{display:flex;align-items:center;gap:12px;min-width:0}.brand-mark{width:36px;height:36px;border-radius:8px;background:linear-gradient(135deg,var(--accent),#87a85b);display:grid;place-items:center;color:#fff;font-weight:800}.brand-title{margin:0;font-size:20px;font-weight:750}.workspace{width:min(1180px,calc(100% - 32px));margin:24px auto 40px}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.tab{border:1px solid var(--line);background:var(--surface);color:var(--muted);border-radius:8px;padding:9px 13px;cursor:pointer;min-height:42px}.tab[aria-selected=true]{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}.tab-count{margin-left:6px;opacity:.8}.dashboard-grid{display:grid;grid-template-columns:minmax(300px,380px) minmax(0,1fr);gap:18px;align-items:start}.list-panel,.detail-panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;box-shadow:var(--shadow);overflow:hidden}.panel-head{min-height:54px;padding:14px 16px;border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;gap:12px}.panel-title{margin:0;font-size:16px;font-weight:750}.panel-meta{color:var(--muted);font-size:14px;white-space:nowrap}.entry-list{display:grid}.entry{width:100%;border:0;border-bottom:1px solid var(--line);background:transparent;color:inherit;display:grid;grid-template-columns:58px minmax(0,1fr);gap:12px;padding:13px 14px;text-align:left;cursor:pointer}.entry:hover,.entry[aria-current=true]{background:var(--accent-soft)}.entry-thumb{width:58px;height:58px;border-radius:8px;border:1px solid var(--line);background:#eef0ea center/cover no-repeat;display:grid;place-items:center;color:var(--muted);font-size:13px;overflow:hidden}.entry-kicker{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;margin-bottom:3px}.badge{display:inline-flex;align-items:center;min-height:22px;border-radius:999px;padding:2px 8px;color:#fff;background:var(--notice);font-size:12px;font-weight:700}.badge.album{background:var(--album)}.badge.announcement{background:var(--accent)}.entry-title{font-size:15px;font-weight:720;line-height:1.4;overflow-wrap:anywhere}.entry-summary{margin-top:5px;color:var(--muted);font-size:14px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.detail-body{padding:20px}.detail-title{margin:0 0 8px;font-size:22px;line-height:1.35;overflow-wrap:anywhere}.detail-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:18px;color:var(--muted);font-size:14px}.detail-content{border-top:1px solid var(--line);padding-top:18px}.detail-content img,.detail-content video{display:block;max-width:100%;height:auto;border-radius:8px}.detail-content figure{margin:18px 0}.detail-content figcaption{color:var(--muted);font-size:14px;margin-top:8px}.detail-content blockquote,.detail-content .kidsnote-callout{border-left:4px solid var(--accent);margin:16px 0;padding:8px 14px;background:#f3f7f0}.empty{padding:24px 18px;color:var(--muted)}.error{padding:18px;color:#8a2d2d}@media(max-width:820px){.topbar-inner,.workspace{width:min(100% - 24px,1180px)}.dashboard-grid{grid-template-columns:1fr}.detail-title{font-size:20px}}"""


APP_JS = """(()=>{const tabs=[{key:"daily",label:"알림장"},{key:"album",label:"앨범"},{key:"announcement",label:"공지"}],state={activeType:window.localStorage.getItem("kidsnote.activeType")||"daily",allPosts:[],posts:[],counts:{daily:0,album:0,announcement:0},selectedId:null},tabsNode=document.getElementById("tabs"),entryList=document.getElementById("entryList"),detail=document.getElementById("detail"),listTitle=document.getElementById("listTitle"),listCount=document.getElementById("listCount"),escapeHtml=e=>String(e||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;").replaceAll("'","&#039;"),activeLabel=()=>tabs.find(e=>e.key===state.activeType)?.label||"알림장";function renderTabs(){tabsNode.innerHTML=tabs.map(e=>`<button class="tab" type="button" data-type="${e.key}" aria-selected="${e.key===state.activeType?"true":"false"}">${e.label}<span class="tab-count">${state.counts[e.key]||0}</span></button>`).join("")}function selectPosts(){state.posts=state.allPosts.filter(e=>e.type===state.activeType),state.selectedId=state.posts[0]?.id||null}function renderList(){if(listTitle.textContent=activeLabel(),listCount.textContent=`${state.posts.length}개`,!state.posts.length){entryList.innerHTML='<div class="empty">비어 있음</div>',detail.innerHTML='<div class="empty">선택된 항목이 없습니다.</div>';return}entryList.innerHTML=state.posts.map(e=>{const t=e.id===state.selectedId?"true":"false",n=e.thumbnail_url?` style="background-image: url('${escapeHtml(e.thumbnail_url)}')"`:"",a=e.thumbnail_url?"":escapeHtml(e.type_label);return`<button class="entry" type="button" data-id="${e.id}" aria-current="${t}"><span class="entry-thumb"${n}>${a}</span><span><span class="entry-kicker"><span class="badge ${e.type}">${escapeHtml(e.type_label)}</span><span>${escapeHtml(e.date)}</span></span><span class="entry-title">${escapeHtml(e.title)}</span><span class="entry-summary">${escapeHtml(e.summary)}</span></span></button>`}).join("")}async function loadDetail(e){state.selectedId=Number(e),renderList(),detail.innerHTML='<div class="empty">불러오는 중</div>';try{const t=await fetch(`/data/posts/${state.selectedId}.json`);if(!t.ok)throw new Error("detail failed");const n=await t.json();detail.innerHTML=`<h2 class="detail-title">${escapeHtml(n.title)}</h2><div class="detail-meta"><span class="badge ${n.type}">${escapeHtml(n.type_label)}</span><span>${escapeHtml(n.date)}</span></div><div class="detail-content">${n.content||""}</div>`}catch(t){detail.innerHTML='<div class="error">상세 내용을 불러오지 못했습니다.</div>'}}async function loadApp(){renderTabs(),entryList.innerHTML='<div class="empty">불러오는 중</div>';try{const e=await fetch("/data/posts.json");if(!e.ok)throw new Error("list failed");const t=await e.json();state.allPosts=t.posts||[],state.counts=t.counts||state.counts,selectPosts(),renderTabs(),renderList(),state.selectedId&&await loadDetail(state.selectedId)}catch(e){entryList.innerHTML='<div class="error">목록을 불러오지 못했습니다.</div>',detail.innerHTML='<div class="error">상세 내용을 불러오지 못했습니다.</div>'}}tabsNode.addEventListener("click",e=>{const t=e.target.closest("button[data-type]");t&&(state.activeType=t.dataset.type,window.localStorage.setItem("kidsnote.activeType",state.activeType),selectPosts(),renderTabs(),renderList(),state.selectedId&&loadDetail(state.selectedId))}),entryList.addEventListener("click",e=>{const t=e.target.closest("button[data-id]");t&&loadDetail(t.dataset.id)}),loadApp()})();"""


HEADERS = """/data/*
  Cache-Control: public, max-age=300

/wp-content/uploads/*
  Cache-Control: public, max-age=31536000, immutable

/assets/*
  Cache-Control: public, max-age=31536000, immutable
"""


if __name__ == "__main__":
    raise SystemExit(main())

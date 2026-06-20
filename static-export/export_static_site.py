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
<body class="locked">
  <section class="passcode-screen" id="passcodeScreen">
    <form class="passcode-card" id="passcodeForm" autocomplete="off">
      <div class="passcode-mark">S</div>
      <h1>Seoi Kidsnote</h1>
      <label class="passcode-label" for="passcodeInput">가족 패스코드</label>
      <input id="passcodeInput" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="6" placeholder="6자리">
      <button class="passcode-submit" type="submit">열기</button>
      <p class="passcode-message" id="passcodeMessage" aria-live="polite"></p>
    </form>
  </section>

  <div class="app-shell" id="appShell" aria-hidden="true">
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand">
          <div class="brand-mark">S</div>
          <h1 class="brand-title">Seoi Kidsnote</h1>
        </div>
        <div class="topbar-actions">
          <div class="sync-meta" id="syncMeta">동기화 확인 중</div>
          <button class="lock-button" id="lockButton" type="button">잠금</button>
        </div>
      </div>
    </header>

    <main class="workspace">
      <section class="tools" aria-label="검색과 필터">
        <label class="search-box">
          <span class="search-icon" aria-hidden="true">⌕</span>
          <input id="searchInput" type="search" placeholder="제목, 날짜, 내용 검색">
        </label>
        <select id="monthFilter" aria-label="월별 필터">
          <option value="">전체 기간</option>
        </select>
        <button class="clear-button" id="clearFilters" type="button">초기화</button>
      </section>
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
  <div class="lightbox" id="lightbox" hidden>
    <button class="lightbox-close" type="button" aria-label="닫기">&times;</button>
    <button class="lightbox-nav lightbox-prev" type="button" aria-label="이전 사진">&#8249;</button>
    <figure class="lightbox-frame">
      <img id="lightboxImage" alt="">
      <figcaption id="lightboxCaption"></figcaption>
    </figure>
    <button class="lightbox-nav lightbox-next" type="button" aria-label="다음 사진">&#8250;</button>
  </div>
  <script src="/assets/app.js"></script>
</body>
</html>
"""


STYLES_CSS = """:root {
  color-scheme: light;
  --bg: #f7f8fa;
  --surface: #ffffff;
  --surface-soft: #f2f5f3;
  --ink: #20231f;
  --muted: #68706b;
  --line: #dce3de;
  --accent: #28705d;
  --accent-strong: #165341;
  --accent-soft: #e6f2ed;
  --album: #b45f2a;
  --notice: #37669c;
  --danger: #9b3434;
  --shadow: 0 16px 40px rgba(25, 32, 28, .08);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.55;
}

button,
input,
select,
a {
  font: inherit;
}

button,
select {
  cursor: pointer;
}

.app-shell {
  min-height: 100vh;
}

.locked .app-shell {
  display: none;
}

.passcode-screen {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
}

body:not(.locked) .passcode-screen {
  display: none;
}

.passcode-card {
  width: min(100%, 360px);
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
  padding: 28px;
  display: grid;
  gap: 12px;
}

.passcode-mark {
  width: 42px;
  height: 42px;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  display: grid;
  place-items: center;
  font-weight: 800;
}

.passcode-card h1 {
  margin: 0 0 8px;
  font-size: 22px;
}

.passcode-label {
  color: var(--muted);
  font-size: 14px;
}

.passcode-card input {
  width: 100%;
  min-height: 46px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0 12px;
  letter-spacing: .18em;
}

.passcode-card input:focus {
  border-color: var(--accent);
  outline: 3px solid var(--accent-soft);
}

.passcode-submit {
  min-height: 46px;
  border: 1px solid var(--accent);
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  font-weight: 720;
}

.passcode-message {
  min-height: 22px;
  margin: 0;
  color: var(--danger);
  font-size: 14px;
}

.topbar {
  border-bottom: 1px solid var(--line);
  background: rgba(255, 255, 255, .92);
  backdrop-filter: blur(12px);
  position: sticky;
  top: 0;
  z-index: 10;
}

.topbar-inner {
  width: min(1220px, calc(100% - 32px));
  margin: 0 auto;
  min-height: 68px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}

.brand-mark {
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--accent);
  display: grid;
  place-items: center;
  color: #fff;
  font-weight: 800;
}

.brand-title {
  margin: 0;
  font-size: 20px;
  font-weight: 760;
  letter-spacing: 0;
}

.sync-meta {
  color: var(--muted);
  font-size: 13px;
  white-space: nowrap;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.lock-button {
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  padding: 0 10px;
  font-size: 13px;
}

.lock-button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.workspace {
  width: min(1220px, calc(100% - 32px));
  margin: 22px auto 40px;
}

.tools {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(150px, 190px) auto;
  gap: 10px;
  margin-bottom: 14px;
}

.search-box {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  align-items: center;
  min-height: 44px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  overflow: hidden;
}

.search-icon {
  display: grid;
  place-items: center;
  color: var(--muted);
  font-size: 20px;
}

.search-box input,
.tools select,
.clear-button {
  min-height: 44px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink);
  border-radius: 8px;
}

.search-box input {
  width: 100%;
  border: 0;
  outline: 0;
  padding: 0 12px 0 0;
}

.tools select {
  padding: 0 12px;
}

.clear-button {
  padding: 0 14px;
  color: var(--muted);
}

.clear-button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.tabs {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.tab {
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--muted);
  border-radius: 8px;
  padding: 9px 13px;
  min-height: 42px;
}

.tab[aria-selected="true"] {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
  font-weight: 720;
}

.tab-count {
  margin-left: 6px;
  opacity: .82;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(320px, 400px) minmax(0, 1fr);
  gap: 18px;
  align-items: start;
}

.list-panel,
.detail-panel {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
  overflow: hidden;
}

.panel-head {
  min-height: 54px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.panel-title {
  margin: 0;
  font-size: 16px;
  font-weight: 760;
}

.panel-meta {
  color: var(--muted);
  font-size: 14px;
  white-space: nowrap;
}

.entry-list {
  display: grid;
  max-height: calc(100vh - 196px);
  overflow: auto;
}

.entry {
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--line);
  background: transparent;
  color: inherit;
  display: grid;
  grid-template-columns: 60px minmax(0, 1fr);
  gap: 12px;
  padding: 13px 14px;
  text-align: left;
}

.entry:hover,
.entry[aria-current="true"] {
  background: var(--accent-soft);
}

.entry-thumb {
  width: 60px;
  height: 60px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--surface-soft) center / cover no-repeat;
  display: grid;
  place-items: center;
  color: var(--muted);
  font-size: 13px;
  overflow: hidden;
}

.entry-kicker {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 13px;
  margin-bottom: 3px;
}

.badge {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  border-radius: 999px;
  padding: 2px 8px;
  color: #fff;
  background: var(--notice);
  font-size: 12px;
  font-weight: 720;
}

.badge.album {
  background: var(--album);
}

.badge.announcement {
  background: var(--accent);
}

.entry-title {
  display: block;
  font-size: 15px;
  font-weight: 720;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.entry-summary {
  display: -webkit-box;
  margin-top: 5px;
  color: var(--muted);
  font-size: 14px;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.detail-body {
  padding: 20px;
}

.detail-title {
  margin: 0 0 8px;
  font-size: 22px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.detail-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 18px;
  color: var(--muted);
  font-size: 14px;
}

.detail-content {
  border-top: 1px solid var(--line);
  padding-top: 18px;
}

.detail-content img,
.detail-content video {
  display: block;
  max-width: 100%;
  height: auto;
  border-radius: 8px;
}

.detail-content figure {
  margin: 18px 0;
}

.detail-content figcaption {
  color: var(--muted);
  font-size: 14px;
  margin-top: 8px;
}

.detail-content blockquote,
.detail-content .kidsnote-callout {
  border-left: 4px solid var(--accent);
  margin: 16px 0;
  padding: 8px 14px;
  background: var(--surface-soft);
}

.album-gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 8px;
  margin: 0 0 18px;
}

.gallery-item {
  position: relative;
  border: 0;
  border-radius: 8px;
  aspect-ratio: 1;
  background: var(--surface-soft) center / cover no-repeat;
  overflow: hidden;
}

.gallery-item:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: 2px;
}

.gallery-count {
  color: var(--muted);
  font-size: 14px;
  margin: 0 0 10px;
}

.is-gallery-source {
  display: none;
}

.empty,
.error {
  padding: 24px 18px;
}

.empty {
  color: var(--muted);
}

.error {
  color: var(--danger);
}

.lightbox[hidden] {
  display: none;
}

.lightbox {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(10, 12, 11, .88);
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) 64px;
  grid-template-rows: 54px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  padding: 12px;
}

.lightbox-frame {
  grid-column: 2;
  grid-row: 2;
  margin: 0;
  min-width: 0;
  min-height: 0;
  display: grid;
  justify-items: center;
  gap: 10px;
}

.lightbox-frame img {
  max-width: 100%;
  max-height: calc(100vh - 110px);
  border-radius: 8px;
  object-fit: contain;
}

.lightbox-frame figcaption {
  color: #fff;
  font-size: 14px;
}

.lightbox button {
  border: 0;
  color: #fff;
  background: rgba(255, 255, 255, .12);
  border-radius: 8px;
}

.lightbox-close {
  grid-column: 3;
  grid-row: 1;
  justify-self: end;
  width: 42px;
  height: 42px;
  font-size: 24px;
}

.lightbox-nav {
  grid-row: 2;
  width: 48px;
  height: 64px;
  font-size: 38px;
}

.lightbox-prev {
  grid-column: 1;
}

.lightbox-next {
  grid-column: 3;
}

@media (max-width: 860px) {
  .topbar-inner,
  .workspace {
    width: min(100% - 24px, 1220px);
  }

  .topbar-inner {
    align-items: flex-start;
    flex-direction: column;
    justify-content: center;
    padding: 10px 0;
  }

  .topbar-actions {
    width: 100%;
    justify-content: space-between;
  }

  .tools,
  .dashboard-grid {
    grid-template-columns: 1fr;
  }

  .entry-list {
    max-height: none;
  }

  .detail-title {
    font-size: 20px;
  }

  .lightbox {
    grid-template-columns: 44px minmax(0, 1fr) 44px;
    padding: 8px;
  }
}
"""


APP_JS = """(() => {
  const PASSCODE_HASH = "ab1b686a59dab68ec51204e6ab55baa0e874902dc3e8ebe161832936d6f28ef2";
  const UNLOCK_KEY = "seoiKidsnoteUnlocked";

  const tabs = [
    { key: "daily", label: "알림장" },
    { key: "album", label: "앨범" },
    { key: "announcement", label: "공지" },
  ];

  const state = {
    activeType: window.localStorage.getItem("kidsnote.activeType") || "daily",
    activeMonth: window.localStorage.getItem("kidsnote.activeMonth") || "",
    query: "",
    allPosts: [],
    posts: [],
    counts: { daily: 0, album: 0, announcement: 0 },
    selectedId: null,
    lightboxItems: [],
    lightboxIndex: 0,
  };

  const tabsNode = document.getElementById("tabs");
  const entryList = document.getElementById("entryList");
  const detail = document.getElementById("detail");
  const listTitle = document.getElementById("listTitle");
  const listCount = document.getElementById("listCount");
  const syncMeta = document.getElementById("syncMeta");
  const searchInput = document.getElementById("searchInput");
  const monthFilter = document.getElementById("monthFilter");
  const clearFilters = document.getElementById("clearFilters");
  const lightbox = document.getElementById("lightbox");
  const lightboxImage = document.getElementById("lightboxImage");
  const lightboxCaption = document.getElementById("lightboxCaption");
  const appShell = document.getElementById("appShell");
  const passcodeForm = document.getElementById("passcodeForm");
  const passcodeInput = document.getElementById("passcodeInput");
  const passcodeMessage = document.getElementById("passcodeMessage");
  const lockButton = document.getElementById("lockButton");

  const escapeHtml = (value) => String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const activeLabel = () => tabs.find((tab) => tab.key === state.activeType)?.label || "알림장";

  function normalize(value) {
    return String(value || "").toLocaleLowerCase("ko-KR");
  }

  function displayTitle(value) {
    return String(value || "").replace(/^_\\d+\\s*/, "").trim();
  }

  async function sha256Hex(value) {
    const bytes = new TextEncoder().encode(value);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  function unlockApp() {
    document.body.classList.remove("locked");
    appShell.removeAttribute("aria-hidden");
  }

  async function unlockAndLoad() {
    unlockApp();
    await loadApp();
  }

  function lockApp() {
    window.localStorage.removeItem(UNLOCK_KEY);
    window.location.reload();
  }

  async function handlePasscodeSubmit(event) {
    event.preventDefault();
    const value = passcodeInput.value.trim();
    passcodeMessage.textContent = "";

    if (value.length !== 6) {
      passcodeMessage.textContent = "6자리 숫자를 입력해주세요.";
      passcodeInput.focus();
      return;
    }

    try {
      const hash = await sha256Hex(value);
      if (hash === PASSCODE_HASH) {
        window.localStorage.setItem(UNLOCK_KEY, "1");
        await unlockAndLoad();
        return;
      }
      passcodeMessage.textContent = "패스코드가 맞지 않습니다.";
      passcodeInput.select();
    } catch {
      passcodeMessage.textContent = "이 브라우저에서 확인할 수 없습니다.";
    }
  }

  function monthKey(post) {
    return String(post.date || "").slice(0, 7);
  }

  function renderSyncMeta(exportedAt) {
    if (!exportedAt) {
      syncMeta.textContent = "동기화 시간 없음";
      return;
    }
    try {
      const formatted = new Intl.DateTimeFormat("ko-KR", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(exportedAt));
      syncMeta.textContent = `마지막 동기화 ${formatted}`;
    } catch {
      syncMeta.textContent = `마지막 동기화 ${exportedAt}`;
    }
  }

  function renderMonthOptions() {
    const months = [...new Set(state.allPosts.map(monthKey).filter(Boolean))].sort().reverse();
    monthFilter.innerHTML = [
      '<option value="">전체 기간</option>',
      ...months.map((month) => `<option value="${month}">${month}</option>`),
    ].join("");
    monthFilter.value = months.includes(state.activeMonth) ? state.activeMonth : "";
    state.activeMonth = monthFilter.value;
  }

  function renderTabs() {
    tabsNode.innerHTML = tabs.map((tab) => (
      `<button class="tab" type="button" data-type="${tab.key}" aria-selected="${tab.key === state.activeType ? "true" : "false"}">`
      + `${tab.label}<span class="tab-count">${state.counts[tab.key] || 0}</span></button>`
    )).join("");
  }

  function applyFilters(keepSelection = false) {
    const query = normalize(state.query).trim();
    state.posts = state.allPosts.filter((post) => {
      if (post.type !== state.activeType) return false;
      if (state.activeMonth && monthKey(post) !== state.activeMonth) return false;
      if (!query) return true;
      return normalize([displayTitle(post.title), post.date, post.summary, post.type_label].join(" ")).includes(query);
    });

    if (keepSelection && state.posts.some((post) => post.id === state.selectedId)) {
      return;
    }
    state.selectedId = state.posts[0]?.id || null;
  }

  function renderList() {
    listTitle.textContent = activeLabel();
    const filters = [state.activeMonth, state.query.trim()].filter(Boolean).length;
    listCount.textContent = filters ? `${state.posts.length}개 필터됨` : `${state.posts.length}개`;

    if (!state.posts.length) {
      entryList.innerHTML = '<div class="empty">조건에 맞는 항목이 없습니다.</div>';
      detail.innerHTML = '<div class="empty">선택된 항목이 없습니다.</div>';
      return;
    }

    entryList.innerHTML = state.posts.map((post) => {
      const selected = post.id === state.selectedId ? "true" : "false";
      const thumb = post.thumbnail_url ? ` style="background-image: url('${escapeHtml(post.thumbnail_url)}')"` : "";
      const fallback = post.thumbnail_url ? "" : escapeHtml(post.type_label);
      const title = displayTitle(post.title);
      return (
        `<button class="entry" type="button" data-id="${post.id}" aria-current="${selected}">`
        + `<span class="entry-thumb"${thumb}>${fallback}</span>`
        + "<span>"
        + `<span class="entry-kicker"><span class="badge ${post.type}">${escapeHtml(post.type_label)}</span><span>${escapeHtml(post.date)}</span></span>`
        + `<span class="entry-title">${escapeHtml(title)}</span>`
        + `<span class="entry-summary">${escapeHtml(post.summary)}</span>`
        + "</span></button>"
      );
    }).join("");
  }

  function enhanceAlbumGallery(post) {
    const content = detail.querySelector(".detail-content");
    if (!content || post.type !== "album") return;

    const figures = Array.from(content.querySelectorAll("figure"));
    const title = displayTitle(post.title);
    const items = figures.map((figure) => {
      const image = figure.querySelector("img");
      if (!image) return null;
      figure.classList.add("is-gallery-source");
      return {
        src: image.currentSrc || image.src,
        alt: image.alt || title,
        caption: figure.querySelector("figcaption")?.textContent || title,
      };
    }).filter(Boolean);

    if (!items.length) return;
    state.lightboxItems = items;
    const gallery = document.createElement("div");
    gallery.className = "album-gallery";
    gallery.innerHTML = items.map((item, index) => (
      `<button class="gallery-item" type="button" data-gallery-index="${index}" aria-label="사진 ${index + 1} 크게 보기" style="background-image: url('${escapeHtml(item.src)}')"></button>`
    )).join("");
    const count = document.createElement("p");
    count.className = "gallery-count";
    count.textContent = `사진 ${items.length}장`;
    content.prepend(gallery);
    content.prepend(count);
  }

  async function loadDetail(id) {
    state.selectedId = Number(id);
    renderList();
    detail.innerHTML = '<div class="empty">불러오는 중</div>';

    try {
      const response = await fetch(`/data/posts/${state.selectedId}.json`);
      if (!response.ok) throw new Error("detail failed");
      const post = await response.json();
      const title = displayTitle(post.title);
      detail.innerHTML = (
        `<h2 class="detail-title">${escapeHtml(title)}</h2>`
        + `<div class="detail-meta"><span class="badge ${post.type}">${escapeHtml(post.type_label)}</span><span>${escapeHtml(post.date)}</span></div>`
        + `<div class="detail-content">${post.content || ""}</div>`
      );
      enhanceAlbumGallery(post);
    } catch {
      detail.innerHTML = '<div class="error">상세 내용을 불러오지 못했습니다.</div>';
    }
  }

  function refreshView(keepSelection = false) {
    applyFilters(keepSelection);
    renderTabs();
    renderList();
    if (state.selectedId) {
      loadDetail(state.selectedId);
    }
  }

  function showLightbox(index) {
    const item = state.lightboxItems[index];
    if (!item) return;
    state.lightboxIndex = index;
    lightboxImage.src = item.src;
    lightboxImage.alt = item.alt;
    lightboxCaption.textContent = `${index + 1} / ${state.lightboxItems.length} · ${item.caption}`;
    lightbox.hidden = false;
  }

  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImage.removeAttribute("src");
  }

  function moveLightbox(delta) {
    if (!state.lightboxItems.length || lightbox.hidden) return;
    const next = (state.lightboxIndex + delta + state.lightboxItems.length) % state.lightboxItems.length;
    showLightbox(next);
  }

  async function loadApp() {
    renderTabs();
    entryList.innerHTML = '<div class="empty">불러오는 중</div>';

    try {
      const response = await fetch("/data/posts.json");
      if (!response.ok) throw new Error("list failed");
      const manifest = await response.json();
      state.allPosts = manifest.posts || [];
      state.counts = manifest.counts || state.counts;
      renderSyncMeta(manifest.exported_at);
      renderMonthOptions();
      refreshView();
    } catch {
      entryList.innerHTML = '<div class="error">목록을 불러오지 못했습니다.</div>';
      detail.innerHTML = '<div class="error">상세 내용을 불러오지 못했습니다.</div>';
    }
  }

  tabsNode.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-type]");
    if (!target) return;
    state.activeType = target.dataset.type;
    window.localStorage.setItem("kidsnote.activeType", state.activeType);
    refreshView();
  });

  entryList.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-id]");
    if (target) loadDetail(target.dataset.id);
  });

  searchInput.addEventListener("input", (event) => {
    state.query = event.target.value;
    refreshView(true);
  });

  monthFilter.addEventListener("change", (event) => {
    state.activeMonth = event.target.value;
    window.localStorage.setItem("kidsnote.activeMonth", state.activeMonth);
    refreshView();
  });

  clearFilters.addEventListener("click", () => {
    state.query = "";
    state.activeMonth = "";
    searchInput.value = "";
    monthFilter.value = "";
    window.localStorage.removeItem("kidsnote.activeMonth");
    refreshView();
  });

  detail.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-gallery-index]");
    if (target) showLightbox(Number(target.dataset.galleryIndex));
  });

  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox || event.target.closest(".lightbox-close")) closeLightbox();
    if (event.target.closest(".lightbox-prev")) moveLightbox(-1);
    if (event.target.closest(".lightbox-next")) moveLightbox(1);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLightbox();
    if (event.key === "ArrowLeft") moveLightbox(-1);
    if (event.key === "ArrowRight") moveLightbox(1);
  });

  passcodeForm.addEventListener("submit", handlePasscodeSubmit);
  lockButton.addEventListener("click", lockApp);

  if (window.localStorage.getItem(UNLOCK_KEY) === "1") {
    unlockAndLoad();
  } else {
    passcodeInput.focus();
  }
})();
"""


HEADERS = """/data/*
  Cache-Control: public, max-age=300

/wp-content/uploads/*
  Cache-Control: public, max-age=31536000, immutable

/assets/*
  Cache-Control: public, max-age=31536000, immutable
"""


if __name__ == "__main__":
    raise SystemExit(main())

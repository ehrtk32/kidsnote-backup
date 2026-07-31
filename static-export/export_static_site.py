#!/usr/bin/env python3
"""Export Kidsnote Notion data directly to a static Cloudflare Pages site."""
from __future__ import annotations

import argparse
import hashlib
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


def is_comment_heading(text: str) -> bool:
    cleaned = re.sub(r"\s+", " ", text).strip()
    return cleaned.startswith("💬 댓글") or cleaned.startswith("댓글 (")


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
        return content, self.first_image_url

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
            data = block.get(btype or "", {})
            if btype in {"heading_1", "heading_2", "heading_3"} and is_comment_heading(plain_text(data.get("rich_text") or [])):
                flush_list()
                break
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


def hashed_asset_name(prefix: str, extension: str, content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}.{digest}.{extension}"


def write_static_assets(out_dir: Path) -> str:
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("styles*.css", "app*.js", "*.svg", "*.jpg", "*.jpeg", "*.png", "*.webp"):
        for path in assets_dir.glob(pattern):
            path.unlink()

    styles_name = hashed_asset_name("styles", "css", STYLES_CSS)
    app_name = hashed_asset_name("app", "js", APP_JS)
    index_html = (
        INDEX_HTML
        .replace("__STYLES_HREF__", f"/assets/{styles_name}")
        .replace("__APP_SRC__", f"/assets/{app_name}")
    )
    write_text(out_dir / "index.html", index_html)
    write_text(assets_dir / styles_name, STYLES_CSS)
    write_text(assets_dir / app_name, APP_JS)
    for source in (Path(__file__).resolve().parent / "assets").iterdir():
        if source.is_file():
            shutil.copy2(source, assets_dir / source.name)
    write_text(out_dir / "_headers", HEADERS)
    return index_html


def write_post_routes(out_dir: Path, posts: list[dict[str, Any]], index_html: str) -> None:
    for post in posts:
        write_text(out_dir / f"posts/{post['id']}/index.html", index_html)


def clean_generated_data(out_dir: Path) -> None:
    for path in (out_dir / "data/posts", out_dir / "posts"):
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
    index_html = write_static_assets(out_dir)

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
    write_post_routes(out_dir, posts, index_html)

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
  <title>서이의 키즈노트</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23548a62'/%3E%3Cpath fill='white' d='M32 52S10 39.5 10 24.5C10 16.8 15.8 12 22.2 12c4.2 0 7.6 2.1 9.8 5.5C34.2 14.1 37.6 12 41.8 12C48.2 12 54 16.8 54 24.5C54 39.5 32 52 32 52z'/%3E%3C/svg%3E">
  <link rel="stylesheet" href="__STYLES_HREF__">
</head>
<body class="locked">
  <section class="passcode-screen" id="passcodeScreen">
    <form class="passcode-card" id="passcodeForm" autocomplete="off">
      <div class="passcode-mark" aria-hidden="true">♥</div>
      <h1>서이의 키즈노트</h1>
      <label class="passcode-label" for="passcodeInput">가족 패스코드</label>
      <input id="passcodeInput" type="password" inputmode="numeric" pattern="[0-9]*" maxlength="6" placeholder="6자리">
      <p class="passcode-hint">아기생년월일 (6자리)</p>
      <button class="passcode-submit" type="submit">열기</button>
      <p class="passcode-message" id="passcodeMessage" aria-live="polite"></p>
    </form>
  </section>

  <div class="app-shell" id="appShell" aria-hidden="true">
    <aside class="sidebar">
      <header class="sidebar-header">
        <div class="sidebar-brand" aria-label="서이의 키즈노트">
          <span class="sidebar-brand-mark" aria-hidden="true">♥</span>
          <span class="sidebar-brand-copy">
            <strong><span>서이</span>의 키즈노트</strong>
            <small>Seoi's Kidsnote</small>
          </span>
        </div>
      </header>
      <section class="profile" aria-label="서이 프로필">
        <button class="profile-photo" id="profilePhoto" type="button" aria-label="서이 사진 크게 보기">
          <img src="/assets/seoi-profile.jpg" alt="서이">
        </button>
        <div class="profile-copy">
          <h1>서이</h1>
          <p><time datetime="2025-03-04">2025.03.04</time><span aria-hidden="true">·</span><strong id="babyDays">D+</strong></p>
        </div>
      </section>
      <nav class="tabs" id="tabs" aria-label="서이의 키즈노트 메뉴"></nav>
      <footer class="sidebar-footer">
        <div class="sync-meta" id="syncMeta">동기화 확인 중</div>
      </footer>
    </aside>

    <main class="workspace">
      <header class="content-header">
        <div>
          <p class="content-eyebrow" id="pageEyebrow">서이의 새로운 기록</p>
          <h2 class="content-title" id="pageTitle">홈</h2>
        </div>
        <button class="filter-toggle-button" id="filterToggle" type="button" aria-expanded="false" aria-controls="filterTools">필터 목록 열기</button>
      </header>
      <section class="tools" id="filterTools" aria-label="검색과 필터" hidden>
        <label class="search-box">
          <span class="search-icon" aria-hidden="true">⌕</span>
          <input id="searchInput" type="search" placeholder="제목, 날짜, 내용 검색">
        </label>
        <select id="monthFilter" aria-label="월별 필터">
          <option value="">전체 기간</option>
        </select>
        <button class="clear-button" id="clearFilters" type="button">초기화</button>
      </section>
      <section class="dashboard-grid">
        <aside class="list-panel">
          <button class="panel-head panel-toggle" id="listToggle" type="button" aria-expanded="true" aria-controls="entryList">
            <span class="panel-title" id="listTitle">알림장</span>
            <span class="panel-head-right">
              <span class="panel-meta" id="listCount">0</span>
              <span class="panel-chevron" id="listChevron" aria-hidden="true">▾</span>
            </span>
          </button>
          <div class="entry-list" id="entryList"></div>
        </aside>
        <article class="detail-panel" id="detailPanel" hidden>
          <div class="detail-body" id="detail">
            <div class="empty">선택된 항목이 없습니다.</div>
          </div>
        </article>
      </section>
    </main>
  </div>
  <div class="lightbox" id="lightbox" hidden>
    <div class="lightbox-top-actions">
      <button class="lightbox-download" type="button" aria-label="사진 다운로드">⤓</button>
      <button class="lightbox-close" type="button" aria-label="닫기">&times;</button>
    </div>
    <div class="lightbox-toolbar" aria-label="사진 확대 컨트롤">
      <button class="lightbox-zoom-out" type="button" aria-label="축소">−</button>
      <button class="lightbox-zoom-reset" id="lightboxZoomValue" type="button" aria-label="확대 초기화">100%</button>
      <button class="lightbox-zoom-in" type="button" aria-label="확대">+</button>
    </div>
    <button class="lightbox-nav lightbox-prev" type="button" aria-label="이전 사진">&#8249;</button>
    <figure class="lightbox-frame">
      <div class="lightbox-image-wrap" id="lightboxImageWrap">
        <img id="lightboxImage" alt="" draggable="false">
      </div>
      <figcaption id="lightboxCaption"></figcaption>
    </figure>
    <button class="lightbox-nav lightbox-next" type="button" aria-label="다음 사진">&#8250;</button>
  </div>
  <script src="__APP_SRC__"></script>
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
  display: grid;
  grid-template-columns: 248px minmax(0, 1fr);
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
  font-size: 19px;
  font-weight: 800;
  line-height: 1;
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

.passcode-hint {
  margin: -4px 0 2px;
  color: var(--muted);
  font-size: 13px;
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

.sidebar {
  position: sticky;
  top: 0;
  height: 100vh;
  align-self: start;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--line);
  background: var(--surface);
  z-index: 20;
}

.sidebar-header {
  min-height: 72px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  display: flex;
  align-items: center;
}

.profile {
  min-height: 226px;
  padding: 28px 24px 24px;
  border-bottom: 1px solid var(--line);
  display: grid;
  place-items: center;
  align-content: center;
  gap: 14px;
  text-align: center;
}

.profile-photo {
  width: 88px;
  height: 88px;
  padding: 0;
  border: 3px solid #fff;
  border-radius: 50%;
  background: var(--surface-soft);
  box-shadow: 0 0 0 1px var(--line), 0 10px 24px rgba(32, 35, 31, .14);
  overflow: hidden;
  cursor: zoom-in;
  transition: box-shadow .18s ease, transform .18s ease;
}

.profile-photo:hover {
  box-shadow: 0 0 0 2px rgba(40, 112, 93, .24), 0 12px 26px rgba(32, 35, 31, .16);
  transform: translateY(-1px);
}

.profile-photo:focus-visible {
  outline: 3px solid rgba(40, 112, 93, .3);
  outline-offset: 3px;
}

.profile-photo img {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
  object-position: 35% 42%;
  transform: scale(2.35);
  transform-origin: 35% 42%;
}

.profile-copy h1 {
  margin: 0;
  font-size: 20px;
  line-height: 1.3;
}

.profile-copy p {
  margin: 5px 0 0;
  color: var(--muted);
  font-size: 13px;
}

.profile-copy p span {
  margin: 0 6px;
  color: var(--line);
}

.profile-copy strong {
  color: var(--accent);
  font-weight: 760;
}

.sidebar-footer {
  margin-top: auto;
  padding: 18px 20px 22px;
  border-top: 1px solid var(--line);
}

.sidebar-brand {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr);
  align-items: center;
  gap: 10px;
  min-width: 0;
  margin: 0;
  color: var(--ink);
}

.sidebar-brand-mark {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  background: var(--accent);
  color: #fff;
  font-size: 15px;
  font-weight: 800;
  line-height: 1;
  box-shadow: 0 6px 14px rgba(40, 112, 93, .2);
}

.sidebar-brand-copy {
  min-width: 0;
  display: grid;
  gap: 1px;
}

.sidebar-brand-copy strong {
  font-size: 16px;
  font-weight: 800;
  line-height: 1.25;
  white-space: nowrap;
}

.sidebar-brand-copy strong span {
  color: var(--accent);
}

.sidebar-brand-copy small {
  color: var(--muted);
  font-size: 11px;
  font-weight: 650;
  line-height: 1.3;
}

.sync-meta {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.filter-toggle-button {
  min-height: 34px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--muted);
  padding: 0 12px;
  font-size: 13px;
  white-space: nowrap;
}

.filter-toggle-button:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.workspace {
  width: min(100%, 1120px);
  margin: 0 auto;
  padding: 34px 40px 56px;
}

.content-header {
  min-height: 64px;
  margin-bottom: 18px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
}

.content-eyebrow {
  margin: 0 0 3px;
  color: var(--accent);
  font-size: 13px;
  font-weight: 720;
}

.content-title {
  margin: 0;
  font-size: 26px;
  line-height: 1.25;
}

.tools {
  display: grid;
  grid-template-columns: minmax(240px, 1fr) minmax(150px, 190px) auto;
  gap: 10px;
  margin-bottom: 14px;
}

.tools[hidden] {
  display: none;
}

.search-box {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr);
  align-items: center;
  min-height: 40px;
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
  min-height: 40px;
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
  display: grid;
  gap: 4px;
  padding: 20px 14px;
}

.tab {
  position: relative;
  width: 100%;
  min-height: 48px;
  border: 0;
  background: transparent;
  color: var(--muted);
  text-decoration: none;
  border-radius: 7px;
  padding: 8px 10px;
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  text-align: left;
  cursor: pointer;
}

.tab:hover {
  background: var(--surface-soft);
  color: var(--ink);
}

.tab:focus-visible {
  outline: 3px solid var(--accent-soft);
}

.tab[aria-selected="true"],
.tab[aria-current="page"] {
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-weight: 720;
}

.tab-icon {
  width: 30px;
  height: 30px;
  border-radius: 7px;
  display: grid;
  place-items: center;
  background: #e8f1fb;
  color: var(--notice);
  font-size: 17px;
  line-height: 1;
}

.tab-icon img {
  width: 18px;
  height: 18px;
  display: block;
}

.tab[data-type="home"] .tab-icon,
.tab[href*="type=home"] .tab-icon {
  background: var(--accent-soft);
  color: var(--accent);
}

.tab[data-type="album"] .tab-icon,
.tab[href*="type=album"] .tab-icon {
  background: #fff0e6;
  color: var(--album);
}

.tab[data-type="announcement"] .tab-icon,
.tab[href*="type=announcement"] .tab-icon {
  background: #fdebea;
  color: #b84b46;
}

.tab-label {
  min-width: 0;
}

.tab-meta {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  gap: 7px;
}

.tab-count {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}

.tab-new-count {
  display: grid;
  place-items: center;
  min-width: 21px;
  height: 21px;
  padding: 0 6px;
  border: 0;
  border-radius: 999px;
  background: #d9342b;
  color: #fff;
  font-size: 12px;
  font-weight: 820;
  line-height: 1;
  box-shadow: none;
}

.dashboard-grid {
  display: grid;
  grid-template-columns: minmax(0, 900px);
  justify-content: start;
  gap: 18px;
  align-items: start;
}

.app-shell.is-detail-route .dashboard-grid {
  grid-template-columns: minmax(0, 960px);
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

.panel-toggle {
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: left;
}

.panel-toggle:hover {
  background: var(--surface-soft);
}

.panel-toggle:focus-visible {
  outline: 3px solid var(--accent-soft);
  outline-offset: -3px;
}

.panel-title {
  margin: 0;
  font-size: 16px;
  font-weight: 760;
}

.panel-head-right {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.panel-meta {
  color: var(--muted);
  font-size: 14px;
  white-space: nowrap;
}

.panel-chevron {
  color: var(--muted);
  font-size: 20px;
  line-height: 1;
}

.entry-list {
  display: grid;
}

.entry-list[hidden] {
  display: none;
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
  text-decoration: none;
  cursor: pointer;
}

.entry:hover {
  background: var(--accent-soft);
}

.entry:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: -3px;
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

.new-pill {
  display: inline-flex;
  align-items: center;
  min-height: 20px;
  border-radius: 999px;
  padding: 1px 7px;
  background: #d9342b;
  color: #fff;
  font-size: 11px;
  font-weight: 820;
  line-height: 1;
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

.detail-back {
  display: inline-flex;
  align-items: center;
  min-height: 38px;
  margin-bottom: 16px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0 12px;
  background: var(--surface);
  color: var(--accent);
  text-decoration: none;
  font-size: 14px;
  font-weight: 720;
}

.detail-back:hover {
  border-color: var(--accent);
  background: var(--surface-soft);
}

.detail-back:focus-visible {
  outline: 3px solid var(--accent-soft);
  border-color: var(--accent);
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

.detail-content figure.is-lightbox-source {
  cursor: zoom-in;
}

.detail-content figure.is-lightbox-source img {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

@media (hover: hover) {
  .detail-content figure.is-lightbox-source:hover img {
    opacity: 0.9;
    transform: scale(0.995);
  }
}

.detail-content figure.is-lightbox-source:focus-visible {
  outline: 3px solid var(--accent);
  outline-offset: 3px;
  border-radius: 10px;
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

.post-nav {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--line);
}

.post-nav-button {
  min-height: 58px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  color: var(--ink);
  padding: 10px 12px;
  text-align: left;
  display: grid;
  gap: 4px;
  text-decoration: none;
}

.post-nav-button:not(.is-disabled):hover {
  border-color: var(--accent);
  background: var(--surface-soft);
}

.post-nav-button:focus-visible {
  outline: 3px solid var(--accent-soft);
  border-color: var(--accent);
}

.post-nav-button.is-disabled {
  color: var(--muted);
  opacity: .55;
}

.post-nav-button.next {
  text-align: right;
  justify-items: end;
}

.post-nav-label {
  color: var(--muted);
  font-size: 13px;
  font-weight: 720;
}

.post-nav-title {
  width: 100%;
  font-size: 14px;
  font-weight: 720;
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
  height: 100%;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  justify-items: center;
  gap: 10px;
}

.lightbox-image-wrap {
  width: 100%;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  display: flex;
  align-items: center;
  justify-content: center;
}

.lightbox-image-wrap.is-zoomed {
  align-items: flex-start;
  justify-content: flex-start;
  cursor: grab;
  touch-action: none;
}

.lightbox-image-wrap img {
  display: block;
  max-width: 100%;
  max-height: 100%;
  border-radius: 8px;
  object-fit: contain;
  user-select: none;
  -webkit-user-drag: none;
}

.lightbox-image-wrap.is-zoomed img {
  max-width: none;
  max-height: none;
  cursor: inherit;
}

.lightbox-image-wrap.is-dragging,
.lightbox-image-wrap.is-dragging img {
  cursor: grabbing;
}

.lightbox-frame figcaption {
  color: #fff;
  font-size: 14px;
  min-height: 22px;
  text-align: center;
}

.lightbox button {
  border: 0;
  color: #fff;
  background: rgba(255, 255, 255, .12);
  border-radius: 8px;
}

.lightbox button:disabled {
  cursor: default;
  opacity: .38;
}

.lightbox-top-actions {
  grid-column: 3;
  grid-row: 1;
  justify-self: end;
  display: flex;
  gap: 6px;
}

.lightbox-top-actions button {
  width: 42px;
  height: 42px;
}

.lightbox-download {
  font-size: 22px;
  font-weight: 760;
}

.lightbox-close {
  font-size: 24px;
}

.lightbox-toolbar {
  grid-column: 2;
  grid-row: 1;
  justify-self: center;
  align-self: center;
  display: flex;
  gap: 6px;
  padding: 4px;
  border-radius: 8px;
  background: rgba(255, 255, 255, .08);
}

.lightbox-toolbar button {
  min-width: 40px;
  height: 36px;
  padding: 0 12px;
  font-size: 18px;
  font-weight: 760;
}

.lightbox-toolbar .lightbox-zoom-reset {
  min-width: 66px;
  font-size: 14px;
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
  .app-shell {
    display: block;
  }

  .sidebar {
    position: static;
    height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
    box-shadow: 0 6px 20px rgba(25, 32, 28, .06);
  }

  .profile {
    min-height: 78px;
    padding: 10px 16px 8px;
    border-bottom: 0;
    display: flex;
    justify-content: flex-start;
    gap: 12px;
    text-align: left;
  }

  .sidebar-header {
    min-height: 64px;
    padding: 12px 16px;
  }

  .profile-photo {
    width: 54px;
    height: 54px;
    border-width: 2px;
    flex: 0 0 auto;
  }

  .profile-copy h1 {
    font-size: 17px;
  }

  .profile-copy p {
    margin-top: 2px;
    font-size: 12px;
  }

  .sidebar-footer {
    display: none;
  }

  .tabs {
    grid-template-columns: repeat(4, minmax(76px, 1fr));
    gap: 4px;
    padding: 0 10px 10px;
    overflow-x: auto;
  }

  .tab {
    min-height: 42px;
    grid-template-columns: 24px minmax(0, 1fr);
    gap: 6px;
    padding: 6px 8px;
  }

  .tab-icon {
    width: 24px;
    height: 24px;
    border-radius: 6px;
    font-size: 14px;
  }

  .tab-count {
    display: none;
  }

  .tab-new-count {
    position: absolute;
    top: -3px;
    right: -2px;
    min-width: 18px;
    height: 18px;
    padding: 0 5px;
    border: 2px solid #fff;
    font-size: 10px;
  }

  .workspace {
    width: 100%;
    padding: 20px 12px 40px;
  }

  .content-header {
    min-height: 52px;
    margin-bottom: 14px;
    align-items: center;
  }

  .content-title {
    font-size: 22px;
  }

  .content-eyebrow {
    font-size: 12px;
  }

  .tools,
  .dashboard-grid {
    grid-template-columns: 1fr;
  }

  .detail-title {
    font-size: 20px;
  }

  .post-nav {
    grid-template-columns: 1fr;
  }

  .post-nav-button.next {
    text-align: left;
    justify-items: start;
  }

  .lightbox {
    grid-template-columns: 44px minmax(0, 1fr) 44px;
    padding: 8px;
  }

  .lightbox-toolbar button {
    min-width: 38px;
    height: 34px;
    padding: 0 10px;
  }
}

@media (max-width: 430px) {
  .tab {
    justify-items: center;
    grid-template-columns: 1fr;
    gap: 2px;
    padding: 6px 4px;
    font-size: 12px;
    text-align: center;
  }

  .tab-icon {
    display: grid;
  }

  .tab-meta {
    position: absolute;
    inset: 0;
    pointer-events: none;
  }

  .content-header {
    align-items: flex-end;
  }

  .filter-toggle-button {
    padding: 0 10px;
  }
}
"""


APP_JS = """(() => {
  const PASSCODE_HASH = "ab1b686a59dab68ec51204e6ab55baa0e874902dc3e8ebe161832936d6f28ef2";
  const UNLOCK_KEY = "seoiKidsnoteUnlocked";

  const tabs = [
    { key: "home", label: "홈", icon: "/assets/home.svg" },
    { key: "daily", label: "알림장", icon: "/assets/daily.svg" },
    { key: "album", label: "앨범", icon: "/assets/album.svg" },
    { key: "announcement", label: "공지", icon: "/assets/announcement.svg" },
  ];
  const LIGHTBOX_MIN_ZOOM = 1;
  const LIGHTBOX_MAX_ZOOM = 4;
  const LIGHTBOX_ZOOM_STEP = 0.5;
  const PROFILE_IMAGE_SRC = "/assets/seoi-profile.jpg";

  function postIdFromPath(pathname) {
    const match = String(pathname || "").match(/^\/posts\/(\d+)\/?$/);
    return match ? Number(match[1]) : null;
  }

  function postPath(id) {
    return `/posts/${Number(id)}/`;
  }

  const state = {
    activeType: window.localStorage.getItem("kidsnote.activeType") || "home",
    activeMonth: window.localStorage.getItem("kidsnote.activeMonth") || "",
    query: "",
    allPosts: [],
    posts: [],
    counts: { daily: 0, album: 0, announcement: 0 },
    newCounts: { daily: 0, album: 0, announcement: 0 },
    recentDateKeys: recentDateKeys(),
    selectedId: postIdFromPath(window.location.pathname),
    detailPosts: [],
    listCollapsed: false,
    filtersOpen: false,
    lightboxItems: [],
    detailLightboxItems: [],
    lightboxIndex: 0,
    lightboxZoom: 1,
    lightboxDrag: null,
  };

  const tabsNode = document.getElementById("tabs");
  const entryList = document.getElementById("entryList");
  const detail = document.getElementById("detail");
  const detailPanel = document.getElementById("detailPanel");
  const listTitle = document.getElementById("listTitle");
  const listCount = document.getElementById("listCount");
  const listPanel = document.querySelector(".list-panel");
  const listToggle = document.getElementById("listToggle");
  const listChevron = document.getElementById("listChevron");
  const syncMeta = document.getElementById("syncMeta");
  const searchInput = document.getElementById("searchInput");
  const monthFilter = document.getElementById("monthFilter");
  const clearFilters = document.getElementById("clearFilters");
  const filterTools = document.getElementById("filterTools");
  const filterToggle = document.getElementById("filterToggle");
  const lightbox = document.getElementById("lightbox");
  const lightboxImageWrap = document.getElementById("lightboxImageWrap");
  const lightboxImage = document.getElementById("lightboxImage");
  const lightboxCaption = document.getElementById("lightboxCaption");
  const lightboxZoomValue = document.getElementById("lightboxZoomValue");
  const lightboxZoomOut = document.querySelector(".lightbox-zoom-out");
  const lightboxZoomIn = document.querySelector(".lightbox-zoom-in");
  const lightboxDownload = document.querySelector(".lightbox-download");
  const lightboxPrev = document.querySelector(".lightbox-prev");
  const lightboxNext = document.querySelector(".lightbox-next");
  const appShell = document.getElementById("appShell");
  const profilePhoto = document.getElementById("profilePhoto");
  const babyDays = document.getElementById("babyDays");
  const pageEyebrow = document.getElementById("pageEyebrow");
  const pageTitle = document.getElementById("pageTitle");
  const passcodeForm = document.getElementById("passcodeForm");
  const passcodeInput = document.getElementById("passcodeInput");
  const passcodeMessage = document.getElementById("passcodeMessage");

  const escapeHtml = (value) => String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const activeLabel = () => tabs.find((tab) => tab.key === state.activeType)?.label || "홈";
  const activeListLabel = () => state.activeType === "home" ? "최신 업데이트" : activeLabel();

  function normalize(value) {
    return String(value || "").toLocaleLowerCase("ko-KR");
  }

  function displayTitle(value) {
    return String(value || "").replace(/^_\\d+\\s*/, "").trim();
  }

  function koreaDateKey(date) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(date).reduce((acc, part) => {
      acc[part.type] = part.value;
      return acc;
    }, {});
    return `${parts.year}.${parts.month}.${parts.day}`;
  }

  function recentDateKeys() {
    const now = new Date();
    const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    return new Set([koreaDateKey(now), koreaDateKey(yesterday)]);
  }

  function renderBabyDays() {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date()).reduce((acc, part) => {
      acc[part.type] = Number(part.value);
      return acc;
    }, {});
    const today = Date.UTC(parts.year, parts.month - 1, parts.day);
    const birthday = Date.UTC(2025, 2, 4);
    const days = Math.floor((today - birthday) / 86400000) + 1;
    babyDays.textContent = `D+${Math.max(1, days)}`;
  }

  function isNewPost(post) {
    return state.recentDateKeys.has(String(post.date || ""));
  }

  function countNewPosts(posts) {
    return posts.reduce((counts, post) => {
      if (isNewPost(post)) counts[post.type] = (counts[post.type] || 0) + 1;
      return counts;
    }, { daily: 0, album: 0, announcement: 0 });
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

  function renderFilterPanelState() {
    const detailRoute = state.selectedId !== null;
    filterTools.hidden = detailRoute || !state.filtersOpen;
    filterToggle.hidden = detailRoute;
    filterToggle.textContent = state.filtersOpen ? "필터 목록 닫기" : "필터 목록 열기";
    filterToggle.setAttribute("aria-expanded", String(state.filtersOpen));
  }

  function renderRouteState() {
    const detailRoute = state.selectedId !== null;
    appShell.classList.toggle("is-detail-route", detailRoute);
    tabsNode.hidden = false;
    listPanel.hidden = detailRoute;
    detailPanel.hidden = !detailRoute;
    renderFilterPanelState();
  }

  function renderPageHeader() {
    if (state.selectedId !== null) {
      pageEyebrow.textContent = "서이의 기록";
      pageTitle.textContent = activeLabel();
      return;
    }
    if (state.activeType === "home") {
      pageEyebrow.textContent = "오늘과 어제의 새 소식";
      pageTitle.textContent = "홈";
      return;
    }
    pageEyebrow.textContent = "서이의 기록 모아보기";
    pageTitle.textContent = activeLabel();
  }

  function toggleFilters() {
    state.filtersOpen = !state.filtersOpen;
    renderFilterPanelState();
    if (state.filtersOpen) searchInput.focus();
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
    const detailRoute = state.selectedId !== null;
    tabsNode.innerHTML = tabs.map((tab) => {
      const count = tab.key === "home"
        ? Object.values(state.newCounts).reduce((total, value) => total + value, 0)
        : state.counts[tab.key] || 0;
      const newCount = tab.key === "home" ? 0 : state.newCounts[tab.key] || 0;
      const content = (
        `<span class="tab-icon" aria-hidden="true"><img src="${tab.icon}" alt=""></span>`
        + `<span class="tab-label">${tab.label}</span>`
        + `<span class="tab-meta"><span class="tab-count">${count}</span>`
        + `${newCount ? `<span class="tab-new-count" aria-label="최근 업데이트 ${newCount}개">${newCount}</span>` : ""}</span>`
      );
      if (detailRoute) {
        const current = tab.key === state.activeType ? ' aria-current="page"' : "";
        return `<a class="tab" href="/?type=${tab.key}"${current}>${content}</a>`;
      }
      return (
        `<button class="tab" type="button" data-type="${tab.key}" aria-selected="${tab.key === state.activeType ? "true" : "false"}">`
        + `${content}</button>`
      );
    }).join("");
  }

  function applyFilters() {
    const query = normalize(state.query).trim();
    state.posts = state.allPosts.filter((post) => {
      if (state.activeType === "home") {
        if (!isNewPost(post)) return false;
      } else if (post.type !== state.activeType) {
        return false;
      }
      if (state.activeMonth && monthKey(post) !== state.activeMonth) return false;
      if (!query) return true;
      return normalize([displayTitle(post.title), post.date, post.summary, post.type_label].join(" ")).includes(query);
    });
  }

  function renderListCollapseState() {
    listPanel.classList.toggle("is-collapsed", state.listCollapsed);
    entryList.hidden = state.listCollapsed;
    listToggle.setAttribute("aria-expanded", String(!state.listCollapsed));
    listToggle.setAttribute("aria-label", `${activeLabel()} 목록 ${state.listCollapsed ? "열기" : "닫기"}`);
    listChevron.textContent = state.listCollapsed ? "▸" : "▾";
  }

  function toggleList() {
    state.listCollapsed = !state.listCollapsed;
    renderListCollapseState();
  }

  function renderList() {
    listTitle.textContent = activeListLabel();
    const filters = [state.activeMonth, state.query.trim()].filter(Boolean).length;
    listCount.textContent = filters ? `${state.posts.length}개 필터됨` : `${state.posts.length}개`;
    renderListCollapseState();

    if (!state.posts.length) {
      entryList.innerHTML = state.activeType === "home"
        ? '<div class="empty">최근 등록된 새로운 기록이 없습니다.</div>'
        : '<div class="empty">조건에 맞는 항목이 없습니다.</div>';
      return;
    }

    entryList.innerHTML = state.posts.map((post) => {
      const thumb = post.thumbnail_url ? ` style="background-image: url('${escapeHtml(post.thumbnail_url)}')"` : "";
      const fallback = post.thumbnail_url ? "" : escapeHtml(post.type_label);
      const title = displayTitle(post.title);
      const newMarker = isNewPost(post) ? '<span class="new-pill">NEW</span>' : "";
      return (
        `<a class="entry" href="${postPath(post.id)}">`
        + `<span class="entry-thumb"${thumb}>${fallback}</span>`
        + "<span>"
        + `<span class="entry-kicker"><span class="badge ${post.type}">${escapeHtml(post.type_label)}</span><span>${escapeHtml(post.date)}</span>${newMarker}</span>`
        + `<span class="entry-title">${escapeHtml(title)}</span>`
        + `<span class="entry-summary">${escapeHtml(post.summary)}</span>`
        + "</span></a>"
      );
    }).join("");
  }

  function enhanceMediaLightbox(post) {
    const content = detail.querySelector(".detail-content");
    if (!content) return;

    const figures = Array.from(content.querySelectorAll("figure"));
    const title = displayTitle(post.title);
    const items = [];

    figures.forEach((figure) => {
      const image = figure.querySelector("img");
      if (!image) return;
      const index = items.length;
      items.push({
        src: image.currentSrc || image.src,
        alt: image.alt || title,
        caption: figure.querySelector("figcaption")?.textContent || title,
      });

      if (post.type === "album") {
        figure.classList.add("is-gallery-source");
      } else {
        figure.classList.add("is-lightbox-source");
        figure.dataset.galleryIndex = String(index);
        figure.tabIndex = 0;
        figure.setAttribute("role", "button");
        figure.setAttribute("aria-label", `사진 ${index + 1} 크게 보기`);
      }
    });

    if (!items.length) return;
    state.detailLightboxItems = items;
    if (post.type !== "album") return;

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

  function isCommentHeadingText(value) {
    const cleaned = (value || "").replace(/\s+/g, " ").trim();
    return cleaned.startsWith("💬 댓글") || cleaned.startsWith("댓글 (");
  }

  function cleanDetailContent(container) {
    const sourceLinks = Array.from(container.querySelectorAll("a")).filter((link) => (
      link.textContent.trim() === "Original Notion page"
    ));
    sourceLinks.forEach((link) => {
      const paragraph = link.closest("p");
      const divider = paragraph?.previousElementSibling;
      paragraph?.remove();
      if (divider?.tagName === "HR") divider.remove();
    });

    const commentHeading = Array.from(container.querySelectorAll("h2, h3, h4")).find((heading) => (
      isCommentHeadingText(heading.textContent)
    ));
    if (!commentHeading) return;

    let node = commentHeading;
    while (node) {
      const next = node.nextElementSibling;
      node.remove();
      node = next;
    }
  }

  function adjacentPost(offset) {
    const index = state.detailPosts.findIndex((post) => post.id === state.selectedId);
    if (index < 0) return null;
    return state.detailPosts[index + offset] || null;
  }

  function renderPostNav() {
    if (state.detailPosts.length < 2) return "";
    const previousPost = adjacentPost(1);
    const nextPost = adjacentPost(-1);

    const button = (post, direction, label) => {
      if (!post) {
        return (
          `<span class="post-nav-button ${direction} is-disabled" aria-disabled="true">`
          + `<span class="post-nav-label">${label}</span>`
          + '<span class="post-nav-title">없음</span>'
          + '</span>'
        );
      }
      return (
        `<a class="post-nav-button ${direction}" href="${postPath(post.id)}">`
        + `<span class="post-nav-label">${label}</span>`
        + `<span class="post-nav-title">${escapeHtml(displayTitle(post.title))}</span>`
        + '</a>'
      );
    };

    return (
      '<nav class="post-nav" aria-label="게시글 이동">'
      + button(previousPost, "previous", "이전 글")
      + button(nextPost, "next", "다음 글")
      + '</nav>'
    );
  }

  async function loadDetail(id) {
    state.selectedId = Number(id);
    detail.innerHTML = '<div class="empty">불러오는 중</div>';

    try {
      const response = await fetch(`/data/posts/${state.selectedId}.json`);
      if (!response.ok) throw new Error("detail failed");
      const post = await response.json();
      const title = displayTitle(post.title);
      const newMarker = isNewPost(post) ? '<span class="new-pill">NEW</span>' : "";
      const backPath = `/?type=${encodeURIComponent(post.type)}`;
      detail.innerHTML = (
        `<a class="detail-back" href="${backPath}">‹ ${escapeHtml(post.type_label)} 목록으로</a>`
        + `<h2 class="detail-title">${escapeHtml(title)}</h2>`
        + `<div class="detail-meta"><span class="badge ${post.type}">${escapeHtml(post.type_label)}</span><span>${escapeHtml(post.date)}</span>${newMarker}</div>`
        + `<div class="detail-content">${post.content || ""}</div>`
        + renderPostNav()
      );
      document.title = `${title} | 서이의 키즈노트`;
      cleanDetailContent(detail.querySelector(".detail-content"));
      state.lightboxItems = [];
      enhanceMediaLightbox(post);
    } catch {
      detail.innerHTML = '<div class="error">상세 내용을 불러오지 못했습니다.</div>';
    }
  }

  function refreshView() {
    applyFilters();
    renderTabs();
    renderPageHeader();
    renderList();
  }

  function clampZoom(value) {
    return Math.min(LIGHTBOX_MAX_ZOOM, Math.max(LIGHTBOX_MIN_ZOOM, value));
  }

  function lightboxFitSize() {
    const naturalWidth = lightboxImage.naturalWidth || 1;
    const naturalHeight = lightboxImage.naturalHeight || 1;
    const viewportWidth = Math.max(1, lightboxImageWrap.clientWidth);
    const viewportHeight = Math.max(1, lightboxImageWrap.clientHeight);
    const ratio = Math.min(viewportWidth / naturalWidth, viewportHeight / naturalHeight, 1);
    return {
      width: Math.max(1, Math.round(naturalWidth * ratio)),
      height: Math.max(1, Math.round(naturalHeight * ratio)),
    };
  }

  function lightboxAnchorFromEvent(event) {
    const wrapRect = lightboxImageWrap.getBoundingClientRect();
    const imageRect = lightboxImage.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (event.clientX - imageRect.left) / Math.max(1, imageRect.width))),
      y: Math.min(1, Math.max(0, (event.clientY - imageRect.top) / Math.max(1, imageRect.height))),
      offsetX: event.clientX - wrapRect.left,
      offsetY: event.clientY - wrapRect.top,
    };
  }

  function renderLightboxZoom(options = {}) {
    const previousCenter = options.preserveCenter ? {
      x: (lightboxImageWrap.scrollLeft + lightboxImageWrap.clientWidth / 2) / Math.max(1, lightboxImageWrap.scrollWidth),
      y: (lightboxImageWrap.scrollTop + lightboxImageWrap.clientHeight / 2) / Math.max(1, lightboxImageWrap.scrollHeight),
    } : null;
    const pointerAnchor = options.anchor || null;
    const size = lightboxFitSize();
    lightboxImage.style.width = `${Math.round(size.width * state.lightboxZoom)}px`;
    lightboxImage.style.height = `${Math.round(size.height * state.lightboxZoom)}px`;
    lightboxImageWrap.classList.toggle("is-zoomed", state.lightboxZoom > LIGHTBOX_MIN_ZOOM);
    lightboxZoomValue.textContent = `${Math.round(state.lightboxZoom * 100)}%`;
    lightboxZoomOut.disabled = state.lightboxZoom <= LIGHTBOX_MIN_ZOOM;
    lightboxZoomIn.disabled = state.lightboxZoom >= LIGHTBOX_MAX_ZOOM;

    window.requestAnimationFrame(() => {
      if (pointerAnchor) {
        lightboxImageWrap.scrollLeft = lightboxImage.offsetWidth * pointerAnchor.x - pointerAnchor.offsetX;
        lightboxImageWrap.scrollTop = lightboxImage.offsetHeight * pointerAnchor.y - pointerAnchor.offsetY;
      } else if (previousCenter) {
        lightboxImageWrap.scrollLeft = lightboxImageWrap.scrollWidth * previousCenter.x - lightboxImageWrap.clientWidth / 2;
        lightboxImageWrap.scrollTop = lightboxImageWrap.scrollHeight * previousCenter.y - lightboxImageWrap.clientHeight / 2;
      } else {
        lightboxImageWrap.scrollLeft = 0;
        lightboxImageWrap.scrollTop = 0;
      }
    });
  }

  function resetLightboxZoom() {
    state.lightboxZoom = LIGHTBOX_MIN_ZOOM;
    renderLightboxZoom();
  }

  function changeLightboxZoom(delta, anchor = null) {
    if (lightbox.hidden) return;
    const next = clampZoom(state.lightboxZoom + delta);
    if (next === state.lightboxZoom) return;
    state.lightboxZoom = next;
    renderLightboxZoom(anchor ? { anchor } : { preserveCenter: true });
  }

  function toggleLightboxZoom(anchor = null) {
    if (lightbox.hidden) return;
    state.lightboxZoom = state.lightboxZoom > LIGHTBOX_MIN_ZOOM ? LIGHTBOX_MIN_ZOOM : 2;
    renderLightboxZoom(anchor && state.lightboxZoom > LIGHTBOX_MIN_ZOOM ? { anchor } : { preserveCenter: state.lightboxZoom > LIGHTBOX_MIN_ZOOM });
  }

  function canDragLightboxImage() {
    return state.lightboxZoom > LIGHTBOX_MIN_ZOOM
      && (lightboxImageWrap.scrollWidth > lightboxImageWrap.clientWidth || lightboxImageWrap.scrollHeight > lightboxImageWrap.clientHeight);
  }

  function startLightboxDrag(event) {
    if (event.button !== 0 || lightbox.hidden || !canDragLightboxImage()) return;
    event.preventDefault();
    state.lightboxDrag = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      scrollLeft: lightboxImageWrap.scrollLeft,
      scrollTop: lightboxImageWrap.scrollTop,
    };
    lightboxImageWrap.classList.add("is-dragging");
    try {
      lightboxImageWrap.setPointerCapture?.(event.pointerId);
    } catch {
      // Synthetic pointer events in tests may not have an active pointer capture target.
    }
  }

  function moveLightboxDrag(event) {
    if (!state.lightboxDrag || event.pointerId !== state.lightboxDrag.pointerId) return;
    event.preventDefault();
    lightboxImageWrap.scrollLeft = state.lightboxDrag.scrollLeft - (event.clientX - state.lightboxDrag.x);
    lightboxImageWrap.scrollTop = state.lightboxDrag.scrollTop - (event.clientY - state.lightboxDrag.y);
  }

  function endLightboxDrag(event) {
    if (!state.lightboxDrag || event.pointerId !== state.lightboxDrag.pointerId) return;
    if (lightboxImageWrap.hasPointerCapture?.(event.pointerId)) {
      lightboxImageWrap.releasePointerCapture(event.pointerId);
    }
    state.lightboxDrag = null;
    lightboxImageWrap.classList.remove("is-dragging");
  }

  function handleLightboxWheel(event) {
    if (lightbox.hidden) return;
    event.preventDefault();
    const delta = event.deltaY < 0 ? LIGHTBOX_ZOOM_STEP : -LIGHTBOX_ZOOM_STEP;
    changeLightboxZoom(delta, lightboxAnchorFromEvent(event));
  }

  function lightboxDownloadName(item, index) {
    const srcPath = (item.src || "").split("?")[0];
    const extension = (srcPath.split(".").pop() || "jpg").replace(/[^a-zA-Z0-9]/g, "").slice(0, 5) || "jpg";
    const caption = (item.caption || item.alt || "seoi-kidsnote")
      .replace(/^_\\d+\\s*/, "")
      .replace(/[\\\\/:*?"<>|]+/g, " ")
      .replace(/\\s+/g, " ")
      .trim()
      .slice(0, 60) || "seoi-kidsnote";
    return `${caption}-${String(index + 1).padStart(2, "0")}.${extension}`;
  }

  async function downloadLightboxImage() {
    const item = state.lightboxItems[state.lightboxIndex];
    if (!item?.src || lightbox.hidden) return;
    lightboxDownload.disabled = true;
    try {
      const response = await fetch(item.src);
      if (!response.ok) throw new Error("download failed");
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = lightboxDownloadName(item, state.lightboxIndex);
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch {
      const link = document.createElement("a");
      link.href = item.src;
      link.download = lightboxDownloadName(item, state.lightboxIndex);
      link.target = "_blank";
      document.body.append(link);
      link.click();
      link.remove();
    } finally {
      lightboxDownload.disabled = false;
    }
  }

  function showLightbox(index, items = null) {
    if (items) state.lightboxItems = items;
    const item = state.lightboxItems[index];
    if (!item) return;
    state.lightboxIndex = index;
    state.lightboxZoom = LIGHTBOX_MIN_ZOOM;
    lightboxImage.removeAttribute("style");
    lightboxImage.src = item.src;
    lightboxImage.alt = item.alt;
    lightboxCaption.textContent = `${index + 1} / ${state.lightboxItems.length} · ${item.caption}`;
    lightboxPrev.hidden = state.lightboxItems.length < 2;
    lightboxNext.hidden = state.lightboxItems.length < 2;
    lightbox.hidden = false;
    window.requestAnimationFrame(resetLightboxZoom);
  }

  function closeLightbox() {
    lightbox.hidden = true;
    state.lightboxZoom = LIGHTBOX_MIN_ZOOM;
    state.lightboxDrag = null;
    lightboxImageWrap.classList.remove("is-zoomed");
    lightboxImageWrap.classList.remove("is-dragging");
    lightboxImage.removeAttribute("style");
    lightboxImage.removeAttribute("src");
  }

  function moveLightbox(delta) {
    if (!state.lightboxItems.length || lightbox.hidden) return;
    const next = (state.lightboxIndex + delta + state.lightboxItems.length) % state.lightboxItems.length;
    showLightbox(next);
  }

  async function loadApp() {
    renderBabyDays();
    const requestedType = new URLSearchParams(window.location.search).get("type");
    if (tabs.some((tab) => tab.key === requestedType)) {
      state.activeType = requestedType;
      window.localStorage.setItem("kidsnote.activeType", state.activeType);
    }
    renderRouteState();
    renderTabs();
    if (state.selectedId === null) {
      entryList.innerHTML = '<div class="empty">불러오는 중</div>';
    } else {
      detail.innerHTML = '<div class="empty">불러오는 중</div>';
    }

    try {
      const response = await fetch("/data/posts.json");
      if (!response.ok) throw new Error("list failed");
      const manifest = await response.json();
      state.allPosts = manifest.posts || [];
      state.counts = manifest.counts || state.counts;
      state.newCounts = countNewPosts(state.allPosts);
      renderSyncMeta(manifest.exported_at);
      if (state.selectedId !== null) {
        const selectedPost = state.allPosts.find((post) => post.id === state.selectedId);
        if (!selectedPost) throw new Error("post not found");
        state.activeType = selectedPost.type;
        state.detailPosts = state.allPosts.filter((post) => post.type === selectedPost.type);
        renderTabs();
        renderPageHeader();
        await loadDetail(state.selectedId);
      } else {
        document.title = "서이의 키즈노트";
        renderMonthOptions();
        refreshView();
      }
    } catch {
      if (state.selectedId === null) {
        entryList.innerHTML = '<div class="error">목록을 불러오지 못했습니다.</div>';
      } else {
        detail.innerHTML = '<a class="detail-back" href="/">‹ 목록으로</a><div class="error">상세 내용을 불러오지 못했습니다.</div>';
      }
    }
  }

  tabsNode.addEventListener("click", (event) => {
    const target = event.target.closest("button[data-type]");
    if (!target) return;
    state.activeType = target.dataset.type;
    window.localStorage.setItem("kidsnote.activeType", state.activeType);
    refreshView();
  });

  listToggle.addEventListener("click", toggleList);
  filterToggle.addEventListener("click", toggleFilters);

  searchInput.addEventListener("input", (event) => {
    state.query = event.target.value;
    refreshView();
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
    const target = event.target.closest("[data-gallery-index]");
    if (target) showLightbox(Number(target.dataset.galleryIndex), state.detailLightboxItems);
  });

  detail.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest("figure[data-gallery-index]");
    if (!target) return;
    event.preventDefault();
    showLightbox(Number(target.dataset.galleryIndex), state.detailLightboxItems);
  });

  profilePhoto.addEventListener("click", () => {
    showLightbox(0, [{
      src: PROFILE_IMAGE_SRC,
      alt: "서이",
      caption: "서이",
    }]);
  });

  lightbox.addEventListener("click", (event) => {
    if (event.target === lightbox || event.target.closest(".lightbox-close")) closeLightbox();
    if (event.target.closest(".lightbox-prev")) moveLightbox(-1);
    if (event.target.closest(".lightbox-next")) moveLightbox(1);
    if (event.target.closest(".lightbox-zoom-out")) changeLightboxZoom(-LIGHTBOX_ZOOM_STEP);
    if (event.target.closest(".lightbox-zoom-in")) changeLightboxZoom(LIGHTBOX_ZOOM_STEP);
    if (event.target.closest(".lightbox-zoom-reset")) resetLightboxZoom();
    if (event.target.closest(".lightbox-download")) downloadLightboxImage();
  });

  lightboxImage.addEventListener("load", resetLightboxZoom);
  lightboxImage.addEventListener("dblclick", (event) => {
    toggleLightboxZoom(lightboxAnchorFromEvent(event));
  });
  lightboxImageWrap.addEventListener("wheel", handleLightboxWheel, { passive: false });
  lightboxImageWrap.addEventListener("pointerdown", startLightboxDrag);
  lightboxImageWrap.addEventListener("pointermove", moveLightboxDrag);
  lightboxImageWrap.addEventListener("pointerup", endLightboxDrag);
  lightboxImageWrap.addEventListener("pointercancel", endLightboxDrag);
  lightboxImageWrap.addEventListener("lostpointercapture", () => {
    state.lightboxDrag = null;
    lightboxImageWrap.classList.remove("is-dragging");
  });
  window.addEventListener("resize", () => {
    if (!lightbox.hidden) renderLightboxZoom();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeLightbox();
    if (event.key === "ArrowLeft") moveLightbox(-1);
    if (event.key === "ArrowRight") moveLightbox(1);
    if (!lightbox.hidden && (event.key === "+" || event.key === "=")) {
      event.preventDefault();
      changeLightboxZoom(LIGHTBOX_ZOOM_STEP);
    }
    if (!lightbox.hidden && event.key === "-") {
      event.preventDefault();
      changeLightboxZoom(-LIGHTBOX_ZOOM_STEP);
    }
    if (event.key === "0" && !lightbox.hidden) {
      event.preventDefault();
      resetLightboxZoom();
    }
  });

  passcodeForm.addEventListener("submit", handlePasscodeSubmit);

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

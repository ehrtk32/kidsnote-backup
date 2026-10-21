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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PAGES_FILE_LIMIT = 20_000
PAGES_FILE_SIZE_LIMIT = 25 * 1024 * 1024

REPORT_ID_CANDIDATES = (
    "Report ID", "리포트 ID", "리포트id", "report_id", "보고서 ID",
    "번호", "숫자", "Number", "id", "ID",
)
DATE_CANDIDATES = ("Date", "날짜")

# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------
# Every post this export produces belongs to one child. Today there is exactly
# one Notion database (서이's), so everything is tagged with that key and the
# 살구 tab renders empty until there is data for them.
#
# EXTENSION POINT — when the second child gets their own Kidsnote account and
# Notion DB: read a second NOTION_DATABASE_ID_2 here, query it with the same
# NotionClient, and tag its pages "second". The frontend already filters on
# this field, so nothing there needs to change.
#
# KEYS ARE PERMANENT. "second" is deliberately not a name: that child is
# currently known by a 태명 (prenatal nickname) that changes once they are
# born and named. The key ends up in every D1 row's `child` column, so
# renaming it later orphans all their videos, comments and likes. Only the
# display name in APP_JS's `children` changes at that point — never this.
DEFAULT_CHILD = "seoi"
CHILD_KEYS = ("seoi", "second")

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


def build_retrying_session() -> requests.Session:
    """429 와 일시적인 5xx 에 물러섰다 다시 시도하는 Session.

    export 한 번은 글 수 × (페이지 쿼리 + 블록 children + 중첩 children) 이라
    수천 번의 호출이 된다. 재시도가 없으면 그중 한 번의 429 가 export 전체를
    죽이고 배포까지 막는다. 같은 저장소의 notion_mirror.py 가 이미 같은
    이유로 같은 설정을 쓰고 있다.
    """
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,  # 0s, 1s, 2s, 4s, 8s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST", "PATCH"}),
        # Notion 이 Retry-After 를 주면 그만큼 기다린다.
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class NotionClient:
    def __init__(self, token: str, database_id: str) -> None:
        self.database_id = database_id.replace("-", "")
        self.session = build_retrying_session()
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
        # 미디어도 수천 건이라 같은 이유로 재시도가 필요하다.
        self.session = build_retrying_session()
        self.media_indexes: dict[str, int] = {}
        self.first_image_url = ""
        self.missing_media: list[str] = []
        self.used_media_paths: set[Path] = set()
        # 사진 모아보기와 좋아요가 쓰는 전체 사진 목록. 사진은 본문 HTML
        # 안에만 있어서, 렌더링하면서 같이 모아 두지 않으면 나중에 다시
        # 파싱해야 한다.
        self.page_images: list[dict[str, Any]] = []

    def render_page(self, page: NotionPage) -> tuple[str, str]:
        self.media_indexes = {}
        self.first_image_url = ""
        self.page_images = []
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
            # 좋아요는 이 경로를 photo_id 로 쓴다. report id 와 글 안에서의
            # 순서로만 정해지므로 재-export 해도 같은 값이 나온다.
            self.page_images.append({"src": target_url, "caption": caption})
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
    for path in (
        out_dir / "data/posts.json",
        out_dir / "data/photos.json",
        out_dir / "export-report.json",
    ):
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
    photos: list[dict[str, Any]] = []
    for index, page in enumerate(pages, start=1):
        print(f"[{index}/{len(pages)}] export {page.report_id}: {page.title}", flush=True)
        content, thumbnail_url = renderer.render_page(page)
        ptype = post_type(page.title)
        summary = {
            "id": page.report_id,
            "child": DEFAULT_CHILD,
            "title": page.title,
            "date": display_date(page.date),
            "type": ptype,
            "type_label": post_type_label(ptype),
            "summary": html_excerpt(content),
            "thumbnail_url": thumbnail_url,
            "slug": f"kidsnote-{page.report_id}",
        }
        posts.append(summary)

        for image in renderer.page_images:
            entry = {
                "post_id": page.report_id,
                "child": DEFAULT_CHILD,
                "src": image["src"],
                "date": summary["date"],
                "type": ptype,
            }
            if image["caption"]:
                entry["caption"] = image["caption"]
            photos.append(entry)

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

    # 사진 모아보기용 전체 인덱스. 좋아요 수는 D1 에서 따로 가져와 합친다 —
    # 여기 넣으면 cron 이 다시 쓸 때마다 사람이 누른 것이 사라진다.
    write_text(
        out_dir / "data/photos.json",
        json.dumps(
            {"count": len(photos), "photos": photos},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    write_post_routes(out_dir, posts, index_html)

    # --limit 을 준 실행은 일부 글만 렌더하므로 used_media_paths 에 그 글들의
    # 미디어만 담긴다. 그대로 prune 하면 나머지 수 GB 를 전부 지운다 — CI 는
    # 어차피 빈 dist/ 로 시작해 무해하지만, 로컬에서는 CLAUDE.md 가 권하는
    # `--limit 5` 미리보기가 캐시를 통째로 날리는 결과가 된다.
    if args.limit is None:
        pruned_media_count = prune_unused_media(out_dir, renderer.used_media_paths)
    else:
        pruned_media_count = 0
        print("--limit 실행이므로 미디어 정리를 건너뜁니다.", flush=True)
    stats = file_stats(out_dir)
    report = {
        "out_dir": str(out_dir),
        "missing_media_count": len(renderer.missing_media),
        "missing_media": renderer.missing_media[:50],
        "pruned_media_count": pruned_media_count,
        "photo_count": len(photos),
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
  <title>키즈노트</title>
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%23548a62'/%3E%3Cpath fill='white' d='M32 52S10 39.5 10 24.5C10 16.8 15.8 12 22.2 12c4.2 0 7.6 2.1 9.8 5.5C34.2 14.1 37.6 12 41.8 12C48.2 12 54 16.8 54 24.5C54 39.5 32 52 32 52z'/%3E%3C/svg%3E">
  <link rel="stylesheet" href="__STYLES_HREF__">
</head>
<body class="locked">
  <section class="passcode-screen" id="passcodeScreen">
    <form class="passcode-card" id="passcodeForm" autocomplete="off">
      <div class="passcode-mark" aria-hidden="true">♥</div>
      <h1>우리 아이 키즈노트</h1>
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
        <div class="sidebar-brand" id="sidebarBrand">
          <span class="sidebar-brand-mark" aria-hidden="true">♥</span>
          <span class="sidebar-brand-copy">
            <strong><span id="brandName"></span>의 키즈노트</strong>
            <small id="brandRoman"></small>
          </span>
        </div>
      </header>
      <section class="profile" id="profile">
        <button class="profile-photo" id="profilePhoto" type="button">
          <img id="profileImage" src="" alt="">
        </button>
        <div class="profile-copy">
          <h1 id="profileName"></h1>
          <p class="profile-meta" id="profileMeta"></p>
        </div>
        <div class="child-switch" id="childSwitch" role="group" aria-label="자녀 전환"></div>
      </section>
      <nav class="tabs" id="tabs" aria-label="키즈노트 메뉴"></nav>
      <footer class="sidebar-footer">
        <div class="sync-meta" id="syncMeta">동기화 확인 중</div>
      </footer>
    </aside>

    <main class="workspace">
      <header class="content-header">
        <div>
          <p class="content-eyebrow" id="pageEyebrow"></p>
          <h2 class="content-title" id="pageTitle">홈</h2>
        </div>
        <div class="content-actions">
          <button class="filter-toggle-button" id="filterToggle" type="button" aria-expanded="false" aria-controls="filterTools">필터 목록 열기</button>
          <button class="admin-button" id="adminButton" type="button" aria-label="관리자 모드">관리자</button>
        </div>
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
      <button class="lightbox-download" type="button" aria-label="사진 다운로드"><img src="/assets/download.svg" alt=""></button>
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
  <div class="admin-modal" id="adminModal" hidden role="dialog" aria-modal="true" aria-labelledby="adminModalTitle">
    <div class="admin-panel">
      <header class="admin-head">
        <h3 id="adminModalTitle">관리자 모드</h3>
        <button class="admin-close" type="button" aria-label="닫기">&times;</button>
      </header>

      <form class="admin-form" id="adminLoginForm">
        <label for="adminPasscode">관리자 비밀번호</label>
        <input id="adminPasscode" type="password" inputmode="numeric" autocomplete="off" placeholder="6자리">
        <button class="admin-submit" type="submit">확인</button>
      </form>

      <form class="admin-form" id="adminVideoForm" hidden>
        <label for="videoChild">자녀</label>
        <select id="videoChild"></select>
        <label for="videoUrl">영상 공유 링크</label>
        <input id="videoUrl" type="url" placeholder="https://naver.me/..." autocomplete="off">
        <label for="videoTitle">제목</label>
        <input id="videoTitle" type="text" maxlength="200" autocomplete="off">
        <label for="videoDate">날짜 <span class="admin-optional">(선택)</span></label>
        <input id="videoDate" type="date">
        <button class="admin-submit" type="submit">영상 추가</button>
      </form>

      <form class="admin-form admin-form-secondary" id="adminPasscodeForm" hidden>
        <button class="admin-toggle" id="adminPasscodeToggle" type="button" aria-expanded="false">
          비밀번호 변경
        </button>
        <div id="adminPasscodeFields" hidden>
          <label for="currentPasscode">현재 비밀번호</label>
          <input id="currentPasscode" type="password" autocomplete="off">
          <label for="newPasscode">새 비밀번호</label>
          <input id="newPasscode" type="password" autocomplete="off" placeholder="4자 이상">
          <label for="newPasscodeConfirm">새 비밀번호 확인</label>
          <input id="newPasscodeConfirm" type="password" autocomplete="off">
          <button class="admin-submit" type="submit">변경</button>
        </div>
      </form>

      <p class="admin-message" id="adminMessage" aria-live="polite"></p>
    </div>
  </div>
  <div class="video-modal" id="videoModal" hidden role="dialog" aria-modal="true" aria-labelledby="videoModalTitle">
    <div class="video-modal-panel">
      <header class="video-modal-head">
        <h3 id="videoModalTitle"></h3>
        <button class="video-modal-close" type="button" aria-label="닫기">&times;</button>
      </header>
      <div class="video-modal-frame" id="videoModalFrame"></div>
      <div class="video-modal-comments" id="videoComments"></div>
      <footer class="video-modal-foot">
        <a id="videoModalLink" href="#" target="_blank" rel="noopener noreferrer">새 창에서 열기 ↗</a>
      </footer>
    </div>
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
  --video: #6a4b9c;
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

/* 사진이 아직 없는 자녀. 버튼이지만 열 게 없으니 확대 커서를 쓰지 않는다. */
.profile-photo.is-placeholder {
  display: grid;
  place-items: center;
  font-size: 34px;
  font-weight: 700;
  color: var(--accent);
  background: var(--accent-soft);
  cursor: default;
}

.profile-photo.is-placeholder:hover {
  transform: none;
}

.profile-meta:empty {
  display: none;
}

.child-switch {
  display: inline-flex;
  padding: 3px;
  gap: 3px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-soft);
}

.child-chip {
  padding: 5px 16px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s ease, color .15s ease;
}

.child-chip:hover {
  color: var(--ink);
}

.child-chip[aria-pressed="true"] {
  background: var(--surface);
  color: var(--accent-strong);
  box-shadow: 0 1px 3px rgba(32, 35, 31, .12);
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

/* ---------- 영상 ---------- */
/* 목록은 .entry 행을 재사용한다. 여기 있는 건 영상에만 필요한 차이뿐이다. */
.entry-thumb-video {
  color: var(--video);
  font-size: 20px;
  line-height: 1;
}

.badge.video {
  background: var(--video);
}

/* ---------- 댓글 ---------- */
.comments {
  margin-top: 28px;
  padding-top: 18px;
  border-top: 1px solid var(--line);
}

.comments-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 650;
}

.comments-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.comment {
  padding: 10px 12px;
  border-radius: 12px;
  background: var(--surface-soft);
}

.comment-body {
  margin: 0;
  font-size: 14px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.comment-time {
  display: block;
  margin-top: 4px;
  font-size: 11px;
  color: var(--muted);
}

.comment-form {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.comment-form textarea {
  flex: 1;
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 9px 11px;
  font: inherit;
  font-size: 14px;
  color: var(--ink);
  background: var(--surface);
  resize: vertical;
}

.comment-form textarea:focus {
  outline: 2px solid var(--accent-soft);
  border-color: var(--accent);
}

.comment-form button {
  flex: none;
  align-self: flex-end;
  border: 0;
  border-radius: 10px;
  padding: 10px 16px;
  background: var(--accent);
  color: #fff;
  font-size: 14px;
  font-weight: 650;
  cursor: pointer;
}

.comment-form button:disabled {
  opacity: .6;
  cursor: default;
}

.comment-message {
  margin: 8px 0 0;
  min-height: 16px;
  font-size: 13px;
  color: var(--danger);
}

/* ---------- 좋아요 ---------- */
.detail-content figure.has-like {
  position: relative;
}

.like-button {
  position: absolute;
  right: 10px;
  bottom: 10px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border: 0;
  border-radius: 999px;
  padding: 6px 11px;
  background: rgba(16, 18, 17, .55);
  color: rgba(255, 255, 255, .85);
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  transition: background .15s ease, color .15s ease, transform .1s ease;
}

.like-button:hover {
  background: rgba(16, 18, 17, .75);
}

.like-button:active {
  transform: scale(.94);
}

.thumb-icon {
  width: 14px;
  height: 14px;
  display: block;
}

/* 되돌리기가 없으니 "눌린 상태"가 없다. 대신 누를 때마다 한 번 튀어서
   연타해도 반응이 보이게 한다. */
.like-button.is-bumped {
  animation: like-bump .32s ease;
}

@keyframes like-bump {
  0%   { transform: scale(1); }
  35%  { transform: scale(1.22); background: var(--accent); color: #fff; }
  100% { transform: scale(1); }
}

.like-count {
  font-variant-numeric: tabular-nums;
  font-weight: 650;
}

.gallery-cell {
  position: relative;
  display: inline-grid;
}

/* ---------- 사진 모아보기 ---------- */
.photo-tools {
  display: flex;
  gap: 6px;
  padding: 12px 14px 0;
}

.photo-sort {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 5px 13px;
  background: var(--surface);
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
}

.photo-sort.is-active {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent-strong);
  font-weight: 650;
}

.photo-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 10px;
  padding: 12px 14px;
}

.photo-cell {
  position: relative;
  margin: 0;
  border-radius: 12px;
  overflow: hidden;
  background: var(--surface-soft);
  cursor: zoom-in;
}

.photo-cell img {
  display: block;
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
}

.photo-cell figcaption {
  position: absolute;
  left: 8px;
  bottom: 8px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(16, 18, 17, .55);
  color: rgba(255, 255, 255, .9);
  font-size: 11px;
}

.photo-cell .like-button {
  right: 8px;
  bottom: 8px;
  padding: 5px 9px;
  font-size: 12px;
}

.photo-more {
  display: block;
  width: calc(100% - 28px);
  margin: 0 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 11px;
  background: var(--surface);
  color: var(--muted);
  font-size: 14px;
  cursor: pointer;
}

.photo-more:hover {
  border-color: var(--accent);
  color: var(--accent-strong);
}

.video-modal-comments:not(:empty) {
  padding: 0 16px;
  max-height: 40vh;
  overflow-y: auto;
}

.video-modal-comments .comments {
  margin-top: 14px;
}

/* ---------- 관리자 모드 ---------- */
.content-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.admin-button {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 7px 12px;
  background: var(--surface);
  color: var(--muted);
  font-size: 13px;
  cursor: pointer;
}

.admin-button:hover {
  border-color: var(--accent);
  color: var(--accent-strong);
}

.admin-modal[hidden] {
  display: none;
}

.admin-modal {
  position: fixed;
  inset: 0;
  z-index: 70;
  background: rgba(10, 12, 11, .5);
  display: grid;
  place-items: center;
  padding: 16px;
}

.admin-panel {
  width: min(420px, 100%);
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  background: var(--surface);
  border-radius: 16px;
  box-shadow: var(--shadow);
}

.admin-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 10px 14px 18px;
  border-bottom: 1px solid var(--line);
}

.admin-head h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
}

.admin-close {
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 10px;
  background: var(--surface-soft);
  color: var(--ink);
  font-size: 21px;
  line-height: 1;
  cursor: pointer;
}

.admin-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 16px 18px 4px;
}

.admin-form[hidden] {
  display: none;
}

.admin-form label {
  font-size: 12px;
  font-weight: 600;
  color: var(--muted);
  margin-top: 6px;
}

.admin-optional {
  font-weight: 400;
}

.admin-form input,
.admin-form select {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 9px 11px;
  font-size: 14px;
  font-family: inherit;
  color: var(--ink);
  background: var(--surface);
}

.admin-form input:focus,
.admin-form select:focus {
  outline: 2px solid var(--accent-soft);
  border-color: var(--accent);
}

.admin-submit {
  margin-top: 14px;
  border: 0;
  border-radius: 10px;
  padding: 11px;
  background: var(--accent);
  color: #fff;
  font-size: 14px;
  font-weight: 650;
  cursor: pointer;
}

.admin-submit:hover {
  background: var(--accent-strong);
}

.admin-form-secondary {
  border-top: 1px solid var(--line);
  margin-top: 14px;
  padding-top: 12px;
}

.admin-toggle {
  align-self: flex-start;
  border: 0;
  padding: 2px 0;
  background: none;
  color: var(--muted);
  font-size: 13px;
  text-decoration: underline;
  cursor: pointer;
}

.admin-toggle:hover {
  color: var(--accent-strong);
}

#adminPasscodeFields {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

#adminPasscodeFields[hidden] {
  display: none;
}

.admin-message {
  margin: 10px 18px 16px;
  min-height: 18px;
  font-size: 13px;
  color: var(--muted);
}

.admin-message.is-error {
  color: var(--danger);
}

/* ---------- 영상 모달 ---------- */
body.modal-open {
  overflow: hidden;
}

/* `display: grid` below outranks the UA stylesheet's [hidden] rule, so the
   modal needs this or it renders on page load with an empty frame. */
.video-modal[hidden] {
  display: none;
}

.video-modal {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(10, 12, 11, .88);
  display: grid;
  place-items: center;
  padding: 16px;
}

.video-modal-panel {
  display: flex;
  flex-direction: column;
  width: min(960px, 100%);
  max-height: calc(100vh - 32px);
  background: var(--surface);
  border-radius: 16px;
  overflow: hidden;
}

.video-modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 10px 12px 16px;
  border-bottom: 1px solid var(--line);
}

.video-modal-head h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 650;
  overflow-wrap: anywhere;
}

.video-modal-close {
  flex: none;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 10px;
  background: var(--surface-soft);
  color: var(--ink);
  font-size: 22px;
  line-height: 1;
  cursor: pointer;
}

.video-modal-close:hover {
  background: var(--line);
}

.video-modal-frame {
  flex: 1;
  min-height: 0;
  aspect-ratio: 16 / 9;
  background: #000;
}

.video-modal-frame iframe {
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
}

.video-modal-foot {
  padding: 10px 16px 12px;
  border-top: 1px solid var(--line);
}

.video-modal-foot a {
  font-size: 13px;
  color: var(--muted);
  text-decoration: none;
}

.video-modal-foot a:hover {
  color: var(--video);
  text-decoration: underline;
}

@media (max-width: 640px) {
  .video-modal {
    padding: 0;
  }

  .video-modal-panel {
    width: 100%;
    height: 100%;
    max-height: 100vh;
    border-radius: 0;
  }

  .video-modal-frame {
    aspect-ratio: auto;
  }
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
  display: grid;
  place-items: center;
}

.lightbox-download img {
  width: 20px;
  height: 20px;
  display: block;
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
    /* overflow-x: auto forces the computed overflow-y to auto as well, so this
       row clips vertically at its padding edge. .tab-new-count is pulled 3px
       above the tab, so without padding-top the badge got sliced off. Keep
       enough room for the badge plus its 2px ring. */
    padding: 7px 10px 10px;
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

  // 자녀 설정.
  //
  // `key` 는 절대 바꾸지 말 것. D1 의 videos / comments / likes 가 이 값으로
  // 행을 묶는다. 둘째의 key 가 이름이 아니라 "second" 인 이유가 이것이다 —
  // 지금 부르는 "살구"는 태명이라 출생 후 바뀐다. 이름이 바뀌면 아래 `name`
  // 과 `roman` 만 고치면 되고, 저장된 데이터는 그대로 따라온다.
  //
  // `birth` 가 비면 D+ 카운터를 숨긴다. 생년월일이 생기면 여기 넣으면 켜진다.
  // `photo` 가 비면 이름 첫 글자 플레이스홀더를 쓴다.
  const children = [
    {
      key: "seoi",
      name: "서이",
      roman: "Seoi's Kidsnote",
      birth: "2025-03-04",
      photo: "/assets/seoi-profile.jpg",
    },
    {
      key: "second",
      name: "살구",
      roman: "Salgu's Kidsnote",
      birth: "",
      photo: "",
    },
  ];
  const DEFAULT_CHILD = "seoi";

  const tabs = [
    { key: "home", label: "홈", icon: "/assets/home.svg" },
    { key: "daily", label: "알림장", icon: "/assets/daily.svg" },
    { key: "album", label: "앨범", icon: "/assets/album.svg" },
    { key: "announcement", label: "공지", icon: "/assets/announcement.svg" },
    { key: "video", label: "영상", icon: "/assets/video.svg" },
    { key: "photos", label: "사진", icon: "/assets/photos.svg" },
  ];
  // Videos are not Notion posts: no detail route, no NEW pill, no month
  // filter. The tab reuses the list panel but takes its items from a
  // separate manifest, so every list path branches on this.
  const VIDEO_TYPE = "video";
  // 사진 모아보기. 글 목록이 아니라 사진 단위라 목록 경로가 또 갈린다.
  const PHOTOS_TYPE = "photos";
  const PHOTO_PAGE = 120;

  // 좋아요 아이콘. currentColor 라 버튼 색을 그대로 따라간다.
  const THUMB_ICON = (
    '<svg class="thumb-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    + '<path d="M7 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h3v12Zm2-12.2 4.2-7A2 2 0 0 1 17 3.8'
    + 'l-.9 4.5a1 1 0 0 0 1 1.2h3.2a2.5 2.5 0 0 1 2.4 3.1l-1.8 7A2.5 2.5 0 0 1 18.5 22H9V9.8Z"/>'
    + "</svg>"
  );

  function likeButtonHtml(src, postId, count) {
    return (
      `<button class="like-button" type="button" data-photo-src="${escapeHtml(src)}"`
      + ` data-post-id="${escapeHtml(String(postId))}" aria-label="좋아요">`
      + `${THUMB_ICON}<span class="like-count">${count || ""}</span></button>`
    );
  }
  const LIGHTBOX_MIN_ZOOM = 1;
  const LIGHTBOX_MAX_ZOOM = 4;
  const LIGHTBOX_ZOOM_STEP = 0.5;

  function postIdFromPath(pathname) {
    const match = String(pathname || "").match(/^\/posts\/(\d+)\/?$/);
    return match ? Number(match[1]) : null;
  }

  function postPath(id) {
    return `/posts/${Number(id)}/`;
  }

  const state = {
    activeChild: window.localStorage.getItem("kidsnote.activeChild") || DEFAULT_CHILD,
    activeType: window.localStorage.getItem("kidsnote.activeType") || "home",
    activeMonth: window.localStorage.getItem("kidsnote.activeMonth") || "",
    query: "",
    allPosts: [],
    posts: [],
    videos: [],
    videosLoaded: false,
    photos: [],
    photosLoaded: false,
    photoLikes: {},   // photo_id → 누른 횟수
    photoSort: window.localStorage.getItem("kidsnote.photoSort") || "date",
    photoLimit: PHOTO_PAGE,
    adminPasscode: "",
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
  const brandName = document.getElementById("brandName");
  const brandRoman = document.getElementById("brandRoman");
  const profileImage = document.getElementById("profileImage");
  const profileName = document.getElementById("profileName");
  const profileMeta = document.getElementById("profileMeta");
  const childSwitch = document.getElementById("childSwitch");
  const adminButton = document.getElementById("adminButton");
  const adminModal = document.getElementById("adminModal");
  const adminClose = document.querySelector(".admin-close");
  const adminLoginForm = document.getElementById("adminLoginForm");
  const adminPasscodeInput = document.getElementById("adminPasscode");
  const adminVideoForm = document.getElementById("adminVideoForm");
  const adminMessage = document.getElementById("adminMessage");
  const adminPasscodeForm = document.getElementById("adminPasscodeForm");
  const adminPasscodeToggle = document.getElementById("adminPasscodeToggle");
  const adminPasscodeFields = document.getElementById("adminPasscodeFields");
  const currentPasscode = document.getElementById("currentPasscode");
  const newPasscode = document.getElementById("newPasscode");
  const newPasscodeConfirm = document.getElementById("newPasscodeConfirm");
  const videoChild = document.getElementById("videoChild");
  const videoUrl = document.getElementById("videoUrl");
  const videoTitle = document.getElementById("videoTitle");
  const videoDate = document.getElementById("videoDate");
  const videoModal = document.getElementById("videoModal");
  const videoModalTitle = document.getElementById("videoModalTitle");
  const videoModalFrame = document.getElementById("videoModalFrame");
  const videoModalLink = document.getElementById("videoModalLink");
  const videoModalClose = document.querySelector(".video-modal-close");
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

  const activeChild = () =>
    children.find((child) => child.key === state.activeChild) || children[0];

  function daysSinceBirth(birth) {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date()).reduce((acc, part) => {
      acc[part.type] = Number(part.value);
      return acc;
    }, {});
    const [year, month, day] = birth.split("-").map(Number);
    const today = Date.UTC(parts.year, parts.month - 1, parts.day);
    const birthday = Date.UTC(year, month - 1, day);
    return Math.floor((today - birthday) / 86400000) + 1;
  }

  function renderProfile() {
    const child = activeChild();
    brandName.textContent = child.name;
    brandRoman.textContent = child.roman;
    profileName.textContent = child.name;

    if (child.photo) {
      profileImage.src = child.photo;
      profileImage.alt = child.name;
      profilePhoto.hidden = false;
      profilePhoto.setAttribute("aria-label", `${child.name} 사진 크게 보기`);
      profilePhoto.classList.remove("is-placeholder");
      profilePhoto.textContent = "";
      profilePhoto.replaceChildren(profileImage);
    } else {
      // 사진이 아직 없는 자녀는 이름 첫 글자를 쓴다. 라이트박스로 열 것도
      // 없으므로 버튼이지만 비활성으로 둔다.
      profilePhoto.hidden = false;
      profilePhoto.classList.add("is-placeholder");
      profilePhoto.removeAttribute("aria-label");
      profilePhoto.replaceChildren(document.createTextNode(child.name.slice(0, 1)));
    }

    if (child.birth) {
      const days = daysSinceBirth(child.birth);
      profileMeta.innerHTML =
        `<time datetime="${escapeHtml(child.birth)}">${escapeHtml(child.birth.replaceAll("-", "."))}</time>`
        + '<span aria-hidden="true">·</span>'
        + `<strong>D+${Math.max(1, days)}</strong>`;
    } else {
      profileMeta.textContent = "";
    }
  }

  function renderChildSwitch() {
    childSwitch.innerHTML = children.map((child) => (
      `<button class="child-chip" type="button" data-child="${escapeHtml(child.key)}"`
      + ` aria-pressed="${child.key === state.activeChild ? "true" : "false"}">`
      + `${escapeHtml(child.name)}</button>`
    )).join("");
  }

  function setActiveChild(key) {
    if (!children.some((child) => child.key === key) || key === state.activeChild) return;
    state.activeChild = key;
    window.localStorage.setItem("kidsnote.activeChild", key);
    renderProfile();
    renderChildSwitch();
    renderDocumentTitle();
    recomputeCounts();
    renderMonthOptions();
    refreshView();
  }

  function renderDocumentTitle() {
    document.title = `${activeChild().name}의 키즈노트`;
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

  // 탭 카운트는 매니페스트의 전체 합계가 아니라 지금 보고 있는 자녀 기준으로
  // 다시 센다. 자녀를 바꿀 때마다 호출된다.
  function recomputeCounts() {
    const posts = childPosts();
    state.counts = posts.reduce((counts, post) => {
      counts[post.type] = (counts[post.type] || 0) + 1;
      return counts;
    }, { daily: 0, album: 0, announcement: 0 });
    state.newCounts = countNewPosts(posts);
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
      pageEyebrow.textContent = `${activeChild().name}의 기록`;
      pageTitle.textContent = activeLabel();
      return;
    }
    if (state.activeType === "home") {
      pageEyebrow.textContent = "오늘과 어제의 새 소식";
      pageTitle.textContent = "홈";
      return;
    }
    pageEyebrow.textContent = `${activeChild().name}의 기록 모아보기`;
    pageTitle.textContent = activeLabel();
  }

  function toggleFilters() {
    state.filtersOpen = !state.filtersOpen;
    renderFilterPanelState();
    if (state.filtersOpen) searchInput.focus();
  }

  function renderMonthOptions() {
    // Videos contribute their months too, otherwise the 영상 tab offers a
    // month list that can only ever filter its own cards away.
    const months = [...new Set(
      [...childPosts(), ...childVideos(), ...childPhotos()].map(monthKey).filter(Boolean),
    )].sort().reverse();
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
        : tab.key === VIDEO_TYPE
          ? childVideos().length
          : tab.key === PHOTOS_TYPE
            ? childPhotos().length
            : state.counts[tab.key] || 0;
      const newCount = tab.key === "home" || tab.key === VIDEO_TYPE || tab.key === PHOTOS_TYPE
        ? 0
        : state.newCounts[tab.key] || 0;
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

  function childVideos() {
    return state.videos.filter((video) => (video.child || DEFAULT_CHILD) === state.activeChild);
  }

  function filteredVideos() {
    const query = normalize(state.query).trim();
    return childVideos().filter((video) => {
      if (state.activeMonth && monthKey(video) !== state.activeMonth) return false;
      if (!query) return true;
      return normalize([video.title, video.date, video.summary].join(" ")).includes(query);
    });
  }

  // 글에 child 가 없으면 서이 것으로 본다. 현재 Notion DB가 서이 하나뿐이라
  // 예전 export 가 만든 posts.json 에는 이 필드가 없다.
  const postChild = (post) => post.child || DEFAULT_CHILD;

  function childPosts() {
    return state.allPosts.filter((post) => postChild(post) === state.activeChild);
  }

  function applyFilters() {
    const query = normalize(state.query).trim();
    if (state.activeType === VIDEO_TYPE || state.activeType === PHOTOS_TYPE) {
      state.posts = [];
      return;
    }
    state.posts = childPosts().filter((post) => {
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

  function renderVideoList() {
    const videos = filteredVideos();
    const filters = [state.activeMonth, state.query.trim()].filter(Boolean).length;
    listTitle.textContent = activeListLabel();
    listCount.textContent = filters ? `${videos.length}개 필터됨` : `${videos.length}개`;
    renderListCollapseState();

    if (!videos.length) {
      // 지금 자녀 기준으로 판단해야 한다. 전체 영상 수로 보면 살구처럼
      // 영상이 하나도 없는 자녀에게 "필터 결과 없음"이라고 말하게 된다.
      entryList.innerHTML = childVideos().length
        ? '<div class="empty">조건에 맞는 영상이 없습니다.</div>'
        : state.videosLoaded
          ? `<div class="empty">${escapeHtml(activeChild().name)} 영상이 아직 없습니다.</div>`
          : '<div class="error">영상 목록을 불러오지 못했습니다.</div>';
      return;
    }

    // 알림장·앨범·공지가 쓰는 .entry 행을 그대로 쓴다. 썸네일이 없으니
    // 카드 그리드를 유지할 이유가 없고, .entry 가 이미 width/border/background
    // 를 눕혀 둬서 <button> 에 그대로 붙는다.
    entryList.innerHTML = videos.map((video) => {
      const date = video.date ? `<span>${escapeHtml(video.date)}</span>` : "";
      const summary = video.summary
        ? `<span class="entry-summary">${escapeHtml(video.summary)}</span>`
        : "";
      return (
        `<button class="entry" type="button" data-video-id="${escapeHtml(video.id)}">`
        + '<span class="entry-thumb entry-thumb-video" aria-hidden="true">▶</span>'
        + "<span>"
        + `<span class="entry-kicker"><span class="badge video">영상</span>${date}</span>`
        + `<span class="entry-title">${escapeHtml(video.title)}</span>${summary}`
        + "</span></button>"
      );
    }).join("");
  }

  function renderList() {
    if (state.activeType === VIDEO_TYPE) {
      renderVideoList();
      return;
    }
    if (state.activeType === PHOTOS_TYPE) {
      renderPhotoList();
      return;
    }
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

  // ---- 댓글 ----
  function commentSectionHtml() {
    return (
      '<section class="comments" id="comments">'
      + '<h3 class="comments-title">댓글</h3>'
      + '<div class="comments-list" id="commentsList"><div class="empty">불러오는 중</div></div>'
      + '<form class="comment-form" id="commentForm">'
      + '<textarea id="commentBody" rows="2" maxlength="1000" placeholder="한마디 남겨주세요"></textarea>'
      + '<button type="submit">등록</button>'
      + "</form>"
      + '<p class="comment-message" id="commentMessage" aria-live="polite"></p>'
      + "</section>"
    );
  }

  function formatCommentTime(ms) {
    const d = new Date(Number(ms));
    if (Number.isNaN(d.getTime())) return "";
    return new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    }).format(d);
  }

  function renderComments(list) {
    const node = document.getElementById("commentsList");
    if (!node) return;
    if (!list.length) {
      node.innerHTML = '<div class="empty">아직 댓글이 없습니다.</div>';
      return;
    }
    node.innerHTML = list.map((comment) => (
      '<article class="comment">'
      + `<p class="comment-body">${escapeHtml(comment.body)}</p>`
      + `<time class="comment-time">${escapeHtml(formatCommentTime(comment.created_at))}</time>`
      + "</article>"
    )).join("");
  }

  async function loadComments(type, id) {
    const node = document.getElementById("commentsList");
    try {
      const response = await fetch(`/api/comments?type=${type}&id=${encodeURIComponent(id)}`);
      const ctype = response.headers.get("content-type") || "";
      if (!response.ok || !ctype.includes("application/json")) throw new Error("comments");
      const payload = await response.json();
      renderComments(payload.comments || []);
    } catch {
      if (node) node.innerHTML = '<div class="error">댓글을 불러오지 못했습니다.</div>';
    }
  }

  function wireCommentForm(type, id) {
    const form = document.getElementById("commentForm");
    const input = document.getElementById("commentBody");
    const message = document.getElementById("commentMessage");
    if (!form) return;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const body = input.value.trim();
      if (!body) return;
      message.textContent = "";
      form.querySelector("button").disabled = true;
      try {
        const response = await fetch("/api/comments", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ type, id: String(id), child: state.activeChild, body }),
        });
        const ctype = response.headers.get("content-type") || "";
        if (!ctype.includes("application/json")) throw new Error("no api");
        const payload = await response.json();
        if (!response.ok) {
          message.textContent = response.status === 429
            ? "잠시 후에 다시 시도해주세요."
            : (payload.error || "등록하지 못했습니다.");
          return;
        }
        input.value = "";
        await loadComments(type, id);
      } catch {
        message.textContent = "등록하지 못했습니다.";
      } finally {
        form.querySelector("button").disabled = false;
      }
    });
  }

  // ---- 사진 좋아요 ----
  // 되돌리기가 없다. 마음에 들면 여러 번 눌러도 되는 버튼이라, 누를 때마다
  // 서버 카운터가 1씩 올라간다.
  async function bumpLike(button) {
    const photoSrc = button.dataset.photoSrc;
    if (!photoSrc) return;

    // 응답을 기다리는 동안에도 눌린 느낌이 나야 연타가 어색하지 않다.
    button.classList.remove("is-bumped");
    void button.offsetWidth;   // 애니메이션 재시작
    button.classList.add("is-bumped");

    try {
      const response = await fetch("/api/likes", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          photo_id: photoSrc,
          post_id: button.dataset.postId || "",
          child: state.activeChild,
        }),
      });
      const ctype = response.headers.get("content-type") || "";
      if (!response.ok || !ctype.includes("application/json")) return;
      const payload = await response.json();
      state.photoLikes[photoSrc] = payload.count;
      // 같은 사진이 상세와 모아보기에 동시에 떠 있을 수 있다.
      document.querySelectorAll(`[data-photo-src="${CSS.escape(photoSrc)}"]`).forEach((node) => {
        const count = node.querySelector(".like-count");
        if (count) count.textContent = payload.count || "";
      });
    } catch {
      // 네트워크가 끊겼어도 버튼은 계속 눌러진다. 다음 로드에서 맞춰진다.
    }
  }

  /** 상세 본문의 사진마다 하트를 붙이고 현재 집계를 반영한다. */
  async function enhancePhotoLikes(post) {
    const content = detail.querySelector(".detail-content");
    if (!content) return;
    const figures = Array.from(content.querySelectorAll("figure"));
    if (!figures.length) return;

    function makeLikeButton(src) {
      const holder = document.createElement("div");
      holder.innerHTML = likeButtonHtml(src, post.id, state.photoLikes[src] || 0);
      return holder.firstElementChild;
    }

    const gallery = content.querySelector(".album-gallery");
    if (gallery) {
      // 앨범 글은 원본 figure 가 CSS 로 숨겨지고 타일 버튼이 대신 보인다.
      // figure 에 하트를 붙이면 보이지 않으므로 타일 쪽에 붙인다. 버튼 안에
      // 버튼을 넣을 수는 없어서 타일을 감싸고 형제로 둔다.
      Array.from(gallery.querySelectorAll(".gallery-item")).forEach((tile, index) => {
        const item = state.detailLightboxItems[index];
        if (!item) return;
        const src = new URL(item.src, window.location.origin).pathname;
        const cell = document.createElement("div");
        cell.className = "gallery-cell";
        tile.replaceWith(cell);
        cell.appendChild(tile);
        cell.appendChild(makeLikeButton(src));
      });
    } else {
      figures.forEach((figure) => {
        const image = figure.querySelector("img");
        if (!image) return;
        const src = new URL(image.getAttribute("src"), window.location.origin).pathname;
        figure.classList.add("has-like");
        figure.appendChild(makeLikeButton(src));
      });
    }

    try {
      const response = await fetch(`/api/likes?post=${encodeURIComponent(post.id)}`);
      const ctype = response.headers.get("content-type") || "";
      if (!response.ok || !ctype.includes("application/json")) return;
      const payload = await response.json();
      for (const [src, info] of Object.entries(payload.photos || {})) {
        state.photoLikes[src] = info.count;
        content.querySelectorAll(`[data-photo-src="${CSS.escape(src)}"]`).forEach((node) => {
          const count = node.querySelector(".like-count");
          if (count) count.textContent = info.count || "";
        });
      }
    } catch {
      // 집계를 못 가져와도 하트는 눌러진다.
    }
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
        + commentSectionHtml()
        + renderPostNav()
      );
      document.title = `${title} | ${activeChild().name}의 키즈노트`;
      cleanDetailContent(detail.querySelector(".detail-content"));
      state.lightboxItems = [];
      enhanceMediaLightbox(post);
      enhancePhotoLikes(post);
      wireCommentForm("post", post.id);
      loadComments("post", post.id);
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

  // ---- 관리자 모드 ----
  // 비밀번호는 Function 이 Pages Secret 과 대조한다. 여기서 들고 있는 값은
  // 다음 요청에 같이 보내기 위한 것일 뿐이고, 이 코드가 통과 여부를 정하지
  // 않는다. 사이트 입구의 가족 패스코드와 다른 점이 이것이다.
  function setAdminMessage(text, isError) {
    adminMessage.textContent = text;
    adminMessage.classList.toggle("is-error", Boolean(isError));
  }

  function openAdminModal() {
    adminModal.hidden = false;
    document.body.classList.add("modal-open");
    setAdminMessage("", false);
    if (state.adminPasscode) {
      showAdminVideoForm();
    } else {
      adminLoginForm.hidden = false;
      adminVideoForm.hidden = true;
      adminPasscodeForm.hidden = true;
      adminPasscodeInput.value = "";
      adminPasscodeInput.focus();
    }
  }

  function closeAdminModal() {
    if (adminModal.hidden) return;
    adminModal.hidden = true;
    document.body.classList.remove("modal-open");
    adminPasscodeInput.value = "";
  }

  function showAdminVideoForm() {
    adminLoginForm.hidden = true;
    adminVideoForm.hidden = false;
    adminPasscodeForm.hidden = false;
    videoChild.innerHTML = children.map((child) => (
      `<option value="${escapeHtml(child.key)}"${child.key === state.activeChild ? " selected" : ""}>`
      + `${escapeHtml(child.name)}</option>`
    )).join("");
    videoUrl.focus();
  }

  /** 관리자 API 호출 공통. 통과하면 payload, 아니면 null 을 주고 메시지를 띄운다. */
  async function adminRequest(url, method, body) {
    let response;
    try {
      response = await fetch(url, {
        method,
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ ...body, passcode: state.adminPasscode }),
      });
    } catch {
      setAdminMessage("서버에 연결하지 못했습니다.", true);
      return null;
    }

    const type = response.headers.get("content-type") || "";
    if (!type.includes("application/json")) {
      setAdminMessage("API를 찾을 수 없습니다. 배포에 Functions가 빠졌을 수 있습니다.", true);
      return null;
    }

    const payload = await response.json().catch(() => ({}));
    if (response.ok) return payload;

    if (response.status === 429) {
      const minutes = Math.ceil((payload.retry_after_seconds || 0) / 60);
      setAdminMessage(`시도가 너무 많습니다. ${minutes}분 뒤에 다시 해주세요.`, true);
    } else if (response.status === 401) {
      // 비번이 틀렸거나 바뀐 것이므로 들고 있던 값을 버린다.
      state.adminPasscode = "";
      adminLoginForm.hidden = false;
      adminVideoForm.hidden = true;
      adminPasscodeForm.hidden = true;
      adminPasscodeInput.value = "";
      adminPasscodeInput.focus();
      setAdminMessage("비밀번호가 맞지 않습니다.", true);
    } else {
      setAdminMessage(payload.error || "요청에 실패했습니다.", true);
    }
    return null;
  }

  async function handleAdminLogin(event) {
    event.preventDefault();
    const value = adminPasscodeInput.value.trim();
    if (!value) {
      setAdminMessage("비밀번호를 입력해주세요.", true);
      return;
    }
    // 전용 로그인 엔드포인트를 두지 않는다. 목록 조회는 비번이 필요 없고,
    // 추가는 어차피 비번을 같이 보내므로, 확인은 첫 추가에서 자연히 이뤄진다.
    // 여기서는 값만 받아 두고 폼을 연다.
    state.adminPasscode = value;
    setAdminMessage("", false);
    showAdminVideoForm();
  }

  function clearPasscodeFields() {
    currentPasscode.value = "";
    newPasscode.value = "";
    newPasscodeConfirm.value = "";
  }

  async function handlePasscodeChange(event) {
    event.preventDefault();
    const current = currentPasscode.value.trim();
    const next = newPasscode.value.trim();
    const confirm = newPasscodeConfirm.value.trim();

    if (!current || !next) {
      setAdminMessage("현재 비밀번호와 새 비밀번호를 입력해주세요.", true);
      return;
    }
    if (next !== confirm) {
      setAdminMessage("새 비밀번호가 서로 다릅니다.", true);
      return;
    }
    if (next.length < 4) {
      setAdminMessage("새 비밀번호는 4자 이상이어야 합니다.", true);
      return;
    }

    setAdminMessage("변경하는 중...", false);
    // 현재 비번은 폼에 입력된 값을 쓴다. 모달에 들고 있던 값이 아니라 —
    // 바꾸는 사람이 지금 알고 있는지 확인하는 것이 요점이기 때문이다.
    let response;
    try {
      response = await fetch("/api/admin/passcode", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ passcode: current, new_passcode: next }),
      });
    } catch {
      setAdminMessage("서버에 연결하지 못했습니다.", true);
      return;
    }

    const type = response.headers.get("content-type") || "";
    if (!type.includes("application/json")) {
      setAdminMessage("API를 찾을 수 없습니다. 배포에 Functions가 빠졌을 수 있습니다.", true);
      return;
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 429) {
        const minutes = Math.ceil((payload.retry_after_seconds || 0) / 60);
        setAdminMessage(`시도가 너무 많습니다. ${minutes}분 뒤에 다시 해주세요.`, true);
      } else if (response.status === 401) {
        setAdminMessage("현재 비밀번호가 맞지 않습니다.", true);
      } else {
        setAdminMessage(payload.error || "변경하지 못했습니다.", true);
      }
      return;
    }

    // 들고 있던 값이 낡았으므로 새 것으로 갈아 끼운다. 안 그러면 다음
    // 영상 추가에서 401 이 나고 로그인 화면으로 튕긴다.
    state.adminPasscode = next;
    clearPasscodeFields();
    adminPasscodeFields.hidden = true;
    adminPasscodeToggle.setAttribute("aria-expanded", "false");
    setAdminMessage("비밀번호를 변경했습니다.", false);
  }

  async function handleVideoSubmit(event) {
    event.preventDefault();
    const payload = {
      child: videoChild.value,
      url: videoUrl.value.trim(),
      title: videoTitle.value.trim(),
      date: videoDate.value,
    };
    if (!payload.url || !payload.title) {
      setAdminMessage("링크와 제목은 필수입니다.", true);
      return;
    }

    setAdminMessage("추가하는 중...", false);
    const result = await adminRequest("/api/videos", "POST", payload);
    if (!result) return;

    setAdminMessage(`"${result.video.title}" 추가했습니다.`, false);
    videoUrl.value = "";
    videoTitle.value = "";
    videoDate.value = "";
    videoUrl.focus();

    await loadVideos();
    // 방금 넣은 영상이 지금 보고 있는 자녀의 것이 아니면 그쪽으로 옮겨 준다.
    if (result.video.child !== state.activeChild) {
      setActiveChild(result.video.child);
    } else {
      recomputeCounts();
      renderMonthOptions();
      refreshView();
    }
  }

  function openVideoModal(id) {
    const video = state.videos.find((item) => item.id === id);
    if (!video) return;
    videoModalTitle.textContent = video.title;
    videoModalLink.href = video.url;
    // The iframe is built on open and destroyed on close rather than kept
    // hidden: the host autoplays as soon as it is framed, so a parked
    // iframe would mean audio from a modal nobody opened.
    const frame = document.createElement("iframe");
    frame.src = video.url;
    frame.title = video.title;
    frame.allow = "autoplay; fullscreen; encrypted-media";
    frame.allowFullscreen = true;
    frame.referrerPolicy = "no-referrer-when-downgrade";
    videoModalFrame.replaceChildren(frame);
    videoModal.hidden = false;
    document.body.classList.add("modal-open");
    videoModalClose.focus();

    const slot = document.getElementById("videoComments");
    if (slot) {
      slot.innerHTML = commentSectionHtml();
      wireCommentForm("video", video.id);
      loadComments("video", video.id);
    }
  }

  function closeVideoModal() {
    if (videoModal.hidden) return;
    videoModal.hidden = true;
    videoModalFrame.replaceChildren();
    const slot = document.getElementById("videoComments");
    if (slot) slot.innerHTML = "";
    videoModalLink.removeAttribute("href");
    document.body.classList.remove("modal-open");
  }

  async function loadVideos() {
    try {
      const response = await fetch("/api/videos", { headers: { accept: "application/json" } });
      // Functions 가 빠진 배포에서는 /api/* 가 SPA 폴백으로 떨어져 200 에
      // HTML 이 온다. 상태 코드만 보면 통과해 버리므로 타입을 확인한다.
      const type = response.headers.get("content-type") || "";
      if (!response.ok || !type.includes("application/json")) throw new Error("videos api");
      const payload = await response.json();
      state.videos = payload.videos || [];
      state.videosLoaded = true;
    } catch {
      state.videos = [];
      state.videosLoaded = false;
    }
  }

  async function loadPhotos() {
    try {
      const response = await fetch("/data/photos.json");
      if (!response.ok) throw new Error("photos");
      const payload = await response.json();
      state.photos = payload.photos || [];
      state.photosLoaded = true;
    } catch {
      state.photos = [];
      state.photosLoaded = false;
    }
  }

  /** 많이 눌린 순 집계를 받아 photoLikes 에 합친다. 실패해도 목록은 보인다. */
  async function loadTopLikes() {
    try {
      const response = await fetch("/api/likes?top=500");
      const type = response.headers.get("content-type") || "";
      if (!response.ok || !type.includes("application/json")) return;
      const payload = await response.json();
      for (const row of payload.photos || []) {
        state.photoLikes[row.photo_id] = row.count;
      }
    } catch {
      // 좋아요 수가 없어도 사진은 보여야 한다.
    }
  }

  function childPhotos() {
    return state.photos.filter((photo) => (photo.child || DEFAULT_CHILD) === state.activeChild);
  }

  function filteredPhotos() {
    const query = normalize(state.query).trim();
    let list = childPhotos().filter((photo) => {
      if (state.activeMonth && String(photo.date || "").slice(0, 7) !== state.activeMonth) return false;
      if (!query) return true;
      return normalize([photo.caption, photo.date].join(" ")).includes(query);
    });
    if (state.photoSort === "likes") {
      list = [...list].sort((a, b) => {
        const la = state.photoLikes[a.src] || 0;
        const lb = state.photoLikes[b.src] || 0;
        if (lb !== la) return lb - la;
        return String(b.date).localeCompare(String(a.date));
      });
    } else {
      list = [...list].sort((a, b) => String(b.date).localeCompare(String(a.date)));
    }
    return list;
  }

  function renderPhotoList() {
    const photos = filteredPhotos();
    const filters = [state.activeMonth, state.query.trim()].filter(Boolean).length;
    listTitle.textContent = "사진 모아보기";
    listCount.textContent = filters ? `${photos.length}장 필터됨` : `${photos.length}장`;
    renderListCollapseState();

    if (!photos.length) {
      entryList.innerHTML = state.photosLoaded
        ? `<div class="empty">${escapeHtml(activeChild().name)} 사진이 아직 없습니다.</div>`
        : '<div class="error">사진 목록을 불러오지 못했습니다.</div>';
      return;
    }

    const shown = photos.slice(0, state.photoLimit);
    const cards = shown.map((photo, index) => (
      `<figure class="photo-cell" data-photo-index="${index}">`
      + `<img src="${escapeHtml(photo.src)}" alt="${escapeHtml(photo.caption || "")}" loading="lazy">`
      + likeButtonHtml(photo.src, photo.post_id, state.photoLikes[photo.src] || 0)
      + `<figcaption>${escapeHtml(photo.date || "")}</figcaption>`
      + "</figure>"
    )).join("");

    const more = photos.length > shown.length
      ? `<button class="photo-more" type="button" id="photoMore">더 보기 (${photos.length - shown.length}장 남음)</button>`
      : "";

    entryList.innerHTML = (
      '<div class="photo-tools">'
      + `<button class="photo-sort${state.photoSort === "date" ? " is-active" : ""}" type="button" data-sort="date">최신순</button>`
      + `<button class="photo-sort${state.photoSort === "likes" ? " is-active" : ""}" type="button" data-sort="likes">좋아요순</button>`
      + "</div>"
      + `<div class="photo-grid">${cards}</div>${more}`
    );

    // 모아보기에서 사진을 누르면 라이트박스로 크게 본다.
    state.lightboxItems = shown.map((photo) => ({
      src: photo.src,
      alt: photo.caption || "",
      caption: photo.caption || photo.date || "",
    }));
  }

  async function loadApp() {
    const params = new URLSearchParams(window.location.search);
    const requestedChild = params.get("child");
    if (children.some((child) => child.key === requestedChild)) {
      state.activeChild = requestedChild;
      window.localStorage.setItem("kidsnote.activeChild", state.activeChild);
    }
    renderProfile();
    renderChildSwitch();
    renderDocumentTitle();
    const requestedType = params.get("type");
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
      recomputeCounts();
      renderSyncMeta(manifest.exported_at);
      if (state.selectedId !== null) {
        const selectedPost = state.allPosts.find((post) => post.id === state.selectedId);
        if (!selectedPost) throw new Error("post not found");
        // 딥링크로 들어온 글이 지금 보고 있는 자녀의 것이 아닐 수 있다.
        // 글 쪽으로 맞춰야 사이드바와 본문이 따로 놀지 않는다.
        if (postChild(selectedPost) !== state.activeChild) {
          state.activeChild = postChild(selectedPost);
          window.localStorage.setItem("kidsnote.activeChild", state.activeChild);
          renderProfile();
          renderChildSwitch();
          renderDocumentTitle();
        }
        recomputeCounts();
        state.activeType = selectedPost.type;
        state.detailPosts = childPosts().filter((post) => post.type === selectedPost.type);
        renderTabs();
        renderPageHeader();
        await loadDetail(state.selectedId);
        // 상세를 먼저 그리고 나서 채운다. photos.json 은 수천 장짜리라
        // 기다렸다가 그리면 본문이 그만큼 늦게 뜬다.
        Promise.all([loadVideos(), loadPhotos()]).then(() => {
          recomputeCounts();
          renderTabs();
        });
      } else {
        renderDocumentTitle();
        await Promise.all([loadVideos(), loadPhotos()]);
        await loadTopLikes();
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

  childSwitch.addEventListener("click", (event) => {
    const chip = event.target.closest("button[data-child]");
    if (chip) setActiveChild(chip.dataset.child);
  });

  entryList.addEventListener("click", (event) => {
    const like = event.target.closest(".like-button");
    if (like) {
      event.preventDefault();
      bumpLike(like);
      return;
    }

    const sort = event.target.closest("button[data-sort]");
    if (sort) {
      state.photoSort = sort.dataset.sort;
      window.localStorage.setItem("kidsnote.photoSort", state.photoSort);
      state.photoLimit = PHOTO_PAGE;
      renderPhotoList();
      return;
    }

    if (event.target.closest("#photoMore")) {
      state.photoLimit += PHOTO_PAGE;
      renderPhotoList();
      return;
    }

    const cell = event.target.closest("figure[data-photo-index]");
    if (cell) {
      showLightbox(Number(cell.dataset.photoIndex), state.lightboxItems);
      return;
    }

    const card = event.target.closest("button[data-video-id]");
    if (!card) return;
    openVideoModal(card.dataset.videoId);
  });

  adminButton.addEventListener("click", openAdminModal);
  adminClose.addEventListener("click", closeAdminModal);
  adminModal.addEventListener("click", (event) => {
    if (event.target === adminModal) closeAdminModal();
  });
  adminLoginForm.addEventListener("submit", handleAdminLogin);
  adminVideoForm.addEventListener("submit", handleVideoSubmit);
  adminPasscodeForm.addEventListener("submit", handlePasscodeChange);
  adminPasscodeToggle.addEventListener("click", () => {
    const open = adminPasscodeFields.hidden;
    adminPasscodeFields.hidden = !open;
    adminPasscodeToggle.setAttribute("aria-expanded", String(open));
    if (open) currentPasscode.focus(); else clearPasscodeFields();
  });

  videoModalClose.addEventListener("click", closeVideoModal);
  videoModal.addEventListener("click", (event) => {
    if (event.target === videoModal) closeVideoModal();
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
    const like = event.target.closest(".like-button");
    if (like) {
      event.preventDefault();
      event.stopPropagation();
      bumpLike(like);
      return;
    }
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
    const child = activeChild();
    if (!child.photo) return;
    showLightbox(0, [{ src: child.photo, alt: child.name, caption: child.name }]);
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
    // The video modal takes Escape first so closing it never also tears
    // down a lightbox sitting behind it.
    if (event.key === "Escape" && !adminModal.hidden) {
      closeAdminModal();
      return;
    }
    if (event.key === "Escape" && !videoModal.hidden) {
      closeVideoModal();
      return;
    }
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

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
<body>
  <div class="app-shell" id="appShell">
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
        <button class="admin-hotspot admin-hotspot-desktop" id="adminHotspotDesktop"
                type="button" tabindex="-1" aria-hidden="true"></button>
      </footer>
    </aside>

    <main class="workspace">
      <header class="content-header">
        <div>
          <p class="content-eyebrow" id="pageEyebrow"></p>
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
          <div class="panel-head-row">
            <button class="panel-head panel-toggle" id="listToggle" type="button" aria-expanded="true" aria-controls="entryList">
              <span class="panel-title" id="listTitle">알림장</span>
              <span class="panel-head-right">
                <span class="panel-meta" id="listCount">0</span>
                <span class="panel-chevron" id="listChevron" aria-hidden="true">▾</span>
              </span>
            </button>
            <!-- 제목 줄 자체가 <button> 이라 그 안에 버튼을 못 넣는다.
                 형제로 두고 한 줄처럼 보이게 묶는다. -->
            <span class="panel-head-tools" id="listTools"></span>
          </div>
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
      <figcaption class="lightbox-caption-bar">
        <span class="lightbox-like" id="lightboxLike" hidden></span>
        <span id="lightboxCaption"></span>
      </figcaption>
    </figure>
    <button class="lightbox-nav lightbox-next" type="button" aria-label="다음 사진">&#8250;</button>
  </div>
  <input class="upload-input" id="uploadInput" type="file" multiple hidden>
  <div class="upload-sheet" id="uploadSheet" hidden>
    <p class="upload-title" id="uploadTitle"></p>
    <div class="upload-track"><span class="upload-fill" id="uploadFill"></span></div>
    <p class="upload-note" id="uploadNote"></p>
  </div>
  <div class="admin-banner" id="adminBanner" hidden>관리자 모드</div>
  <button class="admin-hotspot admin-hotspot-mobile" id="adminHotspotMobile"
          type="button" tabindex="-1" aria-hidden="true"></button>
  <div class="admin-modal" id="adminModal" hidden role="dialog" aria-modal="true" aria-labelledby="adminModalTitle">
    <div class="admin-panel">
      <header class="admin-head">
        <h3 id="adminModalTitle">관리자 모드</h3>
        <button class="admin-close" type="button" aria-label="닫기">&times;</button>
      </header>

      <div class="admin-body">
      <form class="admin-form" id="adminLoginForm">
        <label for="adminPasscode">관리자 비밀번호</label>
        <input id="adminPasscode" type="password" inputmode="numeric" autocomplete="off" placeholder="6자리">
        <button class="admin-submit" type="submit">확인</button>
      </form>

      <div class="admin-session" id="adminSessionBox" hidden>
        <p>관리자 모드가 켜져 있습니다. 영상과 댓글에 삭제 버튼이 보입니다.</p>
        <button class="admin-exit" id="adminExit" type="button">관리자 모드 끄기</button>
      </div>

      <form class="admin-form admin-form-secondary" id="adminMyboxForm" hidden>
        <button class="admin-toggle" id="adminMyboxToggle" type="button" aria-expanded="false">
          MYBOX 폴더에서 가져오기
        </button>
        <div id="adminMyboxFields" hidden>
          <label for="myboxChild">자녀</label>
          <select id="myboxChild"></select>
          <label for="myboxFolder">폴더</label>
          <select id="myboxFolder"><option value="">불러오는 중...</option></select>
          <button class="admin-submit" type="submit">가져오기</button>
        </div>
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
      </div>

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
        <button class="video-step" id="videoPrev" type="button">‹ 이전</button>
        <span class="video-step-mid">
          <span class="video-like" id="videoLike"></span>
          <span class="video-step-count" id="videoStepCount"></span>
        </span>
        <button class="video-step" id="videoNext" type="button">다음 ›</button>
      </footer>
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

/* 위의 scale 은 서이 사진에서 얼굴만 남기려고 맞춘 크롭이다. 일러스트는
   원형에 꽉 차게 그려져 있어서 그대로 두면 배만 확대된다. */
.profile-photo.is-illustration img {
  transform: none;
  object-fit: contain;
}

/* 크게 열 게 없으니 확대 커서도 쓰지 않는다. 플레이스홀더와 같은 이유다.
   그림이 투명 PNG 라 원의 배경색이 그대로 비친다. 기본값(연한 회녹색)을 두면
   살빛이 탁해 보여서 따뜻한 쪽으로 깐다. */
.profile-photo.is-illustration {
  cursor: default;
  background: #fdf0e6;
}

.profile-photo.is-illustration:hover {
  transform: none;
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

/* 탭 묶음의 머리글. 누를 수 없는 라벨이라 탭과 확실히 달라 보여야 한다 —
   같은 크기로 두면 비활성 탭처럼 보인다. */
.tab-group {
  margin: 10px 0 2px;
  padding: 0 10px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 760;
  letter-spacing: .06em;
}

.tab-group:first-child {
  margin-top: 0;
}

/* 그룹에 속하지 않는데 그룹 뒤에 오는 탭. 머리글이 없어서 앞 묶음에 딸린
   것처럼 보이므로 선으로 끊는다. */
.tab.is-standalone {
  margin-top: 10px;
  border-top: 1px solid var(--line);
  padding-top: 14px;
  border-radius: 0 0 7px 7px;
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

/* 제목 줄. 접기 버튼이 남는 폭을 전부 먹고, 도구는 오른쪽에 붙는다. */
.panel-head-row {
  display: flex;
  align-items: stretch;
  border-bottom: 1px solid var(--line);
}

.panel-head-row .panel-head {
  flex: 1;
  min-width: 0;
  border-bottom: 0;
}

.panel-head-tools {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding-right: 10px;
}

.panel-head-tools:empty {
  display: none;
}

/* 아이콘 버튼. 손가락으로 누를 34px 을 확보하되 아이콘은 18px 로 둔다. */
.view-pick {
  display: inline-grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.view-pick svg {
  width: 18px;
  height: 18px;
  display: block;
  fill: currentColor;
}

.view-pick.is-active {
  background: var(--accent-soft);
  color: var(--accent-strong);
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
  position: relative;
  color: var(--video);
  font-size: 20px;
  line-height: 1;
}

/* 썸네일로 쓰는 <video>. controls 가 없으므로 버튼 안에 들어가도 된다 —
   controls 를 붙이면 상호작용 요소가 되어 버튼 중첩이 된다. */
.entry-thumb-media {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  background: #000;
}

/* 첫 프레임 위에 얹는 재생 표시. 프레임이 밝으면 ▶ 가 묻혀서 어둡게 깐다. */
.entry-thumb-video .entry-play {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #fff;
  font-size: 17px;
  background: rgba(0, 0, 0, .22);
  text-shadow: 0 1px 4px rgba(0, 0, 0, .6);
}

/* 프레임을 못 받은 영상(공유 링크, 또는 실패)은 원래 모습으로 둔다. */
.entry-thumb-video:not(:has(.entry-thumb-media)) .entry-play {
  position: static;
  background: none;
  color: var(--video);
  text-shadow: none;
}

.badge.upload {
  background: var(--accent);
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
/* 영상 그리드. 사진 격자를 그대로 쓰되 칸이 가로로 길다 — 영상은 대부분
   가로이고, 세로로 길게 자르면 무슨 장면인지 안 보인다. */
.video-grid .photo-cell {
  aspect-ratio: 4 / 3;
}

.video-cell .entry-thumb {
  width: 100%;
  height: 100%;
  border: 0;
  border-radius: 0;
}

.video-cell figcaption {
  display: flex;
  align-items: center;
  gap: 6px;
  max-width: calc(100% - 16px);
}

/* 이전/다음. 영상은 제목만으로 구분되는 경우가 많아 몇 번째인지 같이 보인다.
   :has() 로 이 줄만 골라낸다 — 아래 "새 창에서 열기" 줄은 그대로 둬야 한다. */
.video-modal-foot:has(.video-step) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.video-modal-foot .video-step {
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 8px 16px;
  background: var(--surface);
  color: var(--ink);
  font-size: 14px;
  cursor: pointer;
}

.video-modal-foot .video-step:disabled {
  opacity: .4;
  cursor: default;
}

.video-step-count {
  color: var(--muted);
  font-size: 13px;
}

/* 목록 행과 영상 상세의 좋아요. `.like-button` 은 기본이 absolute(격자 칸
   모서리에 붙는 용도)라, 줄 안에 세우려면 흐름으로 되돌려야 한다. */
.entry-like .like-button,
.video-like .like-button {
  position: static;
}

.entry-row .entry-like {
  display: inline-flex;
  flex: none;
  margin-right: 4px;
}

.video-step-mid {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

/* ---------- 업로드 ---------- */

/* 폰에서 올리는 게 기본이라 손가락으로 누를 크기를 먼저 맞춘다. */
.upload-bar {
  display: flex;
  gap: 8px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}

.upload-pick {
  flex: 1;
  min-height: 46px;
  border: 1px dashed var(--accent);
  border-radius: 10px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-size: 15px;
  font-weight: 700;
  cursor: pointer;
}

.upload-pick:disabled {
  opacity: .55;
  cursor: default;
}

/* 진행 상황은 화면 아래에 고정한다. 목록 위쪽에 두면 사진을 고르고 스크롤한
   뒤에는 보이지 않아서, 올라가는 중인지 멈춘 건지 알 수 없다. */
.upload-sheet {
  position: fixed;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%);
  width: min(420px, calc(100% - 24px));
  z-index: 60;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: 0 10px 30px rgba(25, 32, 28, .22);
  padding: 12px 14px;
}

.upload-title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 700;
  color: var(--ink);
}

.upload-track {
  height: 6px;
  border-radius: 999px;
  background: var(--surface-soft);
  overflow: hidden;
}

.upload-fill {
  display: block;
  height: 100%;
  width: 0;
  border-radius: 999px;
  background: var(--accent);
  transition: width .2s ease;
}

.upload-note {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--muted);
}

.upload-note.is-error {
  color: var(--danger);
}

/* 보관본이 아직 없는 사진. 사진은 보이지만 원본이 MYBOX 에 없다. */
.photo-cell.is-partial::after {
  content: "원본 없음";
  position: absolute;
  left: 8px;
  top: 8px;
  padding: 3px 7px;
  border-radius: 999px;
  background: rgba(16, 18, 17, .66);
  color: #fff;
  font-size: 11px;
}

/* 삭제 버튼은 관리자 세션일 때만 나타난다. 가족이 보는 평소 화면에는
   아예 없다. 막는 것은 서버의 비번이고, 이 클래스는 보이고 안 보이고만
   정한다. */
.admin-delete {
  display: none;
}

body.is-admin .admin-delete {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  width: 34px;
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}

body.is-admin .admin-delete:hover {
  border-color: var(--danger);
  background: var(--surface-soft);
}

/* 격자 칸 안에서는 흐름에 끼면 안 된다 — 사진 아래로 밀려나 칸이 늘어진다.
   좋아요가 오른쪽 아래를 쓰므로 오른쪽 위에 둔다. */
body.is-admin .photo-cell .admin-delete {
  position: absolute;
  right: 8px;
  top: 8px;
  width: 32px;
  height: 32px;
  border-color: rgba(255, 255, 255, .3);
  background: rgba(16, 18, 17, .62);
}

body.is-admin .photo-cell .admin-delete:hover {
  border-color: var(--danger);
  background: rgba(16, 18, 17, .82);
}

.entry-row {
  display: flex;
  align-items: center;
  border-bottom: 1px solid var(--line);
}

.entry-row .entry {
  border-bottom: 0;
}

body.is-admin .entry-row .admin-delete {
  margin-right: 12px;
}

.comment {
  position: relative;
}

body.is-admin .comment .admin-delete {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 28px;
  height: 28px;
  font-size: 12px;
}

/* 관리자 모드라는 걸 본인은 알아야 한다. 숨긴 것은 진입점이지 상태가
   아니다. */
.admin-banner {
  position: fixed;
  left: 50%;
  bottom: 14px;
  transform: translateX(-50%);
  z-index: 40;
  padding: 6px 14px;
  border-radius: 999px;
  background: var(--accent-strong);
  color: #fff;
  font-size: 12px;
  font-weight: 650;
  box-shadow: var(--shadow);
  pointer-events: none;
}

.admin-session {
  padding: 16px 18px 0;
}

.admin-session p {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--muted);
}

.admin-exit {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 10px;
  background: var(--surface);
  color: var(--danger);
  font-size: 14px;
  font-weight: 650;
  cursor: pointer;
}

.admin-exit:hover {
  border-color: var(--danger);
}

/* 관리자 진입은 눈에 보이지 않는다. 가족이 매일 보는 화면에 쓸 일 없는
   버튼을 두지 않으려는 것이고, 비밀번호는 어차피 서버가 확인하므로
   숨김 자체가 보안은 아니다.

   aria-hidden + tabindex=-1 로 스크린리더와 탭 이동에서도 빼 둔다. 그래서
   키보드만으로는 열 수 없다 — 필요해지면 키 조합을 따로 붙여야 한다. */
.admin-hotspot {
  padding: 0;
  border: 0;
  background: transparent;
  -webkit-tap-highlight-color: transparent;
  cursor: default;
}

/* PC: 사이드바 하단(동기화 시각 줄) 전체가 눌림 */
.admin-hotspot-desktop {
  position: absolute;
  inset: 0;
}

.sidebar-footer {
  position: relative;
}

.admin-hotspot-mobile {
  display: none;
}

@media (max-width: 860px) {
  /* 모바일: 사이드바 푸터가 숨겨지므로 화면 우상단 모서리를 쓴다.
     상단 바 위에 떠 있어야 해서 z-index 를 사이드바보다 높게 잡는다. */
  .admin-hotspot-desktop {
    display: none;
  }

  .admin-hotspot-mobile {
    display: block;
    position: fixed;
    top: 0;
    right: 0;
    width: 56px;
    height: 56px;
    z-index: 30;
  }
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
  display: flex;
  flex-direction: column;
  width: min(420px, 100%);
  /* 100vh 는 모바일 브라우저 주소창 높이를 빼 주지 않는다. dvh 를 쓰면
     주소창이 접히든 펼쳐지든 실제로 보이는 높이에 맞는다. */
  max-height: calc(100dvh - 32px);
  background: var(--surface);
  border-radius: 16px;
  box-shadow: var(--shadow);
  overflow: hidden;
}

/* 제목과 메시지는 고정이고 폼만 스크롤한다. 비번 변경 칸을 펼치면 내용이
   화면보다 길어지는데, 통째로 스크롤시키면 제목이 위로 밀려 잘리고
   결과 메시지는 아래로 밀려 안 보인다 — 바꿨는지 아닌지 알 수가 없게 된다. */
.admin-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.admin-head {
  flex: none;
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
  flex: none;
  margin: 0;
  padding: 12px 18px 16px;
  min-height: 18px;
  font-size: 13px;
  color: var(--muted);
}

/* 할 말이 있을 때만 선을 긋는다. 빈 줄에 경계선만 떠 있으면 어색하다. */
.admin-message:not(:empty) {
  border-top: 1px solid var(--line);
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

.video-modal-frame video {
  width: 100%;
  height: 100%;
  display: block;
  background: #000;
}

.video-loading {
  margin: 0;
  display: grid;
  place-items: center;
  height: 100%;
  color: rgba(255, 255, 255, .8);
  font-size: 14px;
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
  .admin-modal {
    padding: 0;
  }

  .admin-panel {
    width: 100%;
    height: 100dvh;
    max-height: 100dvh;
    border-radius: 0;
  }

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

/* 사진이 화면을 통째로 쓴다. 예전에는 좌우 64px, 위 54px 을 버튼 자리로
   비워 두고 그 가운데에만 사진을 넣었는데, 세로 사진에서 특히 작게 보였다.
   이제 버튼은 자리를 차지하지 않고 사진 위에 뜬다. */
.lightbox {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: rgba(10, 12, 11, .92);
}

.lightbox-frame {
  position: absolute;
  inset: 0;
  margin: 0;
  display: block;
}

.lightbox-image-wrap {
  position: absolute;
  inset: 0;
  /* 가로 손짓은 우리가 쓴다(사진 넘기기). 이게 없으면 브라우저가 뒤로가기
     제스처로 가져가는 기기가 있다. 확대 중에는 아래의 is-zoomed 가 none 으로
     덮어써서 사진을 끌어 옮길 수 있다. */
  /* 핀치 확대도 우리가 받는다 — pinch-zoom 을 브라우저에 넘기면 화면 전체가
     커져서 버튼까지 따라 커지고, 그동안 우리 확대율은 1 인 채라 넘기기가
     살아 있다. 두 손가락은 touchmove 에서 가로채 lightboxZoom 을 움직인다. */
  touch-action: pan-y;
  overflow: auto;
  overscroll-behavior: contain;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 넘기는 동안 사진이 틀 밖으로 나간다. overflow: auto 인 채로는 그게
   스크롤 영역으로 잡혀 가로 스크롤바가 깜빡인다. */
.lightbox-image-wrap.is-sliding {
  overflow: hidden;
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

/* 캡션과 좋아요가 사진 아래 가장자리에 함께 뜬다. 밝은 사진 위에서도
   글자가 읽혀야 하므로 반투명 판을 깔았다. */
.lightbox-caption-bar {
  /* left:50% + translateX 로 가운데를 맞추면 가용 폭이 "컨테이너 - left",
     즉 절반으로 깎인다. 390px 화면에서 캡션이 두 줄로 쪼개졌다. 양쪽을 다
     묶고 margin auto 로 가운데를 맞춰야 폭이 온전히 남는다. */
  left: 16px;
  right: 16px;
  bottom: 16px;
  width: max-content;
  margin: 0 auto;
  max-width: calc(100% - 32px);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px 6px 14px;
  border-radius: 999px;
  background: rgba(10, 12, 11, .55);
  backdrop-filter: blur(6px);
  color: #fff;
  font-size: 14px;
  text-align: center;
}

/* 모아보기 타일에서는 하트가 사진 모서리에 절대위치로 붙지만, 여기서는
   캡션 옆에 줄 세워야 한다. */
.lightbox-like .like-button {
  position: static;
  background: rgba(255, 255, 255, .16);
}

.lightbox-like .like-button:hover {
  background: rgba(255, 255, 255, .28);
}

.lightbox button {
  border: 0;
  color: #fff;
  /* 밝은 사진 위에 떠 있으므로 밝은 반투명으로는 안 보인다. 어둡게 깔고
     뒤를 흐린다. */
  background: rgba(10, 12, 11, .55);
  backdrop-filter: blur(6px);
  border-radius: 8px;
}

/* 버튼 묶음은 사진 위에 뜨고, 평소에는 흐릿하다가 가까이 가면 또렷해진다.
   터치에는 hover 가 없으므로 기본값도 누를 수 있을 만큼은 보여야 한다. */
.lightbox-top-actions,
.lightbox-toolbar,
.lightbox-nav,
.lightbox-caption-bar {
  position: absolute;
  z-index: 2;
  opacity: .5;
  transition: opacity .15s ease;
}

.lightbox-top-actions:hover,
.lightbox-toolbar:hover,
.lightbox-nav:hover,
.lightbox-caption-bar:hover,
.lightbox-top-actions:focus-within,
.lightbox-toolbar:focus-within,
.lightbox-caption-bar:focus-within,
.lightbox-nav:focus-visible {
  opacity: 1;
}

.lightbox button:disabled {
  cursor: default;
  opacity: .38;
}

.lightbox-top-actions {
  top: 12px;
  right: 12px;
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
  /* 캡션 바와 같은 이유로 양쪽을 묶는다. */
  top: 12px;
  left: 12px;
  right: 12px;
  width: max-content;
  margin: 0 auto;
  display: flex;
  gap: 6px;
  padding: 4px;
  border-radius: 8px;
  background: rgba(10, 12, 11, .45);
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
  top: 50%;
  transform: translateY(-50%);
  width: 48px;
  height: 64px;
  font-size: 38px;
}

.lightbox-prev {
  left: 12px;
}

.lightbox-next {
  right: 12px;
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

  /* 가로 4열 그리드라 머리글이 한 칸만 차지하면 탭 사이에 끼어 버린다.
     줄을 통째로 쓰게 하고, 메뉴가 세로로 길어지지 않게 얇게 둔다. */
  .tab-group {
    grid-column: 1 / -1;
    margin: 6px 0 0;
    padding: 0 2px;
    font-size: 10px;
  }

  /* 가로 배열에서는 위쪽 선이 한 칸에만 그어져 어색하다. 세로 선으로 끊는다. */
  .tab.is-standalone {
    margin-top: 0;
    padding-top: 6px;
    border-top: 0;
    border-left: 1px solid var(--line);
    border-radius: 7px;
    padding-left: 10px;
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

  .lightbox-nav {
    width: 40px;
    height: 56px;
    font-size: 32px;
  }

  .lightbox-prev {
    left: 6px;
  }

  .lightbox-next {
    right: 6px;
  }

  .lightbox-caption-bar {
    bottom: 12px;
    font-size: 13px;
  }

  .lightbox-toolbar button {
    min-width: 38px;
    height: 34px;
    padding: 0 10px;
  }
}

@media (max-width: 430px) {
  /* 가운데 확대 컨트롤과 우상단 버튼이 한 줄에 같이 뜬다. 390px 에서 둘
     사이가 13px 밖에 안 남아서, 더 좁아지면 겹친다. */
  .lightbox-toolbar {
    gap: 2px;
    padding: 3px;
  }

  .lightbox-toolbar button {
    min-width: 32px;
    padding: 0 6px;
  }

  .lightbox-toolbar .lightbox-zoom-reset {
    min-width: 52px;
    font-size: 13px;
  }

  .lightbox-top-actions button {
    width: 38px;
    height: 38px;
  }

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
  // 자녀 설정.
  //
  // `key` 는 절대 바꾸지 말 것. D1 의 videos / comments / likes 가 이 값으로
  // 행을 묶는다. 둘째의 key 가 이름이 아니라 "second" 인 이유가 이것이다 —
  // 지금 부르는 "살구"는 태명이라 출생 후 바뀐다. 이름이 바뀌면 아래 `name`
  // 과 `roman` 만 고치면 되고, 저장된 데이터는 그대로 따라온다.
  //
  // 프로필 아래 줄은 `birth` 와 `lmp` 중 있는 쪽을 쓴다. `birth` 가 이긴다 —
  // 태어나고 나면 주수가 아니라 D+ 를 봐야 하므로, 출생일을 넣는 순간 저절로
  // 넘어간다. `lmp` 는 그때 지워도 되고 남겨 둬도 된다.
  //
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
      // 임신 0주 0일로 잡은 날. 2026-10-21 이 12주 0일이라는 것만 알고 84일을
      // 거꾸로 센 값이다. 병원에서 주수를 다시 잡아 주면 이 날짜만 옮기면
      // 되고, 출산예정일도 여기서 따라 나온다(+280일 = 2027-05-05).
      lmp: "2026-07-29",
      // 주수가 바뀌면 renderProfile 이 fetus-1..9 중에서 골라 덮어쓴다.
      // 여기 값은 주수를 못 셀 때(기준일이 미래일 때)만 쓰인다.
      photo: "/assets/fetus-1.png",
      illustration: true,
    },
  ];
  const DEFAULT_CHILD = "seoi";

  // 탭은 출처로 묶는다. 키즈노트가 보내 준 것(배치가 매일 다시 만든다)과
  // 우리가 직접 올린 것(D1 에 남는다)은 성격이 다르다 — 섞여 있으면 "이 사진이
  // 어디서 온 건지"를 화면만 보고는 알 수 없다. 모아보기는 그 둘을 한 화면에
  // 모으는 자리라 그룹 밖 맨 아래에 둔다.
  //
  // `key` 는 URL(?type=)과 저장된 상태가 쓰므로 바꾸지 말 것. 라벨만 바꾼다 —
  // `photos` 의 라벨이 "사진"에서 "모아보기"로 바뀐 것이 그 예다.
  const tabs = [
    { key: "home", label: "홈", icon: "/assets/home.svg" },
    { key: "daily", label: "알림장", icon: "/assets/daily.svg", group: "키즈노트" },
    { key: "album", label: "앨범", icon: "/assets/album.svg", group: "키즈노트" },
    { key: "announcement", label: "공지", icon: "/assets/announcement.svg", group: "키즈노트" },
    { key: "video", label: "영상", icon: "/assets/video.svg", group: "업로드" },
    { key: "uploads", label: "사진", icon: "/assets/upload.svg", group: "업로드" },
    // 모아보기는 양쪽 사진을 한 화면에 모으는 자리라 어느 그룹에도 안 들어간다.
    { key: "photos", label: "모아보기", icon: "/assets/photos.svg" },
  ];
  // Videos are not Notion posts: no detail route, no NEW pill, no month
  // filter. The tab reuses the list panel but takes its items from a
  // separate manifest, so every list path branches on this.
  const VIDEO_TYPE = "video";
  // 사진 모아보기. 글 목록이 아니라 사진 단위라 목록 경로가 또 갈린다.
  const PHOTOS_TYPE = "photos";
  const UPLOADS_TYPE = "uploads";
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
      // 올린 사진·영상에는 글이 없다. String(undefined) 를 그대로 쓰면
      // "undefined" 가 좋아요 행에 적힌다.
      + ` data-post-id="${escapeHtml(String(postId || ""))}" aria-label="좋아요">`
      + `${THUMB_ICON}<span class="like-count">${count || ""}</span></button>`
    );
  }
  const LIGHTBOX_MIN_ZOOM = 1;
  const LIGHTBOX_MAX_ZOOM = 4;
  const LIGHTBOX_ZOOM_STEP = 0.5;

  // 영상은 주소를 가진다. 팝업은 폰의 뒤로가기(화면 가장자리 쓸기)에
  // 반응하지 않아서, 영상을 보다가 목록으로 돌아올 길이 닫기 버튼뿐이었다.
  //
  // `/posts/123/` 과 달리 실제 파일을 만들지 않는다 — 영상 목록은 D1 에 있어
  // export 시점에 뭐가 있는지 모른다. Pages 가 없는 경로에 index.html 을
  // 돌려주므로(확인함) 주소를 직접 열어도 동작한다.
  function videoIdFromPath(pathname) {
    const match = String(pathname || "").match(/^\/videos\/([A-Za-z0-9_-]+)\/?$/);
    return match ? match[1] : null;
  }

  // 사진도 주소를 가진다. 영상과 같은 이유다 — 폰에서 뒤로가기(화면 가장자리
  // 쓸기)로 라이트박스를 닫을 수 있어야 하고, 사진 한 장을 그대로 보낼 수
  // 있어야 한다.
  //
  // 영상과 달리 사진에는 id 가 없고 경로가 곧 이름이다. 경로를 그대로 주소에
  // 넣으면 슬래시가 섞여 읽기 어렵다. FNV-1a 를 씨앗 둘로 돌려 64비트짜리
  // 짧은 이름을 만든다 — 7천 장에서 겹칠 일은 사실상 없다.
  function photoIdFromPath(pathname) {
    const match = String(pathname || "").match(/^\/photos\/([a-f0-9]{16})\/?$/);
    return match ? match[1] : null;
  }

  function photoRouteId(src) {
    return shuffleKey(0, src).toString(16).padStart(8, "0")
      + shuffleKey(0x9e3779b9, src).toString(16).padStart(8, "0");
  }

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
    uploads: [],
    uploadsLoaded: false,
    photoLikes: {},   // photo_id → 누른 횟수
    photoSort: window.localStorage.getItem("kidsnote.photoSort") || "date",
    // 영상 보기 방식. 목록은 제목이 길어도 읽히고, 그리드는 썸네일이 커서
    // 무슨 장면인지 고르기 좋다. 폰에서는 그리드가 한 화면에 더 들어온다.
    videoView: window.localStorage.getItem("kidsnote.videoView") || "list",
    photoLimit: PHOTO_PAGE,
    // 관리자 세션. 글 상세는 실제 페이지 이동이라 sessionStorage 를 쓴다 —
    // 안 그러면 글 하나 열 때마다 비번을 다시 넣어야 한다. 탭을 닫으면
    // 사라진다.
    adminPasscode: window.sessionStorage.getItem("kidsnote.admin") || "",
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
    lightboxPinch: null,
  };

  const tabsNode = document.getElementById("tabs");
  const entryList = document.getElementById("entryList");
  const detail = document.getElementById("detail");
  const detailPanel = document.getElementById("detailPanel");
  const listTitle = document.getElementById("listTitle");
  const listCount = document.getElementById("listCount");
  const listTools = document.getElementById("listTools");
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
  const lightboxLike = document.getElementById("lightboxLike");
  const uploadInput = document.getElementById("uploadInput");
  const uploadSheet = document.getElementById("uploadSheet");
  const uploadTitle = document.getElementById("uploadTitle");
  const uploadFill = document.getElementById("uploadFill");
  const uploadNote = document.getElementById("uploadNote");
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
  const adminHotspots = [
    document.getElementById("adminHotspotDesktop"),
    document.getElementById("adminHotspotMobile"),
  ];
  const adminModal = document.getElementById("adminModal");
  const adminClose = document.querySelector(".admin-close");
  const adminLoginForm = document.getElementById("adminLoginForm");
  const adminPasscodeInput = document.getElementById("adminPasscode");
  const adminSessionBox = document.getElementById("adminSessionBox");
  const adminExit = document.getElementById("adminExit");
  const adminBanner = document.getElementById("adminBanner");
  const adminMessage = document.getElementById("adminMessage");
  const adminMyboxForm = document.getElementById("adminMyboxForm");
  const adminMyboxToggle = document.getElementById("adminMyboxToggle");
  const adminMyboxFields = document.getElementById("adminMyboxFields");
  const myboxChild = document.getElementById("myboxChild");
  const myboxFolder = document.getElementById("myboxFolder");
  const adminPasscodeForm = document.getElementById("adminPasscodeForm");
  const adminPasscodeToggle = document.getElementById("adminPasscodeToggle");
  const adminPasscodeFields = document.getElementById("adminPasscodeFields");
  const currentPasscode = document.getElementById("currentPasscode");
  const newPasscode = document.getElementById("newPasscode");
  const newPasscodeConfirm = document.getElementById("newPasscodeConfirm");
  const videoModal = document.getElementById("videoModal");
  const videoModalTitle = document.getElementById("videoModalTitle");
  const videoModalFrame = document.getElementById("videoModalFrame");
  const videoModalLink = document.getElementById("videoModalLink");
  const videoModalClose = document.querySelector(".video-modal-close");
  const videoPrev = document.getElementById("videoPrev");
  const videoNext = document.getElementById("videoNext");
  const videoStepCount = document.getElementById("videoStepCount");
  const videoLike = document.getElementById("videoLike");
  const appShell = document.getElementById("appShell");
  const profilePhoto = document.getElementById("profilePhoto");
  const babyDays = document.getElementById("babyDays");
  const pageEyebrow = document.getElementById("pageEyebrow");
  const pageTitle = document.getElementById("pageTitle");

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

  // 보는 사람의 시계가 어디에 있든 서울 날짜로 센다. 시각은 버리고 날짜만
  // 남겨 UTC 자정으로 돌려주므로, 빼면 바로 일수가 된다.
  function seoulToday() {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(new Date()).reduce((acc, part) => {
      acc[part.type] = Number(part.value);
      return acc;
    }, {});
    return Date.UTC(parts.year, parts.month - 1, parts.day);
  }

  function daysSince(isoDate) {
    const [year, month, day] = isoDate.split("-").map(Number);
    return Math.round((seoulToday() - Date.UTC(year, month - 1, day)) / 86400000);
  }

  const daysSinceBirth = (birth) => daysSince(birth) + 1;

  // 임신 주수. 이걸 export 시점에 박아 두면 배치가 하루 걸른 날 주차가
  // 멈추므로, 화면을 열 때마다 브라우저가 다시 센다.
  function gestation(lmp) {
    const days = daysSince(lmp);
    if (days < 0) return null;
    return { weeks: Math.floor(days / 7), days: days % 7 };
  }

  // 태아 그림 9장의 달 경계(주차). 달력 기준 9개월이라 간격이 고르지 않아서
  // 식으로 꾸미면 22주처럼 경계에 걸친 주가 한 달씩 밀린다. 표를 그대로 적는
  // 편이 눈으로 확인하기 쉽다.
  const FETAL_MONTH_WEEKS = [0, 5, 9, 14, 18, 23, 28, 32, 36];

  function fetalImage(weeks) {
    let month = 1;
    FETAL_MONTH_WEEKS.forEach((start, index) => {
      if (weeks >= start) month = index + 1;
    });
    return `/assets/fetus-${month}.png`;
  }

  // 출산예정일은 통상 lmp + 280일이다. 따로 적어 두면 주수와 어긋날 수
  // 있으니 기준일 하나에서 뽑는다.
  function dueDate(lmp) {
    const [year, month, day] = lmp.split("-").map(Number);
    return new Date(Date.UTC(year, month - 1, day + 280)).toISOString().slice(0, 10);
  }

  function renderProfile() {
    const child = activeChild();
    brandName.textContent = child.name;
    brandRoman.textContent = child.roman;
    profileName.textContent = child.name;

    // 그림과 아래 줄이 같은 계산을 써야 "12주"인데 4개월 그림이 뜨는 일이
    // 없다. 그래서 주수는 여기서 한 번만 센다.
    const term = !child.birth && child.lmp ? gestation(child.lmp) : null;
    const photo = term ? fetalImage(term.weeks) : child.photo;

    if (photo) {
      profileImage.src = photo;
      profileImage.alt = child.name;
      profilePhoto.hidden = false;
      profilePhoto.setAttribute("aria-label", `${child.name} 사진 크게 보기`);
      profilePhoto.classList.remove("is-placeholder");
      profilePhoto.classList.toggle("is-illustration", Boolean(child.illustration));
      profilePhoto.textContent = "";
      profilePhoto.replaceChildren(profileImage);
    } else {
      // 사진이 아직 없는 자녀는 이름 첫 글자를 쓴다. 라이트박스로 열 것도
      // 없으므로 버튼이지만 비활성으로 둔다.
      profilePhoto.hidden = false;
      profilePhoto.classList.add("is-placeholder");
      profilePhoto.classList.remove("is-illustration");
      profilePhoto.removeAttribute("aria-label");
      profilePhoto.replaceChildren(document.createTextNode(child.name.slice(0, 1)));
    }

    if (child.birth) {
      const days = daysSinceBirth(child.birth);
      profileMeta.innerHTML =
        `<time datetime="${escapeHtml(child.birth)}">${escapeHtml(child.birth.replaceAll("-", "."))}</time>`
        + '<span aria-hidden="true">·</span>'
        + `<strong>D+${Math.max(1, days)}</strong>`;
    } else if (term) {
      const due = dueDate(child.lmp);
      profileMeta.innerHTML =
        `<time datetime="${escapeHtml(due)}">${escapeHtml(due.replaceAll("-", "."))} 예정</time>`
        + '<span aria-hidden="true">·</span>'
        + `<strong>${term.weeks}주 ${term.days}일</strong>`;
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

  // 영상은 글과 달리 달력 날짜가 없다. 초음파 제목이 "37주차 0일"이라
  // video_date 가 비어 있고, 가진 시간 정보는 **등록 시각**뿐이다. 그래서
  // "새로 찍은 영상"이 아니라 "새로 올린 영상"을 뜻한다 — 폴더에 넣고
  // 동기화한 날 뜬다.
  function isNewVideo(video) {
    if (!video.created_at) return false;
    return state.recentDateKeys.has(koreaDateKey(new Date(video.created_at)));
  }

  const newVideos = () => childVideos().filter(isNewVideo);

  // 홈에 들어갈 것들.
  //
  // **키즈노트 사진**은 넣지 않는다 — 그 사진이 실린 글이 이미 홈에 줄로
  // 서 있어서, 사진까지 세우면 같은 소식이 두 번 보인다.
  //
  // **직접 올린 사진**은 넣는다. 딸린 글이 없어서 안 넣으면 홈에서 아예
  // 안 보이고, 방금 올린 것이 "최신 업데이트"에 없는 게 된다.
  function homeItems() {
    return [...newVideos(), ...newUploads(), ...childPosts().filter(isNewPost)];
  }

  // 사진은 자기 날짜를 들고 있다(글에서 물려받는다). 같은 날 알림장 2건에
  // 사진 12장이면 알림장 배지는 2, 사진 배지는 12다 — 세는 단위가 다를 뿐
  // 둘 다 맞는 숫자다.
  const newPhotos = () => childPhotos().filter(
    (photo) => state.recentDateKeys.has(String(photo.date || "")),
  );

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
    let shownGroup = null;
    tabsNode.innerHTML = tabs.map((tab) => {
      // 그룹이 바뀔 때만 머리글을 낸다. 탭마다 달면 같은 말이 반복된다.
      const heading = tab.group && tab.group !== shownGroup
        ? `<p class="tab-group">${escapeHtml(tab.group)}</p>`
        : "";
      // 그룹 뒤에 그룹 없는 탭이 오면(모아보기) 앞 그룹에 딸린 것처럼 보인다.
      // 머리글이 없으니 선으로 끊어 준다.
      const standalone = !tab.group && shownGroup ? " is-standalone" : "";
      shownGroup = tab.group || null;

      const count = tab.key === "home"
        ? homeItems().length
        : tab.key === VIDEO_TYPE
          ? childVideos().length
          : tab.key === UPLOADS_TYPE
            ? childUploads().length
            : tab.key === PHOTOS_TYPE
              ? galleryItems().length
              : state.counts[tab.key] || 0;
      // 홈은 그 자체가 새 항목 목록이라 빨간 배지를 또 달지 않는다.
      const newCount = tab.key === "home"
        ? 0
        : tab.key === VIDEO_TYPE
          ? newVideos().length
          : tab.key === UPLOADS_TYPE
            ? newUploads().length
            : tab.key === PHOTOS_TYPE
              ? newPhotos().length
              : state.newCounts[tab.key] || 0;
      const content = (
        `<span class="tab-icon" aria-hidden="true"><img src="${tab.icon}" alt=""></span>`
        + `<span class="tab-label">${tab.label}</span>`
        + `<span class="tab-meta"><span class="tab-count">${count}</span>`
        + `${newCount ? `<span class="tab-new-count" aria-label="최근 업데이트 ${newCount}개">${newCount}</span>` : ""}</span>`
      );
      if (detailRoute) {
        const current = tab.key === state.activeType ? ' aria-current="page"' : "";
        return `${heading}<a class="tab${standalone}" href="/?type=${tab.key}"${current}>${content}</a>`;
      }
      return (
        heading
        + `<button class="tab${standalone}" type="button" data-type="${tab.key}" aria-selected="${tab.key === state.activeType ? "true" : "false"}">`
        + `${content}</button>`
      );
    }).join("");
  }

  function childVideos() {
    return state.videos.filter((video) => (video.child || DEFAULT_CHILD) === state.activeChild);
  }

  function childUploads() {
    return state.uploads.filter((item) => (item.child || DEFAULT_CHILD) === state.activeChild);
  }

  // 올린 사진도 영상과 같은 기준이다 — 찍은 날이 아니라 **올린 날**이 새것을
  // 가른다. 몇 년 전 사진을 오늘 올렸으면 오늘 새로 온 소식이 맞다.
  const newUploads = () => childUploads().filter(isNewUploadItem);

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
    if (state.activeType === VIDEO_TYPE || state.activeType === PHOTOS_TYPE
        || state.activeType === UPLOADS_TYPE) {
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

  // 썸네일은 영상 자체의 첫 프레임이다. MYBOX 영상은 같은 출처(/stream)를
  // 거치므로 <video> 가 그려 주고, 따로 저장할 그림이 없다.
  //
  // 몇 초 지점을 집을지는 받아 보고 정한다. 고정값을 쓰면 짧은 영상에서는
  // 끝을 넘고, 초음파처럼 도입부가 어두운 영상에서는 검은 네모가 나온다.
  function thumbOf(video) {
    if (video.source !== "mybox") {
      return '<span class="entry-thumb entry-thumb-video"><span class="entry-play"'
        + ' aria-hidden="true">▶</span></span>';
    }
    const stream = `/api/videos/${encodeURIComponent(video.id)}/stream`;
    return '<span class="entry-thumb entry-thumb-video">'
      + `<video class="entry-thumb-media" data-stream="${escapeHtml(stream)}"`
      + ' preload="none" muted playsinline tabindex="-1" aria-hidden="true"></video>'
      + '<span class="entry-play" aria-hidden="true">▶</span>'
      + "</span>";
  }

  // 목록에 보이는 것만 받아 온다. 썸네일 하나가 MYBOX 다운로드 URL 발급을
  // 한 번씩 부르므로, 폴더가 커지면 화면을 열자마자 수십 번을 치게 된다.
  // 초음파는 탐촉자를 대고 화면을 잡기까지가 검은 화면이고, 그 길이가 영상마다
  // 다르다. 고정 지점을 쓰면 어떤 값을 골라도 몇 개는 검은 네모로 남는다.
  // 그래서 받아 본 뒤 비었으면 다음 지점으로 넘긴다.
  const THUMB_SEEK_STEPS = [6, 20, 45];

  // 프레임이 비었는지 본다. 지표를 두 번 갈아탄 자리다.
  //
  //   평균 밝기 — 못 쓴다. 초음파는 원래 어두워서 빈 프레임 4, 멀쩡한 화면
  //               8~19 로 거의 붙어 있다.
  //   최대 밝기 — 못 쓴다. 측정 마커가 흰색이라 **모든** 프레임이 255 다.
  //   밝은 픽셀 비율 — 쓴다. 빈 프레임 2.9%, 멀쩡한 화면 5.3~47% 로 갈린다.
  //
  // 실측값이라 영상이 바뀌면 다시 재야 한다.
  const BLANK_BRIGHT_RATIO = 0.04;
  const BLANK_BRIGHT_LEVEL = 45;

  function frameIsBlank(media) {
    const canvas = frameIsBlank.canvas
      || (frameIsBlank.canvas = document.createElement("canvas"));
    canvas.width = 32;
    canvas.height = 32;
    try {
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      ctx.clearRect(0, 0, 32, 32);
      ctx.drawImage(media, 0, 0, 32, 32);
      const data = ctx.getImageData(0, 0, 32, 32).data;
      let bright = 0;
      for (let i = 0; i < data.length; i += 4) {
        const level = (data[i] + data[i + 1] + data[i + 2]) / 3;
        if (level > BLANK_BRIGHT_LEVEL) bright += 1;
      }
      return bright / (data.length / 4) < BLANK_BRIGHT_RATIO;
    } catch {
      // 캔버스를 못 읽으면 판단을 포기한다. 멀쩡한 프레임을 지우는 것보다
      // 어두운 프레임을 두는 쪽이 낫다.
      return false;
    }
  }

  function loadThumb(media) {
    // preload 를 먼저 올려야 한다. "none" 인 채로 src 만 넣으면 브라우저가
    // 아무것도 받지 않아서 첫 프레임이 영영 안 그려진다.
    media.preload = "metadata";

    // 탐색은 그 위치만 범위 요청으로 받으므로, 뒤로 가도 처음부터 받지 않는다.
    let step = 0;
    const seekOn = () => {
      if (step >= THUMB_SEEK_STEPS.length || !Number.isFinite(media.duration)) return;
      const at = Math.min(THUMB_SEEK_STEPS[step], media.duration * 0.9);
      step += 1;
      // 짧은 영상은 두 지점이 같은 곳으로 눌린다. 그대로 넣으면 seeked 가
      // 안 울려서 조용히 멈춘다.
      if (Math.abs(at - media.currentTime) < 0.05) {
        seekOn();
        return;
      }
      media.currentTime = at;
    };

    media.addEventListener("loadedmetadata", seekOn, { once: true });
    media.addEventListener("seeked", () => {
      if (frameIsBlank(media)) seekOn();
    });
    media.src = media.dataset.stream;
    // 실패하면 흔적을 지운다. 깨진 <video> 를 두면 검은 네모가 남아서
    // 썸네일이 있는 척하게 된다. 코덱을 못 여는 브라우저도 여기로 온다.
    media.addEventListener("error", () => media.remove(), { once: true });
  }

  const thumbLoader = "IntersectionObserver" in window
    ? new IntersectionObserver((entries, observer) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          observer.unobserve(entry.target);
          loadThumb(entry.target);
        }
      }, { rootMargin: "200px" })
    : null;

  function loadVisibleThumbs() {
    entryList.querySelectorAll(".entry-thumb-media[data-stream]").forEach((media) => {
      if (thumbLoader) thumbLoader.observe(media);
      else loadThumb(media);
    });
  }

  // 알림장·앨범·공지가 쓰는 .entry 행을 그대로 쓴다. .entry 가 이미
  // width/border/background 를 눕혀 둬서 <button> 에 그대로 붙는다.
  // 영상 탭과 홈이 같이 쓴다 — 두 벌로 두면 한쪽만 고치는 일이 생긴다.
  function videoRow(video) {
    const date = video.date ? `<span>${escapeHtml(video.date)}</span>` : "";
    const summary = video.summary
      ? `<span class="entry-summary">${escapeHtml(video.summary)}</span>`
      : "";
    const newMarker = isNewVideo(video) ? '<span class="new-pill">NEW</span>' : "";
    return (
      '<div class="entry-row">'
      + `<button class="entry" type="button" data-video-id="${escapeHtml(video.id)}">`
      + thumbOf(video)
      + "<span>"
      + `<span class="entry-kicker"><span class="badge video">영상</span>${date}${newMarker}</span>`
      + `<span class="entry-title">${escapeHtml(video.title)}</span>${summary}`
      + "</span></button>"
      // 버튼 안에 버튼을 넣을 수 없어서 형제로 둔다. 앨범 타일의 하트와 같은 이유다.
      + `<span class="entry-like">${likeButtonHtml(video.id, "", state.photoLikes[video.id] || 0)}</span>`
      + `<button class="admin-delete" type="button" data-delete-video="${escapeHtml(video.id)}"`
      + ' aria-label="영상 삭제">🗑</button>'
      + "</div>"
    );
  }

  // 목록/그리드 아이콘. 글자로 두면 제목 줄이 좁은 폰에서 제목을 밀어낸다.
  const VIEW_ICON = {
    list: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="4" height="4" rx="1"/>'
      + '<rect x="9" y="6" width="12" height="2" rx="1"/><rect x="3" y="10" width="4" height="4" rx="1"/>'
      + '<rect x="9" y="11" width="12" height="2" rx="1"/><rect x="3" y="15" width="4" height="4" rx="1"/>'
      + '<rect x="9" y="16" width="12" height="2" rx="1"/></svg>',
    grid: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="8" height="8" rx="2"/>'
      + '<rect x="13" y="3" width="8" height="8" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/>'
      + '<rect x="13" y="13" width="8" height="8" rx="2"/></svg>',
  };

  function renderListTools() {
    // 영상 탭에서만 쓴다. 다른 탭에서는 비워야 제목 줄이 원래대로 돌아온다.
    if (state.activeType !== VIDEO_TYPE || state.selectedId !== null) {
      listTools.innerHTML = "";
      return;
    }
    const pick = (key, label) => (
      `<button class="view-pick${state.videoView === key ? " is-active" : ""}" type="button"`
      + ` data-video-view="${key}" aria-pressed="${state.videoView === key}"`
      + ` aria-label="${label}" title="${label}">${VIEW_ICON[key]}</button>`
    );
    listTools.innerHTML = pick("list", "목록으로 보기") + pick("grid", "그리드로 보기");
  }

  // 그리드 칸. 사진 격자와 같은 틀을 쓰되 제목이 사진 위에 얹힌다 —
  // "37주차 0일"처럼 제목이 유일한 구분점이라 안 보이면 고를 수가 없다.
  function videoCell(video) {
    const newMarker = isNewVideo(video) ? '<span class="new-pill">NEW</span>' : "";
    return (
      `<figure class="photo-cell video-cell" data-video-id="${escapeHtml(video.id)}">`
      + thumbOf(video)
      + likeButtonHtml(video.id, "", state.photoLikes[video.id] || 0)
      + `<figcaption>${escapeHtml(video.title)}${newMarker}</figcaption>`
      + `<button class="admin-delete" type="button" data-delete-video="${escapeHtml(video.id)}"`
      + ' aria-label="영상 삭제">🗑</button>'
      + "</figure>"
    );
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
      entryList.innerHTML = uploadBarHtml("video/*", "영상 업로드") + (childVideos().length
        ? '<div class="empty">조건에 맞는 영상이 없습니다.</div>'
        : state.videosLoaded
          ? `<div class="empty">${escapeHtml(activeChild().name)} 영상이 아직 없습니다.</div>`
          : '<div class="error">영상 목록을 불러오지 못했습니다.</div>');
      return;
    }

    entryList.innerHTML = uploadBarHtml("video/*", "영상 업로드")
      + (state.videoView === "grid"
        ? `<div class="photo-grid video-grid">${videos.map(videoCell).join("")}</div>`
        : videos.map(videoRow).join(""));
    loadVisibleThumbs();
  }

  function renderList() {
    renderListTools();
    if (state.activeType === VIDEO_TYPE) {
      renderVideoList();
      return;
    }
    if (state.activeType === PHOTOS_TYPE) {
      renderPhotoList();
      return;
    }
    if (state.activeType === UPLOADS_TYPE) {
      renderUploadList();
      return;
    }
    // 홈에서는 새 영상이 글과 같은 목록에 섞인다. 영상은 페이지가 아니라
    // 모달로 열려서 <a> 가 아니라 <button> 이라, 행을 따로 만들어 앞에 붙인다.
    const homeVideos = state.activeType === "home" ? newVideos() : [];
    const homeUploads = state.activeType === "home" ? newUploads() : [];

    listTitle.textContent = activeListLabel();
    const filters = [state.activeMonth, state.query.trim()].filter(Boolean).length;
    const shown = state.posts.length + homeVideos.length + homeUploads.length;
    listCount.textContent = filters ? `${shown}개 필터됨` : `${shown}개`;
    renderListCollapseState();

    if (!state.posts.length && !homeVideos.length && !homeUploads.length) {
      entryList.innerHTML = state.activeType === "home"
        ? '<div class="empty">최근 등록된 새로운 기록이 없습니다.</div>'
        : '<div class="empty">조건에 맞는 항목이 없습니다.</div>';
      return;
    }

    entryList.innerHTML = homeVideos.map(videoRow).join("")
      + homeUploads.map(uploadRow).join("")
      + state.posts.map((post) => {
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

    if (homeVideos.length) loadVisibleThumbs();
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
        post_id: post.id,
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
      + `<button class="admin-delete" type="button" data-delete-comment="${escapeHtml(String(comment.id))}"`
      + ' aria-label="댓글 삭제">🗑</button>'
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

  // 모바일에서 좌우로 밀어 넘기기. 화살표는 손가락으로 겨누기엔 작고,
  // 사진을 넘기는 손짓은 원래 미는 것이다.
  //
  // 미는 동안 사진이 손가락을 따라간다. 따라오지 않으면 민 것이 먹혔는지,
  // 아직 안 넘어간 건지, 끝이라 안 넘어가는 건지 알 수가 없다.
  //
  // 확대 중에는 끈다 — 그때 같은 손짓은 사진을 끌어 옮기는 동작이고,
  // 둘을 같이 켜면 확대해서 들여다보다가 사진이 넘어가 버린다.
  const SWIPE_MIN_PX = 48;
  const SWIPE_MAX_MS = 700;
  const SWIPE_LOCK_PX = 12;
  const SLIDE_MS = 190;
  let swipeStart = null;
  let swipedAt = 0;
  let slideBusy = false;

  // 사진만 움직인다. 틀을 움직이면 캡션·좋아요까지 같이 끌려간다.
  function setSlideOffset(px, animate) {
    lightboxImage.style.transition = animate
      ? `transform ${SLIDE_MS}ms ease-out, opacity ${SLIDE_MS}ms ease-out`
      : "none";
    lightboxImage.style.transform = px ? `translateX(${Math.round(px)}px)` : "";
    // 멀리 밀수록 옅어진다. 넘어가기 직전이라는 신호다.
    lightboxImage.style.opacity = px ? String(Math.max(0.4, 1 - Math.abs(px) / 900)) : "";
    lightboxImageWrap.classList.toggle("is-sliding", Boolean(px));
  }

  function clearSlideOffset() {
    lightboxImage.style.transition = "";
    lightboxImage.style.transform = "";
    lightboxImage.style.opacity = "";
    lightboxImageWrap.classList.remove("is-sliding");
  }

  // 지금 사진을 민 쪽으로 내보내고, 다음 사진을 반대쪽에서 들여온다.
  // 화살표·키보드도 이걸 쓴다 — 넘어갔다는 신호는 손짓일 때만 필요한 게 아니다.
  function slideLightbox(delta) {
    if (lightbox.hidden || state.lightboxItems.length < 2) return;
    if (slideBusy) return;
    slideBusy = true;
    const width = lightboxImageWrap.clientWidth || 360;
    setSlideOffset(delta > 0 ? -width : width, true);
    window.setTimeout(() => {
      moveLightbox(delta);
      // showLightbox 가 style 을 통째로 지우므로 여기서 다시 건다. 들어오는
      // 쪽은 화면 너비까지 갈 필요가 없다 — 0.4 쯤이면 방향이 읽히고 짧다.
      setSlideOffset(delta > 0 ? width * 0.4 : -width * 0.4, false);
      // 한 프레임 그린 뒤에 0으로 돌려야 브라우저가 중간 상태를 보고
      // 애니메이션을 건다. 같은 프레임에서 바꾸면 그냥 제자리다.
      window.requestAnimationFrame(() => {
        window.requestAnimationFrame(() => {
          setSlideOffset(0, true);
          window.setTimeout(() => {
            clearSlideOffset();
            slideBusy = false;
          }, SLIDE_MS);
        });
      });
    }, SLIDE_MS);
  }

  function startSwipe(event) {
    // canDragLightboxImage() 를 쓰지 않는다. 그건 "확대했고 **실제로 넘친다**"
    // 라서, 작은 사진을 확대하면 넘치지 않아 스와이프가 살아 있다. 확대해 놓고
    // 밀었는데 다음 사진으로 넘어가면 당황스럽다 — 확대 중이면 그냥 끈다.
    if (lightbox.hidden || event.touches.length !== 1 || slideBusy
        || state.lightboxZoom > LIGHTBOX_MIN_ZOOM) {
      swipeStart = null;
      return;
    }
    const touch = event.touches[0];
    swipeStart = { x: touch.clientX, y: touch.clientY, at: Date.now(), locked: false };
  }

  function moveSwipe(event) {
    if (!swipeStart || event.touches.length !== 1) return;
    const touch = event.touches[0];
    const dx = touch.clientX - swipeStart.x;
    const dy = touch.clientY - swipeStart.y;
    if (!swipeStart.locked) {
      // 어느 쪽으로 가려는 손짓인지 정해질 때까지 기다린다.
      if (Math.abs(dx) < SWIPE_LOCK_PX && Math.abs(dy) < SWIPE_LOCK_PX) return;
      if (Math.abs(dy) > Math.abs(dx)) {
        // 세로가 먼저면 스크롤하려는 손짓이다. 더는 보지 않는다.
        swipeStart = null;
        clearSlideOffset();
        return;
      }
      swipeStart.locked = true;
    }
    if (state.lightboxItems.length < 2) return;
    // 브라우저가 가로 손짓을 뒤로가기 제스처로 가져가지 않도록 막는다.
    if (event.cancelable) event.preventDefault();
    setSlideOffset(dx, false);
  }

  function endSwipe(event) {
    const start = swipeStart;
    swipeStart = null;
    if (!start) return;
    if (start.locked) {
      // 되돌아가든 넘어가든, 손을 뗄 때 따라오는 click 으로 창이 닫히면 안 된다.
      swipedAt = Date.now();
    }
    if (state.lightboxItems.length < 2) {
      clearSlideOffset();
      return;
    }

    const touch = event.changedTouches && event.changedTouches[0];
    if (!touch) {
      clearSlideOffset();
      return;
    }
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;

    // 세로로 더 많이 움직였으면 넘기지 않는다. 스크롤하려던 손짓이다.
    const far = Math.abs(dx) >= SWIPE_MIN_PX && Math.abs(dx) >= Math.abs(dy) * 1.5;
    if (!far || Date.now() - start.at > SWIPE_MAX_MS) {
      // 안 넘어가면 제자리로 돌아간다. 그 자체가 "여기까지는 아니다" 라는 답이다.
      setSlideOffset(0, true);
      window.setTimeout(clearSlideOffset, SLIDE_MS);
      return;
    }

    // 왼쪽으로 밀면 다음 사진. 종이를 넘기는 방향과 같다.
    slideLightbox(dx < 0 ? 1 : -1);
    swipedAt = Date.now();
  }

  // 두 손가락으로 확대. 위의 touch-action: pan-y 가 브라우저 핀치를 끄므로
  // (가로 손짓은 넘기기로 쓴다) 여기서 직접 받는다. 화면이 아니라 사진만
  // 커지고, 확대율이 올라가면 넘기기는 저절로 꺼진다.
  function touchSpan(touches) {
    return Math.hypot(
      touches[0].clientX - touches[1].clientX,
      touches[0].clientY - touches[1].clientY,
    ) || 1;
  }

  function touchCenter(touches) {
    return {
      clientX: (touches[0].clientX + touches[1].clientX) / 2,
      clientY: (touches[0].clientY + touches[1].clientY) / 2,
    };
  }

  function startPinch(event) {
    if (lightbox.hidden) return;
    swipeStart = null;
    clearSlideOffset();
    state.lightboxPinch = { span: touchSpan(event.touches), zoom: state.lightboxZoom };
  }

  function movePinch(event) {
    const pinch = state.lightboxPinch;
    if (!pinch || event.touches.length < 2) return;
    if (event.cancelable) event.preventDefault();
    const next = clampZoom(pinch.zoom * (touchSpan(event.touches) / pinch.span));
    if (next === state.lightboxZoom) return;
    state.lightboxZoom = next;
    // 벌린 두 손가락 사이를 붙잡고 커진다. 가운데를 기준으로 하면 보고 있던
    // 자리가 화면 밖으로 밀려난다.
    renderLightboxZoom({ anchor: lightboxAnchorFromEvent(touchCenter(event.touches)) });
  }

  function handleLightboxTouchStart(event) {
    if (event.touches.length >= 2) {
      startPinch(event);
      return;
    }
    startSwipe(event);
  }

  function handleLightboxTouchMove(event) {
    if (state.lightboxPinch) {
      movePinch(event);
      return;
    }
    moveSwipe(event);
  }

  function handleLightboxTouchEnd(event) {
    if (state.lightboxPinch) {
      if (event.touches.length >= 2) return;
      state.lightboxPinch = null;
      // 한 손가락이 남아 있어도 그걸로 넘기지 않는다. 핀치를 풀다가 사진이
      // 넘어가 버린다.
      swipeStart = null;
      return;
    }
    endSwipe(event);
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
    // 프로필 사진처럼 어디에도 속하지 않은 이미지는 좋아요를 달지 않는다.
    // 올린 사진은 글이 없어서 post_id 가 비지만 좋아요는 센다 — photo_likes
    // 는 photo_id 로 세고 post_id 는 적어 두기만 한다.
    if (item.post_id || item.likeable) {
      lightboxLike.innerHTML = likeButtonHtml(
        item.src, item.post_id, state.photoLikes[item.src] || 0,
      );
      lightboxLike.hidden = false;
    } else {
      lightboxLike.innerHTML = "";
      lightboxLike.hidden = true;
    }
    lightboxPrev.hidden = state.lightboxItems.length < 2;
    lightboxNext.hidden = state.lightboxItems.length < 2;
    // 이전·다음으로 옮겨 다닌 것까지 뒤로가기로 되짚게 하면, 20장을 넘긴 뒤
    // 목록으로 가는 데 뒤로가기를 20번 눌러야 한다. 주소만 갈아 끼운다.
    if (pushedPhotoRoute && item.src) {
      window.history.replaceState(
        { photo: photoRouteId(item.src) }, "", `/photos/${photoRouteId(item.src)}/`,
      );
    }
    lightbox.hidden = false;
    window.requestAnimationFrame(resetLightboxZoom);
  }

  function closeLightbox() {
    lightbox.hidden = true;
    state.lightboxZoom = LIGHTBOX_MIN_ZOOM;
    state.lightboxDrag = null;
    state.lightboxPinch = null;
    swipeStart = null;
    slideBusy = false;
    pushedPhotoRoute = false;
    lightboxImageWrap.classList.remove("is-zoomed");
    lightboxImageWrap.classList.remove("is-dragging");
    lightboxImageWrap.classList.remove("is-sliding");
    lightboxImage.removeAttribute("style");
    lightboxImage.removeAttribute("src");
  }

  function moveLightbox(delta) {
    if (!state.lightboxItems.length || lightbox.hidden) return;
    const next = (state.lightboxIndex + delta + state.lightboxItems.length) % state.lightboxItems.length;
    showLightbox(next);
  }

  // 모아보기·업로드에서 연 사진은 주소를 가진다. 영상과 같은 방식이다 —
  // 폰의 뒤로가기로 닫히고, 사진 한 장을 그대로 보낼 수 있다.
  //
  // 글 본문(/posts/123/)에서 연 사진은 그대로 둔다. 그 사진들은 본문에서
  // 긁어온 것이라 photos.json 에 없을 수도 있어서, 주소만 받아서는 다시
  // 찾아낼 수가 없다.
  let pushedPhotoRoute = false;

  function goToPhoto(index, items = null) {
    const list = items || state.lightboxItems;
    const item = list[index];
    if (!item) return;
    if (item.src) {
      const id = photoRouteId(item.src);
      window.history.pushState({ photo: id }, "", `/photos/${id}/`);
      pushedPhotoRoute = true;
    }
    showLightbox(index, items);
  }

  function leavePhoto() {
    if (lightbox.hidden) return;
    if (pushedPhotoRoute) {
      // popstate 가 받아서 닫는다. 여기서 바로 닫으면 주소만 남는다.
      window.history.back();
      return;
    }
    // 주소를 직접 열고 들어왔으면 되돌아갈 자리가 없다. 주소만 목록으로
    // 돌려놓는다 — 안 그러면 새로고침할 때 사진이 다시 열린다.
    if (photoIdFromPath(window.location.pathname)) {
      window.history.replaceState({}, "", `/?type=${encodeURIComponent(state.activeType)}`);
    }
    closeLightbox();
  }

  // 주소를 직접 열었거나 뒤로·앞으로 온 경우. 지금 걸린 필터 안에서 먼저
  // 찾고, 거기 없으면 전체에서 찾는다 — 지난달 필터가 켜져 있다고 공유받은
  // 사진이 안 열리면 그게 더 이상하다.
  function openPhotoRoute(id) {
    const pick = (list) => list.findIndex((item) => photoRouteId(item.src) === id);
    let pool = filteredPhotos().filter((item) => item.kind !== "video");
    let index = pick(pool);
    if (index < 0) {
      pool = galleryItems()
        .filter((item) => item.kind !== "video")
        .sort((a, b) => String(b.date).localeCompare(String(a.date)));
      index = pick(pool);
    }
    if (index < 0) return false;
    showLightbox(index, pool.map((item) => ({
      src: item.src,
      alt: item.caption || "",
      caption: item.caption || item.date || "",
      post_id: item.post_id,
      likeable: true,
    })));
    return true;
  }

  // ---- 관리자 모드 ----
  // 비밀번호는 Function 이 Pages Secret 과 대조한다. 여기서 들고 있는 값은
  // 다음 요청에 같이 보내기 위한 것일 뿐이고, 이 코드가 통과 여부를 정하지
  // 않는다. 사이트 입구의 가족 패스코드와 다른 점이 이것이다.
  function setAdminMessage(text, isError) {
    adminMessage.textContent = text;
    adminMessage.classList.toggle("is-error", Boolean(isError));
  }

  const isAdmin = () => Boolean(state.adminPasscode);

  /** 관리자 상태를 화면 전체에 반영한다. 삭제 버튼들이 이 클래스로 켜진다. */
  function renderAdminState() {
    document.body.classList.toggle("is-admin", isAdmin());
    adminBanner.hidden = !isAdmin();
  }

  function enterAdmin(passcode) {
    state.adminPasscode = passcode;
    window.sessionStorage.setItem("kidsnote.admin", passcode);
    renderAdminState();
  }

  function exitAdmin() {
    state.adminPasscode = "";
    window.sessionStorage.removeItem("kidsnote.admin");
    renderAdminState();
  }

  function openAdminModal() {
    adminModal.hidden = false;
    document.body.classList.add("modal-open");
    setAdminMessage("", false);
    if (isAdmin()) {
      showAdminPanel();
    } else {
      adminLoginForm.hidden = false;
      adminSessionBox.hidden = true;
      adminMyboxForm.hidden = true;
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

  function showAdminPanel() {
    adminLoginForm.hidden = true;
    adminSessionBox.hidden = false;
    adminMyboxForm.hidden = false;
    adminPasscodeForm.hidden = false;
    myboxChild.innerHTML = children.map((child) => (
      `<option value="${escapeHtml(child.key)}"${child.key === state.activeChild ? " selected" : ""}>`
      + `${escapeHtml(child.name)}</option>`
    )).join("");
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
      // 비번이 틀렸거나 바뀐 것이므로 세션을 끊는다.
      exitAdmin();
      adminLoginForm.hidden = false;
      adminSessionBox.hidden = true;
      adminMyboxForm.hidden = true;
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
    // 값이 맞는지 서버에 물어본다. 틀린 채로 세션에 들어가면 삭제 버튼이
    // 보이는데 누를 때마다 실패하는, 설명하기 어려운 상태가 된다.
    setAdminMessage("확인하는 중...", false);
    let response;
    try {
      response = await fetch("/api/admin/check", {
        method: "POST",
        headers: { "content-type": "application/json", accept: "application/json" },
        body: JSON.stringify({ passcode: value }),
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
      } else {
        setAdminMessage("비밀번호가 맞지 않습니다.", true);
      }
      adminPasscodeInput.select();
      return;
    }

    enterAdmin(value);
    setAdminMessage("", false);
    showAdminPanel();
    refreshView();
  }

  function clearPasscodeFields() {
    currentPasscode.value = "";
    newPasscode.value = "";
    newPasscodeConfirm.value = "";
  }

  /** MYBOX 루트의 폴더 목록을 드롭다운에 채운다. 비번이 필요하므로 로그인 뒤에만. */
  async function loadMyboxFolders() {
    myboxFolder.innerHTML = '<option value="">불러오는 중...</option>';
    const result = await adminRequest("/api/mybox/list", "POST", {});
    if (!result) {
      myboxFolder.innerHTML = '<option value="">불러오지 못했습니다</option>';
      return;
    }
    const folders = (result.resources || []).filter((item) => item.type === "folder");
    if (!folders.length) {
      myboxFolder.innerHTML = '<option value="">폴더가 없습니다</option>';
      return;
    }
    myboxFolder.innerHTML = folders.map((folder) => (
      `<option value="${escapeHtml(folder.id)}">${escapeHtml(folder.name)}</option>`
    )).join("");
    setAdminMessage("", false);
  }

  async function handleMyboxSync(event) {
    event.preventDefault();
    const folder = myboxFolder.value;
    if (!folder) {
      setAdminMessage("폴더를 선택해주세요.", true);
      return;
    }
    setAdminMessage("가져오는 중...", false);
    const result = await adminRequest("/api/mybox/sync", "POST", {
      folder,
      child: myboxChild.value,
    });
    if (!result) return;

    const name = myboxFolder.options[myboxFolder.selectedIndex]?.textContent || "";
    setAdminMessage(
      result.count
        ? `"${name}" 에서 영상 ${result.count}개를 가져왔습니다.`
        : `"${name}" 에 동영상이 없습니다.`,
      false,
    );

    await loadVideos();
    if (myboxChild.value !== state.activeChild) {
      setActiveChild(myboxChild.value);
    } else {
      recomputeCounts();
      renderMonthOptions();
      refreshView();
    }
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

  /** 관리자 삭제 공통. 지웠으면 true. */
  async function adminDelete(url, label) {
    if (!isAdmin()) return false;
    if (!window.confirm(`${label}을(를) 삭제할까요?`)) return false;
    const result = await adminRequest(url, "DELETE", {});
    return Boolean(result);
  }

  async function deleteVideo(id) {
    if (!(await adminDelete(`/api/videos/${encodeURIComponent(id)}`, "이 영상"))) return;
    await loadVideos();
    recomputeCounts();
    renderMonthOptions();
    refreshView();
  }

  async function deleteComment(id, reload) {
    if (!(await adminDelete(`/api/comments/${encodeURIComponent(id)}`, "이 댓글"))) return;
    await reload();
  }

  async function openVideoModal(id) {
    const video = state.videos.find((item) => item.id === id);
    if (!video) return;
    videoModalTitle.textContent = video.title;
    videoModalLink.dataset.videoId = video.id;
    renderVideoSteps(video.id);

    if ((video.source || "link") === "mybox") {
      // MYBOX 에서 가져온 영상. 재생 URL 은 10분짜리 1회용이라 목록에
      // 담아 둘 수 없고, 열 때마다 받아 쓴다.
      videoModalLink.removeAttribute("href");
      videoModalFrame.replaceChildren(
        Object.assign(document.createElement("p"), {
          className: "video-loading",
          textContent: "불러오는 중...",
        }),
      );
      videoModal.hidden = false;
      document.body.classList.add("modal-open");
      videoModalClose.focus();

      // MYBOX 가 주는 URL 을 직접 쓰지 않고 /stream 을 거친다. 그쪽 응답이
      // application/octet-stream 이라 <video> 가 거부하기 때문이다.
      const player = document.createElement("video");
      player.src = `/api/videos/${encodeURIComponent(id)}/stream`;
      player.controls = true;
      player.autoplay = true;
      player.playsInline = true;
      player.addEventListener("error", () => {
        videoModalFrame.replaceChildren(
          Object.assign(document.createElement("p"), {
            className: "video-loading",
            textContent: "영상을 불러오지 못했습니다.",
          }),
        );
      });
      if (videoModal.hidden) return;   // 받는 사이에 닫았다면 그만둔다
      videoModalFrame.replaceChildren(player);

      const slot = document.getElementById("videoComments");
      if (slot) {
        slot.innerHTML = commentSectionHtml();
        wireCommentForm("video", video.id);
        loadComments("video", video.id);
      }
      return;
    }

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

  // 우리가 주소를 밀어 넣었는지. 링크로 바로 들어온 경우에는 뒤로 갈 곳이
  // 없어서 history.back() 이 사이트 밖으로 나가 버린다.
  let pushedVideoRoute = false;

  // 지금 보고 있는 목록 안에서 움직인다. state.videos 전체가 아니라
  // filteredVideos() 를 쓰는 이유: 자녀를 바꾸거나 검색한 상태에서 "다음"을
  // 눌렀는데 화면에 없던 영상이 나오면 어디서 온 건지 알 수 없다.
  function videoNeighbours(id) {
    const list = filteredVideos();
    const index = list.findIndex((video) => video.id === id);
    return { list, index };
  }

  function renderVideoSteps(id) {
    const { list, index } = videoNeighbours(id);
    const many = list.length > 1 && index >= 0;
    videoPrev.hidden = !many;
    videoNext.hidden = !many;
    videoStepCount.textContent = many ? `${index + 1} / ${list.length}` : "";
    videoLike.innerHTML = likeButtonHtml(id, "", state.photoLikes[id] || 0);
    if (!many) return;
    videoPrev.disabled = index <= 0;
    videoNext.disabled = index >= list.length - 1;
  }

  function stepVideo(delta) {
    const current = videoModalLink.dataset.videoId;
    const { list, index } = videoNeighbours(current);
    const next = list[index + delta];
    if (!next) return;
    // 주소도 같이 바꾼다. 뒤로가기가 영상 하나씩 되짚는 대신 목록으로
    // 바로 돌아가도록 replace 를 쓴다 — 20개를 넘겼다고 뒤로가기를 20번
    // 눌러야 하면 폰에서 쓸 수 없다.
    if (pushedVideoRoute) {
      window.history.replaceState({ video: next.id }, "", `/videos/${encodeURIComponent(next.id)}/`);
    }
    openVideoModal(next.id);
  }

  function goToVideo(id) {
    if (!id) return;
    window.history.pushState({ video: id }, "", `/videos/${encodeURIComponent(id)}/`);
    pushedVideoRoute = true;
    openVideoModal(id);
  }

  function leaveVideo() {
    if (videoModal.hidden) return;
    if (pushedVideoRoute) {
      // popstate 가 받아서 닫는다. 여기서 바로 닫으면 주소만 남는다.
      window.history.back();
      return;
    }
    window.history.replaceState({}, "", "/");
    closeVideoModal();
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

  async function loadUploads() {
    try {
      const response = await fetch("/api/uploads", { headers: { accept: "application/json" } });
      const type = response.headers.get("content-type") || "";
      if (!response.ok || !type.includes("application/json")) throw new Error("uploads api");
      const payload = await response.json();
      state.uploads = payload.uploads || [];
      state.uploadsLoaded = true;
    } catch {
      state.uploads = [];
      state.uploadsLoaded = false;
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

  // 무작위 배치.
  //
  // 고를 때마다 새로 섞으면 안 된다. renderPhotoList 는 "더 보기", 좋아요,
  // 자녀 전환, 검색마다 다시 도는데 그때마다 순서가 바뀌면 방금 본 사진이
  // 또 나오고 아직 못 본 사진은 뒤로 밀린다. 6,600장이면 끝까지 못 본다.
  //
  // 그래서 씨앗을 하나 잡아 두고 사진마다 (씨앗, 경로)로 같은 숫자를 뽑아
  // 그 순으로 세운다. 씨앗이 그대로면 순서도 그대로다. 순열을 따로 들고
  // 있을 필요가 없어서 사진이 늘어도 메모리가 늘지 않는다.
  let photoSeed = newPhotoSeed();

  function newPhotoSeed() {
    return (Math.random() * 0xffffffff) >>> 0;
  }

  // FNV-1a. 암호용이 아니라 섞기용이라 짧고 빠른 것으로 충분하다.
  function shuffleKey(seed, text) {
    let hash = (0x811c9dc5 ^ seed) >>> 0;
    const value = String(text);
    for (let i = 0; i < value.length; i += 1) {
      hash ^= value.charCodeAt(i);
      hash = Math.imul(hash, 0x01000193);
    }
    return hash >>> 0;
  }

  // 모아보기는 세 군데서 온 것을 한 화면에 모은다 — 키즈노트 사진, 직접 올린
  // 사진, 영상. `kind` 가 무엇을 그릴지와 눌렀을 때 어디로 갈지를 가른다.
  //
  // `key` 는 좋아요를 세는 단위다. photo_likes 의 photo_id 는 길이만 제한된
  // 자유 문자열이라 사진 경로든 영상 id 든 그대로 쓸 수 있다 — 표를 더 만들
  // 이유가 없다.
  function galleryItems() {
    return [
      ...childPhotos().map((photo) => ({
        kind: "photo",
        key: photo.src,
        src: photo.src,
        date: photo.date || "",
        caption: photo.caption || "",
        post_id: photo.post_id,
      })),
      ...childUploads().map((item) => ({
        kind: "upload",
        key: item.src,
        src: item.src,
        date: uploadDate(item),
        caption: item.caption || "",
      })),
      ...childVideos().map((video) => ({
        kind: "video",
        key: video.id,
        id: video.id,
        source: video.source,
        date: video.date || (video.created_at ? koreaDateKey(new Date(video.created_at)) : ""),
        caption: video.title || "",
      })),
    ];
  }

  function filteredPhotos() {
    const query = normalize(state.query).trim();
    let list = galleryItems().filter((item) => {
      if (state.activeMonth && String(item.date || "").slice(0, 7) !== state.activeMonth) return false;
      if (!query) return true;
      return normalize([item.caption, item.date].join(" ")).includes(query);
    });
    if (state.photoSort === "random") {
      list = [...list].sort(
        (a, b) => shuffleKey(photoSeed, a.key) - shuffleKey(photoSeed, b.key),
      );
    } else if (state.photoSort === "likes") {
      list = [...list].sort((a, b) => {
        const la = state.photoLikes[a.key] || 0;
        const lb = state.photoLikes[b.key] || 0;
        if (lb !== la) return lb - la;
        return String(b.date).localeCompare(String(a.date));
      });
    } else {
      list = [...list].sort((a, b) => String(b.date).localeCompare(String(a.date)));
    }
    return list;
  }

  // ---- 업로드 ----
  //
  // 파일 바이트는 브라우저가 MYBOX 로 직접 보낸다. 서버는 올릴 자리(1회용
  // URL)를 내주고, 끝난 뒤 결과를 받아 적기만 한다.
  //
  // 주로 폰에서 쓴다는 전제로 짰다. 그래서:
  //  - 한 번에 한 개씩 보낸다. 끊겨도 잃는 건 전송 중인 하나뿐이다.
  //  - 사진은 **웹용 사본을 전부 먼저** 올리고 원본을 나중에 올린다. 사본은
  //    600KB 수준이라 금방 끝나서, 중간에 앱을 바꿔도 사진은 이미 다 보인다.
  //    끊기면 "보관본만 없는" 상태로 남고 그건 나중에 채울 수 있다.
  //  - MYBOX 는 이어올리기를 지원하지 않는다(Content-Range 400, 업로드 URL
  //    재사용 400). 끊긴 파일은 처음부터 다시 올려야 한다.
  const UPLOAD_MAX_EDGE = 2048;
  const UPLOAD_QUALITY = 0.85;

  let uploadBusy = false;
  let wakeLock = null;

  async function keepScreenAwake(on) {
    try {
      if (on && "wakeLock" in navigator) {
        wakeLock = await navigator.wakeLock.request("screen");
      } else if (!on && wakeLock) {
        await wakeLock.release();
        wakeLock = null;
      }
    } catch {
      // 지원하지 않거나 거부당하면 그냥 둔다. 있으면 좋은 것이지 필수가 아니다.
    }
  }

  function showUploadSheet(title, ratio, note, isError) {
    uploadSheet.hidden = false;
    uploadTitle.textContent = title;
    uploadFill.style.width = `${Math.round(Math.max(0, Math.min(1, ratio)) * 100)}%`;
    uploadNote.textContent = note || "";
    uploadNote.classList.toggle("is-error", Boolean(isError));
  }

  /**
   * 긴 변 2048px JPEG 사본. 화면에 띄울 용도다.
   *
   * 여기서 EXIF 회전이 정리되고 GPS 좌표도 같이 떨어진다 — 다시 인코딩하면
   * 메타데이터가 남지 않는다. 이 저장소가 노션 업로드에서 이미 GPS 를 지우고
   * 있어서 같은 기조다.
   *
   * <img> 로 디코딩하는 이유: 요즘 브라우저는 캔버스에 그릴 때도 EXIF 방향을
   * 적용한다. 아이폰 HEIC 도 사파리는 이걸로 읽는다. 못 읽으면 null 을 주고,
   * 부르는 쪽이 원본만 올린다.
   */
  async function makeWebCopy(file) {
    const url = URL.createObjectURL(file);
    try {
      const image = await new Promise((resolve, reject) => {
        const el = new Image();
        el.onload = () => resolve(el);
        el.onerror = () => reject(new Error("decode"));
        el.src = url;
      });
      const long = Math.max(image.naturalWidth, image.naturalHeight);
      if (!long) return null;
      const scale = Math.min(1, UPLOAD_MAX_EDGE / long);
      const width = Math.round(image.naturalWidth * scale);
      const height = Math.round(image.naturalHeight * scale);

      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      canvas.getContext("2d").drawImage(image, 0, 0, width, height);

      const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", UPLOAD_QUALITY));
      return blob ? { blob, width, height } : null;
    } catch {
      return null;
    } finally {
      URL.revokeObjectURL(url);
    }
  }

  /**
   * MYBOX 로 한 개 보낸다.
   *
   * fetch 가 아니라 XHR 인 이유는 진행률 하나뿐이다. fetch 에는 업로드
   * 진행을 알 방법이 없고, 폰에서 20초씩 걸리는 전송은 멈춘 것처럼 보인다.
   */
  function sendToMybox(uploadUrl, blob, fileName, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("Filedata", blob, fileName);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", uploadUrl);
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total);
      };
      xhr.onload = () => {
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(`업로드 실패 (${xhr.status})`));
          return;
        }
        try {
          resolve(JSON.parse(xhr.responseText || "{}"));
        } catch {
          reject(new Error("업로드 응답을 읽지 못했습니다"));
        }
      };
      xhr.onerror = () => reject(new Error("연결이 끊겼습니다"));
      xhr.onabort = () => reject(new Error("업로드가 중단됐습니다"));
      xhr.send(form);
    });
  }

  /** 자리를 받아 한 개를 끝까지 올린다. 실패하면 던진다. */
  async function uploadOne(blob, fileName, isWeb, onProgress) {
    const issued = await adminRequest("/api/uploads/url", "POST", {
      file_name: fileName,
      file_size: blob.size,
      web: Boolean(isWeb),
    });
    if (!issued?.upload_url) throw new Error("업로드할 자리를 받지 못했습니다");
    return sendToMybox(issued.upload_url, blob, fileName, onProgress);
  }

  // 같은 이름이 겹치지 않게 앞에 시각을 붙인다. MYBOX 에는 이름 변경이 없어서
  // 나중에 고칠 수 없다 — 올릴 때 제대로 지어야 한다.
  function uploadName(file, suffix) {
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`
      + `-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
    const safe = String(file.name || "photo").replace(/[\\/:*?"<>|]/g, "_");
    return suffix ? `${stamp}-${suffix}-${safe}` : `${stamp}-${safe}`;
  }

  /**
   * 고른 파일들을 올린다.
   *
   * 사진은 2단계다 — 사본 전부 → 원본 전부. 한 장씩 "사본+원본"을 끝내고
   * 다음으로 가면, 10장 중 3장째에 끊겼을 때 7장이 아예 안 보인다. 사본을
   * 먼저 다 올리면 30초 만에 열 장이 전부 사이트에 뜨고, 그 뒤에 끊겨도
   * 보관본만 비어 있다.
   */
  async function runUpload(files) {
    if (uploadBusy || !files.length) return;
    uploadBusy = true;
    await keepScreenAwake(true);

    const photos = files.filter((f) => (f.type || "").startsWith("image/"));
    const videos = files.filter((f) => (f.type || "").startsWith("video/"));
    const skipped = files.length - photos.length - videos.length;
    const child = state.activeChild;
    const failures = [];

    // ---- 1단계: 사진 사본 ----
    const recorded = [];
    for (let i = 0; i < photos.length; i += 1) {
      const file = photos[i];
      showUploadSheet(`사진 ${i + 1}/${photos.length} 올리는 중`, 0, file.name);
      try {
        const copy = await makeWebCopy(file);
        if (!copy) throw new Error("이 사진은 브라우저가 열지 못했습니다");
        const name = uploadName(file, "web").replace(/\.[^.]+$/, "") + ".jpg";
        const up = await uploadOne(copy.blob, name, true, (r) => {
          showUploadSheet(`사진 ${i + 1}/${photos.length} 올리는 중`, r, file.name);
        });
        const saved = await adminRequest("/api/uploads", "POST", {
          child,
          web_id: up.resourceId,
          file_name: file.name,
          width: copy.width,
          height: copy.height,
          size: file.size,
        });
        if (!saved?.id) throw new Error("기록하지 못했습니다");
        recorded.push({ file, id: saved.id, webId: up.resourceId });
      } catch (err) {
        failures.push(`${file.name}: ${err.message}`);
      }
    }

    // 사본이 올라간 시점에 이미 볼 수 있다. 원본을 기다리게 하지 않는다.
    if (recorded.length) {
      await loadUploads();
      recomputeCounts();
      renderTabs();
      refreshView();
    }

    // ---- 2단계: 사진 원본 ----
    for (let i = 0; i < recorded.length; i += 1) {
      const { file, id, webId } = recorded[i];
      showUploadSheet(`원본 보관 ${i + 1}/${recorded.length}`, 0, file.name);
      try {
        const up = await uploadOne(file, uploadName(file), false, (r) => {
          showUploadSheet(`원본 보관 ${i + 1}/${recorded.length}`, r, file.name);
        });
        await adminRequest("/api/uploads", "POST", {
          child, web_id: webId, file_id: up.resourceId, file_name: file.name,
        });
      } catch (err) {
        // 사진은 이미 보인다. 보관본만 없는 상태로 남겨 둔다.
        failures.push(`${file.name} 원본: ${err.message}`);
      }
    }

    // ---- 3단계: 영상 ----
    for (let i = 0; i < videos.length; i += 1) {
      const file = videos[i];
      showUploadSheet(`영상 ${i + 1}/${videos.length} 올리는 중`, 0, file.name);
      try {
        const up = await uploadOne(file, uploadName(file), false, (r) => {
          showUploadSheet(`영상 ${i + 1}/${videos.length} 올리는 중`, r, file.name);
        });
        await adminRequest("/api/uploads/video", "POST", {
          child, file_id: up.resourceId, file_name: file.name,
        });
      } catch (err) {
        failures.push(`${file.name}: ${err.message}`);
      }
    }

    // 다시 그리기 **전에** 풀어야 한다. 버튼의 disabled 가 그리는 시점의
    // uploadBusy 로 정해지므로, 순서가 뒤바뀌면 비활성인 채로 남고 새로고침
    // 전까지 다시 못 올린다.
    uploadBusy = false;
    await keepScreenAwake(false);

    await Promise.all([loadUploads(), loadVideos()]);
    recomputeCounts();
    renderTabs();
    refreshView();

    const done = recorded.length + videos.length - failures.filter((f) => !f.includes("원본")).length;
    const notes = [];
    if (skipped) notes.push(`${skipped}개는 사진·영상이 아니라 건너뛰었습니다`);
    if (failures.length) notes.push(failures.slice(0, 3).join(" / "));
    showUploadSheet(
      failures.length ? `${done}개 완료, ${failures.length}개 실패` : `${done}개 올렸습니다`,
      1,
      notes.join(" · "),
      failures.length > 0,
    );

    // 실패가 있으면 읽을 시간을 더 준다.
    window.setTimeout(() => { uploadSheet.hidden = true; }, failures.length ? 9000 : 2600);
  }

  function uploadBarHtml(accept, label) {
    if (!isAdmin()) return "";
    return '<div class="upload-bar">'
      + `<button class="upload-pick" type="button" data-upload-accept="${escapeHtml(accept)}"`
      + `${uploadBusy ? " disabled" : ""}>${escapeHtml(label)}</button>`
      + "</div>";
  }

  // 홈에 서는 올린 사진 한 줄. 글·영상과 같은 .entry 틀을 쓰되 사진이
  // 썸네일 자리에 들어간다. 눌러도 갈 상세가 없으므로 사진 탭으로 보낸다.
  function uploadRow(item) {
    const newMarker = isNewUploadItem(item) ? '<span class="new-pill">NEW</span>' : "";
    return (
      '<div class="entry-row">'
      + `<button class="entry" type="button" data-open-uploads="1">`
      + `<span class="entry-thumb" style="background-image: url('${escapeHtml(item.src)}')"></span>`
      + "<span>"
      + `<span class="entry-kicker"><span class="badge upload">사진</span>`
      + `<span>${escapeHtml(uploadDate(item))}</span>${newMarker}</span>`
      + `<span class="entry-title">${escapeHtml(item.caption || item.file_name || "올린 사진")}</span>`
      + "</span></button>"
      + `<span class="entry-like">${likeButtonHtml(item.src, "", state.photoLikes[item.src] || 0)}</span>`
      + `<button class="admin-delete" type="button" data-delete-upload="${escapeHtml(item.id)}"`
      + ' aria-label="사진 내리기">🗑</button>'
      + "</div>"
    );
  }

  const isNewUploadItem = (item) => Boolean(item.created_at)
    && state.recentDateKeys.has(koreaDateKey(new Date(item.created_at)));

  function renderUploadList() {
    const items = childUploads();
    listTitle.textContent = "올린 사진";
    listCount.textContent = `${items.length}장`;
    renderListCollapseState();

    const bar = uploadBarHtml("image/*", "사진 업로드");

    if (!items.length) {
      // 아직 아무도 안 올린 것과, API 가 죽어서 못 받은 것은 다른 상황이다.
      const empty = state.uploadsLoaded
        ? `<div class="empty">${escapeHtml(activeChild().name)} 사진을 아직 올리지 않았습니다.</div>`
        : '<div class="empty">올린 사진이 아직 없습니다.</div>';
      entryList.innerHTML = bar + empty;
      return;
    }

    const missing = items.filter((item) => item.original_missing).length;
    const warn = missing
      ? `<p class="upload-note is-error">원본 ${missing}개가 MYBOX 에 보관되지 않았습니다. 다시 올리면 채워집니다.</p>`
      : "";

    entryList.innerHTML = bar + warn + `<div class="photo-grid">${items.map((item, index) => (
      `<figure class="photo-cell${item.original_missing ? " is-partial" : ""}" data-upload-index="${index}">`
      + `<img src="${escapeHtml(item.src)}" alt="${escapeHtml(item.caption || "")}" loading="lazy">`
      + likeButtonHtml(item.src, "", state.photoLikes[item.src] || 0)
      + `<figcaption>${escapeHtml(item.caption || uploadDate(item))}</figcaption>`
      + `<button class="admin-delete" type="button" data-delete-upload="${escapeHtml(item.id)}"`
      + ' aria-label="사진 내리기">🗑</button>'
      + "</figure>"
    )).join("")}</div>`;

    // 라이트박스도 같은 순서로 따라간다.
    state.lightboxItems = items.map((item) => ({
      src: item.src,
      alt: item.caption || "",
      caption: item.caption || uploadDate(item),
      likeable: true,
    }));
  }

  function uploadDate(item) {
    if (!item.created_at) return "";
    return koreaDateKey(new Date(item.created_at));
  }

  function renderPhotoList() {
    const photos = filteredPhotos();
    const filters = [state.activeMonth, state.query.trim()].filter(Boolean).length;
    listTitle.textContent = "모아보기";
    listCount.textContent = filters ? `${photos.length}개 필터됨` : `${photos.length}개`;
    renderListCollapseState();

    if (!photos.length) {
      entryList.innerHTML = state.photosLoaded
        ? `<div class="empty">${escapeHtml(activeChild().name)} 사진·영상이 아직 없습니다.</div>`
        : '<div class="error">목록을 불러오지 못했습니다.</div>';
      return;
    }

    const shown = photos.slice(0, state.photoLimit);

    // 영상은 라이트박스가 아니라 영상 화면으로 간다. 그래서 라이트박스
    // 번호는 사진에만 매긴다 — 영상 자리까지 세면 번호가 어긋난다.
    const lightboxOf = new Map();
    shown.forEach((item) => {
      if (item.kind !== "video") lightboxOf.set(item, lightboxOf.size);
    });

    const cards = shown.map((item) => {
      const like = likeButtonHtml(item.key, item.post_id, state.photoLikes[item.key] || 0);
      const caption = `<figcaption>${escapeHtml(item.caption || item.date || "")}</figcaption>`;
      if (item.kind === "video") {
        return `<figure class="photo-cell video-cell" data-video-id="${escapeHtml(item.id)}">`
          + thumbOf(item) + like + caption + "</figure>";
      }
      return `<figure class="photo-cell" data-photo-index="${lightboxOf.get(item)}">`
        + `<img src="${escapeHtml(item.src)}" alt="${escapeHtml(item.caption || "")}" loading="lazy">`
        + like + caption + "</figure>";
    }).join("");

    const more = photos.length > shown.length
      ? `<button class="photo-more" type="button" id="photoMore">더 보기 (${photos.length - shown.length}장 남음)</button>`
      : "";

    entryList.innerHTML = (
      '<div class="photo-tools">'
      + `<button class="photo-sort${state.photoSort === "date" ? " is-active" : ""}" type="button" data-sort="date">최신순</button>`
      + `<button class="photo-sort${state.photoSort === "likes" ? " is-active" : ""}" type="button" data-sort="likes">좋아요순</button>`
      // 이미 무작위일 때는 "다시 섞기"로 바뀐다. 같은 버튼을 또 눌렀는데
      // 아무 일도 안 일어나면 고장으로 보이고, 다시 섞을 길도 없어진다.
      + `<button class="photo-sort${state.photoSort === "random" ? " is-active" : ""}" type="button" data-sort="random">`
      + `${state.photoSort === "random" ? "다시 섞기" : "무작위"}</button>`
      + "</div>"
      + `<div class="photo-grid">${cards}</div>${more}`
    );

    // 영상 썸네일은 보이는 것만 받아 온다.
    loadVisibleThumbs();

    // 모아보기에서 사진을 누르면 라이트박스로 크게 본다. 영상은 빠진다.
    state.lightboxItems = shown.filter((item) => item.kind !== "video").map((photo) => ({
      src: photo.src,
      alt: photo.caption || "",
      caption: photo.caption || photo.date || "",
      // 좋아요는 사진 경로로 센다. post_id 는 어느 글의 사진인지 적어 두는 용도다.
      post_id: photo.post_id,
      likeable: true,
    }));
  }

  async function loadApp() {
    renderAdminState();
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
        Promise.all([loadVideos(), loadPhotos(), loadUploads()]).then(() => {
          recomputeCounts();
          renderTabs();
        });
      } else {
        renderDocumentTitle();
        await Promise.all([loadVideos(), loadPhotos(), loadUploads()]);
        // 주소를 직접 열었거나 공유받은 링크로 들어온 경우. 목록을 먼저
        // 채워야 "3 / 18" 과 이전·다음이 맞는다.
        const deepVideo = videoIdFromPath(window.location.pathname);
        if (deepVideo) openVideoModal(deepVideo);
        const deepPhoto = photoIdFromPath(window.location.pathname);
        // 닫았을 때 사진이 있는 화면이 나오도록 탭도 같이 옮긴다.
        if (deepPhoto && state.activeType !== "photos" && state.activeType !== "uploads") {
          state.activeType = "photos";
        }
        await loadTopLikes();
        renderMonthOptions();
        refreshView();
        // 목록을 다 채운 뒤에 연다 — "3 / 18" 과 이전·다음이 맞아야 한다.
        if (deepPhoto) openPhotoRoute(deepPhoto);
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
    const del = event.target.closest("[data-delete-video]");
    if (del) {
      event.preventDefault();
      deleteVideo(del.dataset.deleteVideo);
      return;
    }

    const toUploads = event.target.closest("[data-open-uploads]");
    if (toUploads) {
      event.preventDefault();
      // 올린 사진은 상세 페이지가 없다. 홈에서 누르면 사진 탭으로 보낸다.
      state.activeType = UPLOADS_TYPE;
      window.localStorage.setItem("kidsnote.activeType", state.activeType);
      refreshView();
      return;
    }

    const pick = event.target.closest("button[data-upload-accept]");
    if (pick) {
      event.preventDefault();
      // accept 를 눌릴 때 정한다. 영상 탭에서는 영상만, 사진 탭에서는 둘 다
      // 고를 수 있어야 해서 입력칸 하나를 돌려 쓴다.
      uploadInput.accept = pick.dataset.uploadAccept;
      uploadInput.value = "";
      uploadInput.click();
      return;
    }

    const drop = event.target.closest("[data-delete-upload]");
    if (drop) {
      event.preventDefault();
      removeUpload(drop.dataset.deleteUpload);
      return;
    }

    const uploadCell = event.target.closest("figure[data-upload-index]");
    if (uploadCell) {
      event.preventDefault();
      goToPhoto(Number(uploadCell.dataset.uploadIndex), state.lightboxItems);
      return;
    }

    const like = event.target.closest(".like-button");
    if (like) {
      event.preventDefault();
      bumpLike(like);
      return;
    }

    const sort = event.target.closest("button[data-sort]");
    if (sort) {
      if (sort.dataset.sort === "random") photoSeed = newPhotoSeed();
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
      goToPhoto(Number(cell.dataset.photoIndex), state.lightboxItems);
      return;
    }

    // 목록은 <button>, 그리드는 <figure> 다. 태그를 묶어 두면 보기를 추가할
    // 때마다 여기도 같이 고쳐야 한다.
    const card = event.target.closest("[data-video-id]");
    if (!card) return;
    goToVideo(card.dataset.videoId);
  });

  adminHotspots.forEach((spot) => spot.addEventListener("click", openAdminModal));
  adminClose.addEventListener("click", closeAdminModal);
  adminModal.addEventListener("click", (event) => {
    if (event.target === adminModal) closeAdminModal();
  });
  adminLoginForm.addEventListener("submit", handleAdminLogin);
  adminExit.addEventListener("click", () => {
    exitAdmin();
    closeAdminModal();
    refreshView();
  });
  adminMyboxForm.addEventListener("submit", handleMyboxSync);
  adminMyboxToggle.addEventListener("click", () => {
    const open = adminMyboxFields.hidden;
    adminMyboxFields.hidden = !open;
    adminMyboxToggle.setAttribute("aria-expanded", String(open));
    if (!open) return;
    adminMyboxFields.scrollIntoView({ block: "nearest", behavior: "smooth" });
    // 폴더가 수십 개라 열 때 한 번만 읽는다.
    if (myboxFolder.options.length <= 1) loadMyboxFolders();
  });

  adminPasscodeForm.addEventListener("submit", handlePasscodeChange);
  adminPasscodeToggle.addEventListener("click", () => {
    const open = adminPasscodeFields.hidden;
    adminPasscodeFields.hidden = !open;
    adminPasscodeToggle.setAttribute("aria-expanded", String(open));
    if (!open) {
      clearPasscodeFields();
      return;
    }
    // 펼쳐진 칸이 화면 밖에 생기면 아무 일도 안 일어난 것처럼 보인다.
    adminPasscodeFields.scrollIntoView({ block: "nearest", behavior: "smooth" });
    currentPasscode.focus({ preventScroll: true });
  });

  videoModal.addEventListener("click", (event) => {
    const del = event.target.closest("[data-delete-comment]");
    if (!del) return;
    event.preventDefault();
    const videoId = videoModalLink.dataset.videoId || "";
    deleteComment(del.dataset.deleteComment, () => loadComments("video", videoId));
  });

  videoModalClose.addEventListener("click", leaveVideo);
  videoModal.addEventListener("click", (event) => {
    const like = event.target.closest(".like-button");
    if (!like) return;
    event.preventDefault();
    event.stopPropagation();
    bumpLike(like);
  });

  videoPrev.addEventListener("click", () => stepVideo(-1));
  videoNext.addEventListener("click", () => stepVideo(1));

  // 사진과 같은 손짓으로 넘긴다. <video> 위에서도 먹어야 해서 모달 전체에 건다.
  let videoSwipe = null;
  videoModal.addEventListener("touchstart", (event) => {
    videoSwipe = event.touches.length === 1
      ? { x: event.touches[0].clientX, y: event.touches[0].clientY, at: Date.now() }
      : null;
  }, { passive: true });
  videoModal.addEventListener("touchend", (event) => {
    const start = videoSwipe;
    videoSwipe = null;
    const touch = start && event.changedTouches && event.changedTouches[0];
    if (!touch) return;
    const dx = touch.clientX - start.x;
    const dy = touch.clientY - start.y;
    if (Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) < Math.abs(dy) * 1.5) return;
    if (Date.now() - start.at > SWIPE_MAX_MS) return;
    stepVideo(dx < 0 ? 1 : -1);
  }, { passive: true });
  videoModal.addEventListener("click", (event) => {
    if (event.target === videoModal) leaveVideo();
  });

  listTools.addEventListener("click", (event) => {
    const view = event.target.closest("button[data-video-view]");
    if (!view) return;
    state.videoView = view.dataset.videoView;
    window.localStorage.setItem("kidsnote.videoView", state.videoView);
    renderList();
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
    const del = event.target.closest("[data-delete-comment]");
    if (del) {
      event.preventDefault();
      deleteComment(del.dataset.deleteComment, () => loadComments("post", state.selectedId));
      return;
    }

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
    // 일러스트는 라이트박스로 열지 않는다. 크게 봐도 더 보이는 게 없고,
    // 라이트박스에는 다운로드 버튼이 달려 있어 더 어색하다.
    if (!child.photo || child.illustration) return;
    showLightbox(0, [{ src: child.photo, alt: child.name, caption: child.name }]);
  });

  lightbox.addEventListener("click", (event) => {
    // 좋아요가 먼저다. 아래 닫기 조건에 걸리면 누르자마자 창이 닫힌다.
    const like = event.target.closest(".like-button");
    if (like) {
      event.preventDefault();
      bumpLike(like);
      return;
    }

    // 사진 바깥을 누르면 닫힌다. 예전에는 여백이 lightbox 자신이었지만 이제
    // 사진 틀이 화면을 다 덮으므로, 틀을 눌렀고 사진이 아닌 경우로 본다.
    // 확대 중에는 끄지 않는다 — 끌어서 옮기다 손을 떼면 닫혀 버린다.
    // 민 직후에는 닫지 않는다. 기기에 따라 스와이프 끝에 click 이 한 번
    // 따라오는데, 그게 바깥 클릭으로 잡히면 넘기자마자 창이 닫힌다.
    const onBackdrop = event.target === lightboxImageWrap
      && state.lightboxZoom <= LIGHTBOX_MIN_ZOOM
      && Date.now() - swipedAt > 400;
    if (onBackdrop || event.target === lightbox || event.target.closest(".lightbox-close")) {
      leavePhoto();
    }
    if (event.target.closest(".lightbox-prev")) slideLightbox(-1);
    if (event.target.closest(".lightbox-next")) slideLightbox(1);
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
  lightboxImageWrap.addEventListener("touchstart", handleLightboxTouchStart, { passive: true });
  // passive 로 두면 preventDefault 가 안 먹어서, 사진을 미는 동안 브라우저가
  // 가로 손짓을 뒤로가기로 가져가는 기기가 있다.
  lightboxImageWrap.addEventListener("touchmove", handleLightboxTouchMove, { passive: false });
  lightboxImageWrap.addEventListener("touchend", handleLightboxTouchEnd, { passive: true });
  lightboxImageWrap.addEventListener("touchcancel", () => {
    swipeStart = null;
    state.lightboxPinch = null;
    clearSlideOffset();
  }, { passive: true });

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
      leaveVideo();
      return;
    }
    if (event.key === "Escape") leavePhoto();
    if (event.key === "ArrowLeft") slideLightbox(-1);
    if (event.key === "ArrowRight") slideLightbox(1);
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

  async function removeUpload(id) {
    if (!isAdmin() || !id) return;
    // MYBOX 에는 삭제 API 가 없다. 여기서 지우는 건 목록에서 내리는 것뿐이고
    // 원본은 드라이브에 남는다 — 그 사실을 묻는 말에 그대로 적는다.
    if (!window.confirm("사이트에서 내릴까요? MYBOX 원본은 지워지지 않습니다.")) return;
    const done = await adminRequest(`/api/uploads/${encodeURIComponent(id)}`, "DELETE", {});
    if (!done) return;
    await loadUploads();
    recomputeCounts();
    renderTabs();
    refreshView();
  }

  window.addEventListener("popstate", () => {
    const id = videoIdFromPath(window.location.pathname);
    if (id) {
      openVideoModal(id);
      return;
    }
    const photo = photoIdFromPath(window.location.pathname);
    if (photo && openPhotoRoute(photo)) return;
    pushedVideoRoute = false;
    pushedPhotoRoute = false;
    closeVideoModal();
    closeLightbox();
  });

  uploadInput.addEventListener("change", () => {
    const files = Array.from(uploadInput.files || []);
    uploadInput.value = "";
    if (files.length) runUpload(files);
  });

  // 올리는 중에 탭을 닫으려 하면 되묻는다. MYBOX 는 이어올리기가 없어서
  // 전송 중인 파일은 처음부터 다시 올려야 한다.
  window.addEventListener("beforeunload", (event) => {
    if (!uploadBusy) return;
    event.preventDefault();
    event.returnValue = "";
  });

  loadApp();
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

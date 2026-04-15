#!/usr/bin/env python3
"""
Export Zubax Forum threads (Discourse) to local Markdown with attachments. This is designed to make forum thread
contents easily available to AI agents, especially when referenced threads contain design specifications.

FEATURES

- Fetch root topic and recursively fetch internally linked topics.
- Supports private topics using ZUBAX_FORUM_API_KEY or ZUBAX_FORUM_API_TOKEN (+ username).
- Downloads attachment assets and rewrites references to local files.
- Writes one Markdown document per fetched topic.

USAGE

    export ZUBAX_FORUM_API_TOKEN='...'
    zubax-forum-export https://forum.zubax.com/t/some-slug/123
    python scripts/zubax_forum_export.py https://forum.zubax.com/t/some-slug/123
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}
UPLOAD_REF_PATTERN = re.compile(
    r"upload://[A-Za-z0-9._-]+|https?://[^\s)\]>\"']*?/uploads/[^\s)\]>\"']+|/uploads/[^\s)\]>\"']+"
)
TOPIC_LINK_PATTERN = re.compile(r"https?://[^\s)\]>\"']+|/t/[^\s)\]>\"']+")
TRAILING_URL_PUNCT = ".,;:!?"
LOGGER = logging.getLogger("zubax_forum_export")


@dataclass
class PostRecord:
    id: int
    post_number: int
    username: str
    created_at: str
    raw: str
    cooked: str


@dataclass
class AttachmentMention:
    canonical_key: str
    token: Optional[str]
    labels: List[str] = field(default_factory=list)


@dataclass
class TopicRecord:
    id: int
    slug: str
    title: str
    original_url: str
    posts: List[PostRecord]
    linked_topic_ids: Set[int] = field(default_factory=set)
    attachment_mentions: Dict[str, AttachmentMention] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)


@dataclass
class CookedUploadInfo:
    ordered_urls: List[str] = field(default_factory=list)
    sha1_to_urls: Dict[str, List[str]] = field(default_factory=dict)
    basename_to_urls: Dict[str, List[str]] = field(default_factory=dict)


class DownloadError(RuntimeError):
    pass


class ForumClient:
    def __init__(
        self,
        origin: str,
        api_key: Optional[str],
        api_username: str,
        timeout: float,
        retries: int,
        verbose: bool = False,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.api_key = api_key
        self.api_username = api_username
        self.timeout = timeout
        self.retries = max(0, retries)
        self.verbose = verbose

    def get_json(self, path_or_url: str) -> dict:
        payload, _ = self._get(path_or_url, accept="application/json")
        return json.loads(payload.decode("utf-8"))

    def get_bytes(self, path_or_url: str) -> Tuple[bytes, Dict[str, str]]:
        return self._get(path_or_url, accept="*/*")

    def _get(self, path_or_url: str, accept: str) -> Tuple[bytes, Dict[str, str]]:
        url = self._to_absolute_url(path_or_url)
        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                return self._single_get(url, accept=accept)
            except HTTPError as ex:
                last_err = ex
                if ex.code in TRANSIENT_HTTP_CODES and attempt < self.retries:
                    self._sleep_for_retry(attempt, f"HTTP {ex.code} for {url}")
                    continue
                # Retry once with auth query params for downloads; some instances require it.
                if self.api_key and ex.code in {401, 403, 404} and self._same_origin(url) and attempt < self.retries:
                    url_with_query = self._with_auth_query(url)
                    try:
                        return self._single_get(url_with_query, accept=accept)
                    except Exception as nested_ex:  # noqa: PERF203
                        last_err = nested_ex
                        if attempt < self.retries:
                            self._sleep_for_retry(attempt, f"retry after auth-query fallback for {url}")
                        continue
                raise
            except (URLError, TimeoutError) as ex:
                last_err = ex
                if attempt < self.retries:
                    self._sleep_for_retry(attempt, f"network error for {url}: {ex}")
                    continue
                raise
        if last_err is None:
            raise DownloadError(f"Failed to fetch {url}")
        raise DownloadError(f"Failed to fetch {url}: {last_err}")

    def _single_get(self, url: str, accept: str) -> Tuple[bytes, Dict[str, str]]:
        headers = {"Accept": accept, "User-Agent": "zubax-forum-exporter/1.0"}
        if self.api_key and self._same_origin(url):
            headers["Api-Key"] = self.api_key
            headers["Api-Username"] = self.api_username
        req = Request(url, method="GET", headers=headers)
        with urlopen(req, timeout=self.timeout) as resp:
            data = resp.read()
            return data, {k.lower(): v for k, v in resp.headers.items()}

    def _with_auth_query(self, url: str) -> str:
        if not self.api_key:
            return url
        parts = urlsplit(url)
        existing_q = dict(item.split("=", 1) if "=" in item else (item, "") for item in parts.query.split("&") if item)
        existing_q["api_key"] = self.api_key
        existing_q["api_username"] = self.api_username
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(existing_q), parts.fragment))

    def _to_absolute_url(self, path_or_url: str) -> str:
        if re.match(r"^https?://", path_or_url, flags=re.IGNORECASE):
            return path_or_url
        return urljoin(self.origin + "/", path_or_url)

    def _same_origin(self, url: str) -> bool:
        return urlsplit(url).netloc.lower() == urlsplit(self.origin).netloc.lower()

    def _sleep_for_retry(self, attempt: int, reason: str) -> None:
        delay = min(60.0, 0.5 * (2**attempt))
        LOGGER.info("[retry] %s; sleeping %.1fs", reason, delay)
        time.sleep(delay)


class CookedParser(HTMLParser):
    def __init__(self, origin: str) -> None:
        super().__init__(convert_charrefs=True)
        self.origin = origin
        self.info = CookedUploadInfo()
        self._seen_urls: Set[str] = set()
        self._current_lightbox_urls: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attrs_map = {k: v for k, v in attrs if v is not None}
        if tag == "a":
            classes = set((attrs_map.get("class") or "").split())
            href = attrs_map.get("href")
            data_download = attrs_map.get("data-download-href")
            urls: List[str] = []
            if href:
                u = canonicalize_upload_url(href, self.origin)
                if u:
                    self._register_url(u)
                    urls.append(u)
            if data_download:
                u = canonicalize_upload_url(data_download, self.origin)
                if u:
                    self._register_url(u)
                    urls.append(u)
            if "lightbox" in classes:
                self._current_lightbox_urls = urls
            else:
                self._current_lightbox_urls = []
            return

        if tag == "img":
            src = attrs_map.get("src")
            if src:
                u = canonicalize_upload_url(src, self.origin)
                if u:
                    self._register_url(u)
            sha1 = attrs_map.get("data-base62-sha1")
            if sha1 and self._current_lightbox_urls:
                bucket = self.info.sha1_to_urls.setdefault(sha1, [])
                for u in self._current_lightbox_urls:
                    if u not in bucket:
                        bucket.append(u)
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current_lightbox_urls = []

    def _register_url(self, url: str) -> None:
        if url in self._seen_urls:
            return
        self._seen_urls.add(url)
        self.info.ordered_urls.append(url)
        base = os.path.basename(urlsplit(url).path)
        if base:
            self.info.basename_to_urls.setdefault(base, []).append(url)


def sanitize_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-_.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned or "topic"


def canonicalize_upload_url(raw_ref: str, origin: str) -> Optional[str]:
    if raw_ref.startswith("upload://"):
        return None
    if not (raw_ref.startswith("/uploads/") or "/uploads/" in raw_ref):
        return None
    if not re.match(r"^https?://", raw_ref, flags=re.IGNORECASE):
        raw_ref = urljoin(origin + "/", raw_ref)
    parts = urlsplit(raw_ref)
    if "/uploads/" not in parts.path:
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def canonicalize_attachment_key(ref: str, origin: str) -> Optional[str]:
    if ref.startswith("upload://"):
        return ref
    return canonicalize_upload_url(ref, origin)


def parse_topic_ref(url_or_path: str, origin: str) -> Optional[Tuple[int, Optional[int], str, str]]:
    absolute = urljoin(origin + "/", url_or_path)
    parts = urlsplit(absolute)
    if parts.netloc.lower() != urlsplit(origin).netloc.lower():
        return None
    segments = [seg for seg in parts.path.split("/") if seg]
    if not segments or segments[0] != "t":
        return None

    topic_idx = None
    topic_id = None
    for i, seg in enumerate(segments[1:], start=1):
        if seg.isdigit():
            topic_idx = i
            topic_id = int(seg)
            break
    if topic_id is None or topic_idx is None:
        return None

    post_number: Optional[int] = None
    if topic_idx + 1 < len(segments) and segments[topic_idx + 1].isdigit():
        post_number = int(segments[topic_idx + 1])
    normalized = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return topic_id, post_number, parts.fragment, normalized


def split_trailing_url_punct(token: str) -> Tuple[str, str]:
    idx = len(token)
    while idx > 0 and token[idx - 1] in TRAILING_URL_PUNCT:
        idx -= 1
    return token[:idx], token[idx:]


def parse_cooked_upload_info(cooked: str, origin: str) -> CookedUploadInfo:
    parser = CookedParser(origin)
    parser.feed(cooked or "")
    return parser.info


def label_from_markdown_target(raw: str, target: str) -> Optional[str]:
    escaped_target = re.escape(target)
    pattern = re.compile(rf"!\[([^\]]+)\]\({escaped_target}\)|\[([^\]]+)\]\({escaped_target}\)")
    match = pattern.search(raw)
    if not match:
        return None
    raw_label = match.group(1) or match.group(2) or ""
    label = raw_label.split("|", 1)[0].strip()
    if not label:
        return None
    return label


def extract_attachment_mentions(raw: str, origin: str) -> Dict[str, AttachmentMention]:
    out: Dict[str, AttachmentMention] = {}
    for match in UPLOAD_REF_PATTERN.finditer(raw):
        text_ref = match.group(0)
        canonical = canonicalize_attachment_key(text_ref, origin)
        if not canonical:
            continue
        token: Optional[str]
        if canonical.startswith("upload://"):
            token = canonical[len("upload://") :]
        else:
            path = urlsplit(canonical).path
            token = os.path.basename(path) if "/uploads/short-url/" in path else None
        mention = out.get(canonical)
        if mention is None:
            mention = AttachmentMention(canonical_key=canonical, token=token)
            out[canonical] = mention
        label = label_from_markdown_target(raw, text_ref)
        if label and label not in mention.labels:
            mention.labels.append(label)
    return out


def extract_internal_topic_ids(text: str, origin: str) -> Set[int]:
    found: Set[int] = set()
    for match in TOPIC_LINK_PATTERN.finditer(text):
        token = match.group(0)
        core, _ = split_trailing_url_punct(token)
        parsed = parse_topic_ref(core, origin)
        if not parsed:
            continue
        topic_id, _, _, _ = parsed
        found.add(topic_id)
    return found


def safe_filename_from_url(url: str) -> Optional[str]:
    path = urlsplit(url).path
    name = os.path.basename(path)
    if not name:
        return None
    name = sanitize_slug(name)
    return name or None


def extension_from_content_type(content_type: str) -> str:
    if not content_type:
        return ""
    ctype = content_type.split(";", 1)[0].strip().lower()
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
        "application/zip": ".zip",
        "application/json": ".json",
        "application/octet-stream": ".bin",
    }
    return mapping.get(ctype, "")


def dedupe_filename(target_dir: Path, proposed: str) -> str:
    stem, ext = os.path.splitext(proposed)
    candidate = proposed
    i = 2
    while (target_dir / candidate).exists():
        candidate = f"{stem}-{i}{ext}"
        i += 1
    return candidate


def build_attachment_candidates(
    mention: AttachmentMention,
    cooked_info: CookedUploadInfo,
    origin: str,
) -> List[str]:
    candidates: List[str] = []
    seen: Set[str] = set()

    def add(url: str) -> None:
        canonical = canonicalize_upload_url(url, origin)
        if not canonical or canonical in seen:
            return
        seen.add(canonical)
        candidates.append(canonical)

    if mention.canonical_key.startswith("upload://"):
        token = mention.token or mention.canonical_key[len("upload://") :]
        token_no_ext = token.rsplit(".", 1)[0]
        add(urljoin(origin + "/", f"/uploads/short-url/{token}"))
        for u in cooked_info.ordered_urls:
            if token in u or token_no_ext in u:
                add(u)
        for u in cooked_info.basename_to_urls.get(token, []):
            add(u)
        for u in cooked_info.sha1_to_urls.get(token_no_ext, []):
            add(u)
        return candidates

    add(mention.canonical_key)
    path = urlsplit(mention.canonical_key).path
    if "/uploads/short-url/" in path:
        token = os.path.basename(path)
        token_no_ext = token.rsplit(".", 1)[0]
        for u in cooked_info.ordered_urls:
            if token in u or token_no_ext in u:
                add(u)
        for u in cooked_info.sha1_to_urls.get(token_no_ext, []):
            add(u)
    return candidates


def rewrite_attachments(raw: str, origin: str, local_by_key: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        ref = match.group(0)
        canonical = canonicalize_attachment_key(ref, origin)
        if not canonical:
            return ref
        return local_by_key.get(canonical, ref)

    return UPLOAD_REF_PATTERN.sub(repl, raw)


def rewrite_topic_links(raw: str, origin: str, topic_filename_by_id: Dict[int, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(0)
        core, suffix = split_trailing_url_punct(token)
        parsed = parse_topic_ref(core, origin)
        if not parsed:
            return token
        topic_id, post_number, fragment, _ = parsed
        local = topic_filename_by_id.get(topic_id)
        if not local:
            return token
        anchor = ""
        if post_number is not None:
            anchor = f"#post-{post_number}"
        elif fragment:
            anchor = f"#{fragment}"
        return f"{local}{anchor}{suffix}"

    return TOPIC_LINK_PATTERN.sub(repl, raw)


def fetch_topic(client: ForumClient, topic_id: int, origin: str) -> TopicRecord:
    topic_json = client.get_json(f"/t/{topic_id}.json")
    slug = topic_json.get("slug") or f"topic-{topic_id}"
    title = topic_json.get("title") or slug
    original_url = f"{origin}/t/{slug}/{topic_json.get('id', topic_id)}"

    stream = topic_json.get("post_stream", {}).get("stream", [])
    if not stream:
        stream = [post.get("id") for post in topic_json.get("post_stream", {}).get("posts", []) if post.get("id")]

    posts: List[PostRecord] = []
    linked: Set[int] = set()
    attachment_mentions: Dict[str, AttachmentMention] = {}
    for post_id in stream:
        post_json = client.get_json(f"/posts/{post_id}.json")
        raw = post_json.get("raw") or ""
        cooked = post_json.get("cooked") or ""
        post = PostRecord(
            id=int(post_json["id"]),
            post_number=int(post_json.get("post_number", 0)),
            username=post_json.get("username") or "unknown",
            created_at=post_json.get("created_at") or "",
            raw=raw,
            cooked=cooked,
        )
        posts.append(post)

        linked.update(extract_internal_topic_ids(raw, origin))
        for key, mention in extract_attachment_mentions(raw, origin).items():
            existing = attachment_mentions.get(key)
            if existing is None:
                attachment_mentions[key] = mention
            else:
                for label in mention.labels:
                    if label not in existing.labels:
                        existing.labels.append(label)
    linked.discard(topic_id)
    return TopicRecord(
        id=int(topic_json.get("id", topic_id)),
        slug=slug,
        title=title,
        original_url=original_url,
        posts=posts,
        linked_topic_ids=linked,
        attachment_mentions=attachment_mentions,
    )


def choose_attachment_filename(
    mention: AttachmentMention,
    source_url: str,
    headers: Dict[str, str],
) -> str:
    if mention.labels:
        label = sanitize_slug(mention.labels[0])
        if label and "." in label:
            return label
    from_url = safe_filename_from_url(source_url)
    if from_url:
        return from_url
    if mention.token:
        token_name = sanitize_slug(mention.token)
        if token_name:
            return token_name
    ext = extension_from_content_type(headers.get("content-type", ""))
    return f"attachment{ext or '.bin'}"


def download_topic_attachments(
    topic: TopicRecord,
    client: ForumClient,
    output_dir: Path,
    topic_basename: str,
    verbose: bool,
) -> Dict[str, str]:
    attachment_dir = output_dir / f"{topic_basename}.attachments"
    attachment_dir.mkdir(parents=True, exist_ok=True)

    # Build a per-topic cooked index once to map upload:// tokens to downloadable URLs.
    cooked_info = CookedUploadInfo()
    for post in topic.posts:
        info = parse_cooked_upload_info(post.cooked, client.origin)
        for u in info.ordered_urls:
            if u not in cooked_info.ordered_urls:
                cooked_info.ordered_urls.append(u)
        for k, vals in info.sha1_to_urls.items():
            bucket = cooked_info.sha1_to_urls.setdefault(k, [])
            for v in vals:
                if v not in bucket:
                    bucket.append(v)
        for k, vals in info.basename_to_urls.items():
            bucket = cooked_info.basename_to_urls.setdefault(k, [])
            for v in vals:
                if v not in bucket:
                    bucket.append(v)

    local_by_key: Dict[str, str] = {}
    for mention in topic.attachment_mentions.values():
        candidates = build_attachment_candidates(mention, cooked_info, client.origin)
        if not candidates:
            message = f"attachment unresolved: {mention.canonical_key} (no candidate URLs)"
            topic.unresolved.append(message)
            LOGGER.error(message)
            continue

        saved = False
        first_error: Optional[str] = None
        for candidate in candidates:
            try:
                payload, headers = client.get_bytes(candidate)
                filename = choose_attachment_filename(mention, candidate, headers)
                filename = dedupe_filename(attachment_dir, filename)
                out_path = attachment_dir / filename
                out_path.write_bytes(payload)
                rel_path = f"{attachment_dir.name}/{filename}"
                local_by_key[mention.canonical_key] = rel_path
                saved = True
                LOGGER.info("[attachment] %s -> %s", mention.canonical_key, rel_path)
                break
            except Exception as ex:  # noqa: PERF203
                if first_error is None:
                    first_error = str(ex)
                continue
        if not saved:
            message = (
                f"attachment unresolved: {mention.canonical_key} "
                f"(tried {len(candidates)} URL(s); first error: {first_error})"
            )
            topic.unresolved.append(message)
            LOGGER.error(message)
    return local_by_key


def render_topic_markdown(
    topic: TopicRecord,
    output_path: Path,
    local_attachment_by_key: Dict[str, str],
    topic_filename_by_id: Dict[int, str],
) -> None:
    lines: List[str] = []
    lines.append(f"# {topic.title}")
    lines.append("")
    lines.append(f"- Original URL: {topic.original_url}")
    lines.append(f"- Topic ID: {topic.id}")
    lines.append("")

    for post in topic.posts:
        lines.append(f'<a id="post-{post.post_number}"></a>')
        lines.append(f"## Post {post.post_number} - @{post.username} - {post.created_at}")
        lines.append("")
        rewritten = rewrite_attachments(post.raw, topic.original_url, local_attachment_by_key)
        rewritten = rewrite_topic_links(rewritten, topic.original_url, topic_filename_by_id)
        lines.append(rewritten.rstrip())
        lines.append("")
        lines.append("---")
        lines.append("")

    linked_local = sorted(topic_filename_by_id[tid] for tid in topic.linked_topic_ids if tid in topic_filename_by_id)
    if linked_local:
        lines.append("## Linked Topics")
        lines.append("")
        for item in linked_local:
            lines.append(f"- [{item}]({item})")
        lines.append("")

    if topic.unresolved:
        lines.append("## Unresolved Items")
        lines.append("")
        for item in topic.unresolved:
            lines.append(f"- {item}")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("thread_url", help="Root thread URL, e.g. https://forum.zubax.com/t/some-slug/123")
    parser.add_argument("--output-dir", default=".", help="Directory where Markdown + attachments are written")
    parser.add_argument(
        "--api-username",
        default=None,
        help="Discourse API username (fallback: ZUBAX_FORUM_API_USERNAME, then 'system')",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Discourse API key (fallback: ZUBAX_FORUM_API_KEY, then ZUBAX_FORUM_API_TOKEN)",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=-1,
        help="Recursive fetch depth for linked threads (-1 = unlimited, 0 = root only)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=10, help="HTTP retry count for transient failures")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging (default: INFO)")
    return parser.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)

    api_key = args.api_key or os.getenv("ZUBAX_FORUM_API_KEY") or os.getenv("ZUBAX_FORUM_API_TOKEN")
    api_username = args.api_username or os.getenv("ZUBAX_FORUM_API_USERNAME") or "system"
    origin_parts = urlsplit(args.thread_url)
    if not origin_parts.scheme or not origin_parts.netloc:
        LOGGER.error("thread URL must be absolute (with scheme and host): %s", args.thread_url)
        return 2
    origin = f"{origin_parts.scheme}://{origin_parts.netloc}"

    parsed = parse_topic_ref(args.thread_url, origin=origin)
    if not parsed:
        LOGGER.error("unable to parse topic ID from URL: %s", args.thread_url)
        return 2
    root_topic_id, _, _, _ = parsed

    if not api_key:
        LOGGER.warning(
            "ZUBAX_FORUM_API_KEY/ZUBAX_FORUM_API_TOKEN is not set; " "private topics/attachments may be inaccessible."
        )

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    client = ForumClient(
        origin=origin,
        api_key=api_key,
        api_username=api_username,
        timeout=args.timeout,
        retries=args.retries,
        verbose=args.verbose,
    )

    queue: List[Tuple[int, int]] = [(root_topic_id, 0)]
    visited: Set[int] = set()
    topics: Dict[int, TopicRecord] = {}
    unresolved_topics: List[str] = []

    while queue:
        topic_id, depth = queue.pop(0)
        if topic_id in visited:
            continue
        visited.add(topic_id)

        LOGGER.info("[topic] fetching %s (depth %s)", topic_id, depth)
        try:
            topic = fetch_topic(client, topic_id, origin)
        except Exception as ex:
            msg = f"topic unresolved: {topic_id} ({ex})"
            if topic_id == root_topic_id:
                LOGGER.error("failed to fetch root topic %s: %s", topic_id, ex)
                return 1
            unresolved_topics.append(msg)
            LOGGER.error(msg)
            continue

        topics[topic.id] = topic
        if args.max_depth != -1 and depth >= args.max_depth:
            continue
        for linked in sorted(topic.linked_topic_ids):
            if linked not in visited:
                queue.append((linked, depth + 1))

    topic_filename_by_id: Dict[int, str] = {}
    topic_basename_by_id: Dict[int, str] = {}
    for tid, topic in topics.items():
        base = f"{tid}-{sanitize_slug(topic.slug)}"
        topic_basename_by_id[tid] = base
        topic_filename_by_id[tid] = f"{base}.md"

    if unresolved_topics and root_topic_id in topics:
        topics[root_topic_id].unresolved.extend(unresolved_topics)

    for tid, topic in topics.items():
        base = topic_basename_by_id[tid]
        attachment_map = download_topic_attachments(topic, client, output_dir, base, args.verbose)
        render_topic_markdown(
            topic=topic,
            output_path=output_dir / topic_filename_by_id[tid],
            local_attachment_by_key=attachment_map,
            topic_filename_by_id=topic_filename_by_id,
        )
        LOGGER.info("[write] %s", topic_filename_by_id[tid])

    LOGGER.info("Exported %s topic(s) into %s", len(topics), output_dir)
    unresolved_total = sum(len(t.unresolved) for t in topics.values())
    if unresolved_total:
        LOGGER.error("Unresolved items: %s (see '## Unresolved Items' in output Markdown)", unresolved_total)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

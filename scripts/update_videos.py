#!/usr/bin/env python3
"""Refresh data/videos.json from BossKronik's public YouTube uploads.

Prefers the public channel RSS feed (no API key). If that feed does not
return Atom XML, falls back to the public /videos tab, which lists only
videos YouTube is already showing on the channel. Scheduled and private
videos are not included. The file is rewritten only when the public list
or the channel counts actually change.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

CHANNEL_ID = "UCQzwqsRr5IfwatqzGlUWxyg"
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
VIDEOS_PAGE = "https://www.youtube.com/@bosskronik/videos"
ABOUT_PAGE = "https://www.youtube.com/@bosskronik/about"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "videos.json"
UA = "Mozilla/5.0 (compatible; BossKronikSite/1.0; +https://bosskronik.github.io/bosskronik/)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def fetch(url: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last_error: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code in (404, 400, 401, 403):
                raise
        except urllib.error.URLError as error:
            last_error = error
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def clean_title(title: str) -> str:
    cleaned = re.sub(r"\bPOV\b", "", title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+\|", " |", cleaned)
    cleaned = re.sub(r"\|\s+", "| ", cleaned)
    return cleaned.strip(" -|")


def video_record(video_id: str, title: str, published: str | None = None) -> dict:
    record = {
        "id": video_id,
        "title": clean_title(title),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumb": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    }
    if published:
        record["published"] = published
    return record


def parse_rss(payload: bytes) -> list[dict]:
    root = ElementTree.fromstring(payload)
    videos = []
    for entry in root.findall("atom:entry", NS):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=NS) or "").strip()
        title = (entry.findtext("atom:title", default="", namespaces=NS) or "").strip()
        published = (entry.findtext("atom:published", default="", namespaces=NS) or "").strip()
        if not video_id or not title:
            continue
        videos.append(video_record(video_id, title, published or None))
    return videos


def extract_initial_data(html: str) -> dict:
    marker = "ytInitialData"
    start = html.find(marker)
    if start < 0:
        raise ValueError("ytInitialData missing")
    start = html.find("{", start)
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(html[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start : index + 1])
    raise ValueError("ytInitialData was not closed")


def text_content(node: dict | None) -> str:
    if not isinstance(node, dict):
        return ""
    if isinstance(node.get("content"), str):
        return node["content"]
    if isinstance(node.get("simpleText"), str):
        return node["simpleText"]
    return ""


def parse_videos_page(html: str) -> list[dict]:
    data = extract_initial_data(html)
    found: list[dict] = []
    seen: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            metadata = node.get("metadata")
            if isinstance(metadata, dict) and "lockupMetadataViewModel" in metadata:
                lockup = metadata["lockupMetadataViewModel"]
                title = text_content(lockup.get("title"))
                video_ids = []

                def find_ids(item) -> None:
                    if isinstance(item, dict):
                        video_id = item.get("videoId")
                        if isinstance(video_id, str) and len(video_id) == 11:
                            video_ids.append(video_id)
                        for value in item.values():
                            find_ids(value)
                    elif isinstance(item, list):
                        for value in item:
                            find_ids(value)

                find_ids(node)
                video_id = next((item for item in video_ids if item not in seen), "")
                if video_id and title:
                    seen.add(video_id)
                    found.append(video_record(video_id, title))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def parse_channel_counts(*pages: str) -> dict:
    counts = {}
    combined = "\n".join(pages)
    patterns = {
        "subs": r'"content":"([\d.,]+[KMB]?) subscribers"',
        "videos": r'"content":"([\d.,]+) videos"',
        "views": r'"viewCountText":"([\d.,]+) views"',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, combined)
        if match:
            counts[key] = match.group(1)
    return counts


def load_existing() -> dict:
    if not OUT.exists():
        return {}
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def signature(payload: dict) -> str:
    stable = {
        "videos": payload.get("videos") or [],
        "channel": payload.get("channel") or {},
    }
    return json.dumps(stable, ensure_ascii=False, sort_keys=True)


def main() -> int:
    fetched_via = "rss"
    try:
        rss = fetch(RSS_URL)
        videos = parse_rss(rss)
        if not videos:
            raise ValueError("RSS feed had no entries")
    except Exception as error:
        print(f"RSS unavailable ({error}). Using the public videos tab.", file=sys.stderr)
        fetched_via = "channel-videos-page"
        page = fetch(VIDEOS_PAGE).decode("utf-8", "replace")
        videos = parse_videos_page(page)
        if not videos:
            print("No public videos found. Leaving data/videos.json unchanged.", file=sys.stderr)
            return 1
        about = ""
        try:
            about = fetch(ABOUT_PAGE).decode("utf-8", "replace")
        except Exception as about_error:
            print(f"Channel about page skipped ({about_error}).", file=sys.stderr)
        counts = parse_channel_counts(page, about)
    else:
        counts = {}
        try:
            about = fetch(ABOUT_PAGE).decode("utf-8", "replace")
            counts = parse_channel_counts(about)
        except Exception as about_error:
            print(f"Channel counts skipped ({about_error}).", file=sys.stderr)

    existing = load_existing()
    channel = dict(existing.get("channel") or {})
    channel.update(counts)
    videos = videos[:15]
    payload = {
        "channel_id": CHANNEL_ID,
        "source": RSS_URL,
        "fetched_via": fetched_via,
        "channel": channel,
        "videos": videos,
    }
    if existing and signature(existing) == signature(payload):
        print(f"No change ({len(videos)} public videos via {fetched_via}).")
        return 0

    payload["updated"] = datetime.now(timezone.utc).isoformat()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(videos)} public videos via {fetched_via}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

import requests

from .core import Source, dated, duration_text, source_url
from .gemini import Gemini, ServiceError, search_enabled
from .media import Downloads, MediaError, run

SEED_CHANNELS = ("DarkViperAU", "TGgta6", "TGG_", "GTASeriesVideos")
TWITCH_CHANNELS = ("darkviperau", "tgg_gta", "gtaseriesvideos")
TOPICS = ("GTA 6 gameplay first playthrough", "GTA VI mission driving funny reaction", "GTA VI podcast radio theories")
YOUTUBE = "https://www.googleapis.com/youtube/v3"
TWITCH = "https://api.twitch.tv/helix"
GTA_VI = re.compile(r"\b(?:gta|grand theft auto)\s*(?:vi|6)\b", re.IGNORECASE)


def response_json(response: requests.Response, label: str) -> dict:
    if not response.ok:
        raise ValueError(f"{label} returned HTTP {response.status_code}")
    return response.json()


def add(sources: dict[str, Source], item: Source | None) -> None:
    if item and (item.key not in sources or item.duration > sources[item.key].duration):
        sources[item.key] = item


def youtube_discovery(sources: dict[str, Source], api_key: str, days: int) -> None:
    def api(path: str, **params) -> list[dict]:
        try:
            data = response_json(requests.get(f"{YOUTUBE}/{path}", params={**params, "key": api_key}, timeout=25), "YouTube API")
        except requests.RequestException:
            raise ValueError("YouTube API connection failed") from None
        return data.get("items", [])

    ids = [item.key.split(":", 1)[1] for item in sources.values() if item.platform == "youtube"]
    until = dated(days)
    for topic in TOPICS:
        rows = api("search", part="snippet", type="video", order="date", maxResults=18, publishedAfter=until, q=topic)
        ids.extend(row["id"]["videoId"] for row in rows)
    for handle in SEED_CHANNELS:
        channels = api("channels", part="contentDetails", forHandle=handle)
        for channel in channels:
            uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
            rows = api("playlistItems", part="snippet", playlistId=uploads, maxResults=12)
            ids.extend(row["snippet"]["resourceId"]["videoId"] for row in rows if row["snippet"]["publishedAt"] >= until)
    unique = list(dict.fromkeys(ids))
    for index in range(0, len(unique), 50):
        rows = api("videos", part="snippet,contentDetails,status,statistics", id=",".join(unique[index:index + 50]))
        for row in rows:
            snippet = row["snippet"]
            status = row["status"]
            if status.get("privacyStatus") != "public" or snippet.get("liveBroadcastContent") in {"live", "upcoming"}:
                sources.pop(f"youtube:{row['id']}", None)
                continue
            item = source_url(f"https://www.youtube.com/watch?v={row['id']}")
            if item is None:
                continue
            item.title = snippet.get("title", "")
            item.creator = snippet.get("channelTitle", "")
            item.published = snippet.get("publishedAt", "")
            item.duration = duration_text(row["contentDetails"].get("duration", ""))
            item.views = int(row.get("statistics", {}).get("viewCount") or 0)
            item.rights = "YouTube Creative Commons (platform label)" if status.get("license") == "creativeCommon" else "Standard YouTube license"
            add(sources, item)


def twitch_discovery(sources: dict[str, Source], client: str, secret: str, days: int) -> None:
    try:
        token = response_json(requests.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": client, "client_secret": secret, "grant_type": "client_credentials",
        }, timeout=25), "Twitch OAuth")["access_token"]
    except requests.RequestException:
        raise ValueError("Twitch OAuth connection failed") from None

    def api(path: str, **params) -> list[dict]:
        try:
            return response_json(requests.get(f"{TWITCH}/{path}", params=params, headers={
                "Client-Id": client, "Authorization": f"Bearer {token}",
            }, timeout=25), "Twitch Helix").get("data", [])
        except requests.RequestException:
            raise ValueError("Twitch Helix connection failed") from None

    categories = api("search/categories", query="Grand Theft Auto VI", first=12)
    category = next((row["id"] for row in categories
                     if row["name"].casefold() in {"grand theft auto vi", "grand theft auto 6", "gta vi"}), None)
    if not category:
        return
    anchors: dict[str, list[float]] = defaultdict(list)
    for clip in api("clips", game_id=category, first=45, started_at=dated(days), ended_at=dated()):
        item = source_url(clip["url"])
        if item is None:
            continue
        video_id = clip.get("video_id")
        offset = clip.get("vod_offset")
        item.title = clip.get("title", "")
        item.creator = clip.get("broadcaster_name", "")
        item.duration = float(clip.get("duration") or 0)
        item.views = int(clip.get("view_count") or 0)
        item.published = clip.get("created_at", "")
        if video_id and type(offset) is int:
            item.parent = f"twitch-vod:{video_id}"
            item.parent_offset = float(offset)
            anchors[item.parent].append(float(offset))
        add(sources, item)
    videos = api("videos", game_id=category, type="archive", sort="views", period="week", first=20)
    if anchors:
        videos.extend(api("videos", id=[key.split(":", 1)[1] for key in list(anchors)[:15]]))
    for row in videos:
        item = source_url(f"https://www.twitch.tv/videos/{row['id']}")
        if item is None:
            continue
        item.title = row.get("title", "")
        item.creator = row.get("user_name", "")
        item.published = row.get("published_at", "")
        item.duration = duration_text(row.get("duration", ""))
        item.views = int(row.get("view_count") or 0)
        item.anchors = anchors[item.key]
        add(sources, item)


def discovered_with_gemini(gemini: Gemini, days: int) -> list[Source]:
    answer = gemini.search(
        f"Today is {dated()[:10]}. Find actual public GTA VI gameplay archives and Twitch clip links from the past {days} days, "
        "plus substantive GTA VI podcast or theory discussions. Search YouTube and Twitch separately. Include launch "
        f"broadcasts from {', '.join(SEED_CHANNELS)} if already archived, as well as newly discovered creators. "
        "Find multiple different channels and quiet as well as action-heavy footage. Return the actual canonical direct "
        "YouTube watch, Twitch clips.twitch.tv, or Twitch /videos/ URLs from search results, one per line, up to 28. "
        "Exclude category pages, channel links, trailers, GTA V and GTA Online, teasers, and upcoming events."
    )
    links = list(dict.fromkeys(unquote(link.rstrip(").,;]")) for link in re.findall(r'https://[^\s<>"\]]+', answer)))
    sources = [item for link in links if (item := source_url(link))]
    citations = [link for link in links if link.startswith("https://vertexaisearch.cloud.google.com/grounding-api-redirect/")]
    if citations:
        with ThreadPoolExecutor(max_workers=5) as pool:
            sources.extend(item for item in pool.map(grounding_source, citations[:18]) if item)
    return sources


def tavily_discovery(api_key: str) -> list[Source]:
    """Search public video pages using two basic searches."""
    found: list[Source] = []
    queries = (
        ("GTA VI launch gameplay Twitch clips and stream archives first playthrough", ["twitch.tv"]),
        ("GTA VI YouTube gameplay full stream archive funny moments playthrough", ["youtube.com", "youtu.be"]),
    )
    for query, domains in queries:
        try:
            response = requests.post("https://api.tavily.com/search", timeout=(8, 22),
                                     headers={"Authorization": f"Bearer {api_key}"},
                                     json={"query": query, "include_domains": domains, "search_depth": "basic",
                                           "auto_parameters": False, "time_range": "week", "max_results": 12})
        except requests.RequestException:
            raise ValueError("Tavily connection failed") from None
        rows = response_json(response, "Tavily search").get("results", [])
        found.extend(item for row in rows if (item := source_url(row.get("url") or "")))
    return found


def grounding_source(link: str) -> Source | None:
    parts = urlsplit(link)
    if parts.netloc != "vertexaisearch.cloud.google.com" or parts.scheme != "https" or not parts.path.startswith("/grounding-api-redirect/"):
        return None
    try:
        with requests.get(link, allow_redirects=False, stream=True, timeout=(5, 8)) as response:
            return source_url(response.headers.get("Location", "")) if response.is_redirect else None
    except requests.RequestException:
        return None


def youtube_fallback(downloads: Downloads, topic: str) -> list[Source]:
    response = run(downloads.ytdlp(f"ytsearch15:{topic}", ["--flat-playlist", "--dump-single-json"]), timeout=55)
    rows = json.loads(response).get("entries", [])
    return [item for row in rows if row and (item := source_url(f"https://www.youtube.com/watch?v={row.get('id', '')}"))]


def listed_twitch_source(row: dict, channel: str, *, clip: bool, earliest: str) -> Source | None:
    game = (row.get("game") or {}).get("name") or ""
    title = row.get("title") or ""
    if not GTA_VI.search(game) and (game.casefold() not in {"", "just chatting", "talk shows & podcasts", "special events"}
                                   or not GTA_VI.search(title)):
        return None
    published = row.get("publishedAt") or row.get("createdAt") or ""
    if published < earliest or row.get("status") == "RECORDING":
        return None
    url = (row.get("url") or "") if clip else f"https://www.twitch.tv/videos/{row.get('id', '')}"
    item = source_url(url)
    if item is None:
        return None
    creator = (row.get("broadcaster") if clip else row.get("owner")) or {}
    item.creator = creator.get("displayName") or channel
    item.title = title
    item.duration = float(row.get("durationSeconds" if clip else "lengthSeconds") or 0)
    item.views = int(row.get("viewCount") or 0)
    item.published = published
    return item if item.duration >= 10 else None


def twitch_fallback(sources: dict[str, Source], report: list[str], days: int) -> None:
    earliest = dated(days)

    def list_channel(channel: str) -> tuple[list[Source], list[str]]:
        items: list[Source] = []
        errors = []
        lists = ((True, "clips", 24, ["--period", "last_week"]), (False, "videos", 12, ["--skip-live"]))
        for clip, command, limit, args in lists:
            try:
                cmd = ["twitch-dl", command, "--limit"]
                try:
                    output = json.loads(run([*cmd, str(limit), *args, "--json", channel], timeout=30))
                except MediaError as error:
                    if "service error" not in str(error).casefold():
                        raise
                    output = json.loads(run([*cmd, "8", *args, "--json", channel], timeout=25))
                rows = output if clip else output["videos"]
                for row in rows:
                    item = listed_twitch_source(row, channel, clip=clip, earliest=earliest)
                    if item:
                        items.append(item)
            except (KeyError, MediaError, ValueError) as error:
                errors.append(f"Twitch watchlist {channel} {command}: {error}")
        return items, errors

    count = 0
    with ThreadPoolExecutor(max_workers=len(TWITCH_CHANNELS)) as pool:
        for items, errors in pool.map(list_channel, TWITCH_CHANNELS):
            report.extend(errors)
            count += len(items)
            for item in items:
                add(sources, item)
    report.append(f"Twitch watchlist found {count} recent GTA VI clips and archives.")


def discover(gemini: Gemini, downloads: Downloads, report: list[str], *, days: int = 7) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    if search_enabled():
        try:
            for item in discovered_with_gemini(gemini, days):
                add(sources, item)
        except (ServiceError, ValueError) as error:
            report.append(f"Gemini web discovery: {error}")
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            for item in tavily_discovery(tavily_key):
                add(sources, item)
        except (KeyError, ValueError) as error:
            report.append(f"Free web discovery: {error}")
    youtube_key = os.getenv("YOUTUBE_API_KEY")
    twitch_client = os.getenv("TWITCH_CLIENT_ID")
    twitch_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if youtube_key:
        try:
            youtube_discovery(sources, youtube_key, days)
        except (KeyError, ValueError) as error:
            report.append(f"YouTube discovery: {error}")
    if twitch_client and twitch_secret:
        try:
            twitch_discovery(sources, twitch_client, twitch_secret, days)
        except (KeyError, ValueError) as error:
            report.append(f"Twitch discovery: {error}")
    if not any(item.platform.startswith("twitch") and item.duration for item in sources.values()):
        twitch_fallback(sources, report, days)
    if not any(item.platform == "youtube" for item in sources.values()):
        for topic in (TOPICS[0], TOPICS[2]):
            try:
                for item in youtube_fallback(downloads, topic):
                    add(sources, item)
            except (MediaError, ValueError) as error:
                report.append(f"YouTube search fallback: {error}")
    youtube_links = sum(item.platform == "youtube" for item in sources.values())
    link_label = "link" if youtube_links == 1 else "links"
    report.append(f"YouTube discovery collected {youtube_links} video {link_label}.")
    youtube_blocks = 0
    skipped = 0
    for item in list(sources.values()):
        if not item.duration:
            if item.platform == "youtube" and youtube_blocks >= 2:
                skipped += 1
                sources.pop(item.key, None)
                continue
            try:
                downloads.metadata(item)
            except (MediaError, ValueError) as error:
                if item.platform == "youtube" and "sign in to confirm" in str(error).casefold():
                    youtube_blocks += 1
                report.append(f"{item.key} metadata: {error}")
                sources.pop(item.key, None)
        if 0 < item.duration < 10:
            sources.pop(item.key, None)
    if skipped:
        report.append(f"Skipped {skipped} further YouTube metadata calls after repeated sign-in challenges.")
    # YouTube's replay graph is optional; the API and coverage sweep supply the baseline.
    replay_targets = sorted((item for item in sources.values() if item.platform == "youtube" and item.duration > 420
                             and not item.anchors and item.published), key=priority, reverse=True)
    for item in replay_targets[:4]:
        try:
            duration = item.duration
            downloads.metadata(item)
            item.duration = item.duration or duration
        except (MediaError, ValueError):
            continue
    eligible = {key: item for key, item in sources.items() if item.duration >= 10}
    if not eligible:
        try:
            gemini.check()
        except (ServiceError, ValueError) as error:
            report.append(f"Plain Gemini API request: {error}")
        else:
            report.append("A plain Gemini API request succeeded.")
    return eligible


def priority(item: Source) -> float:
    # Logarithmic reach provides a lead without letting follower count dominate.
    return math.log1p(item.views) + 3 * (item.published >= dated(2)) + 1.5 * (item.creator in SEED_CHANNELS) + bool(item.anchors)

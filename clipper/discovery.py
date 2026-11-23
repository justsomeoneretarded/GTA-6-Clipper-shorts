from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlencode, urlsplit

import requests

from .core import Source, dated, duration_text, source_cutoff, source_url
from .gemini import Gemini, ServiceError, search_enabled
from .media import Downloads, MediaError, run, youtube_session_blocked

SEARCH_TERMS = ("GTA 6", "GTA VI", "GTA", "GTA stream", "GTA game", "GTA live")
YOUTUBE = "https://www.googleapis.com/youtube/v3"
TWITCH = "https://api.twitch.tv/helix"
GTA_VI = re.compile(r"\b(?:gta|grand theft auto)\s*(?:vi|6)\b", re.IGNORECASE)


def response_json(response: requests.Response, label: str) -> dict:
    if not response.ok:
        raise ValueError(f"{label} returned HTTP {response.status_code}")
    return response.json()


def add(sources: dict[str, Source], item: Source | None) -> None:
    if not item:
        return
    previous = sources.get(item.key)
    if previous is None or item.recording or (not previous.recording and item.duration > previous.duration):
        if previous and item.recording:
            item.anchors.extend(anchor for anchor in previous.anchors if anchor < item.duration)
        sources[item.key] = item


def youtube_discovery(sources: dict[str, Source], api_key: str, days: int) -> None:
    def api(path: str, **params) -> list[dict]:
        try:
            data = response_json(requests.get(f"{YOUTUBE}/{path}", params={**params, "key": api_key}, timeout=25), "YouTube API")
        except requests.RequestException:
            raise ValueError("YouTube API connection failed") from None
        return data.get("items", [])

    ids = [item.key.split(":", 1)[1] for item in sources.values() if item.platform == "youtube"]
    until = source_cutoff(days)
    for topic in SEARCH_TERMS:
        rows = api("search", part="snippet", type="video", order="date", maxResults=18, publishedAfter=until, q=topic)
        ids.extend(row["id"]["videoId"] for row in rows)
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
    for clip in api("clips", game_id=category, first=45, started_at=source_cutoff(days), ended_at=dated()):
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
        "plus substantive GTA VI podcast or theory discussions. Search YouTube and Twitch separately using "
        f"these queries: {', '.join(SEARCH_TERMS)}. Include archived launches and clips from live channels. "
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
        ("GTA 6 GTA VI gameplay live stream clips", ["twitch.tv"]),
        ("GTA 6 GTA VI gameplay live stream clips", ["youtube.com", "youtu.be"]),
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


def youtube_search(downloads: Downloads, topic: str) -> list[Source]:
    # YouTube's results filter combines upload-date sorting and video results.
    query = urlencode({"search_query": topic, "sp": "CAISAhAB"})
    options = ["--flat-playlist", "--dump-single-json", "--playlist-end", "15"]
    try:
        result = downloads.ytdlp(f"https://www.youtube.com/results?{query}", options, timeout=55)
        rows = json.loads(result).get("entries", [])
    except MediaError as error:
        if youtube_session_blocked(error):
            raise
        rows = []
    if not rows:
        result = downloads.ytdlp(f"ytsearch15:{topic}", options, timeout=55)
        rows = json.loads(result).get("entries", [])
    sources = []
    for row in rows:
        if not row or row.get("live_status") in {"is_live", "is_upcoming"}:
            continue
        item = source_url(f"https://www.youtube.com/watch?v={row.get('id', '')}")
        if item:
            item.title = str(row.get("title") or "")
            item.creator = str(row.get("uploader") or "")
            item.duration = float(row.get("duration") or 0)
            item.views = int(row.get("view_count") or 0)
            sources.append(item)
    return sources


def listed_twitch_source(row: dict, channel: str, *, clip: bool, earliest: str) -> Source | None:
    game = (row.get("game") or {}).get("name") or ""
    title = row.get("title") or ""
    if not GTA_VI.search(game) and (game.casefold() not in {"", "just chatting", "talk shows & podcasts", "special events"}
                                   or not GTA_VI.search(title)):
        return None
    published = row.get("publishedAt") or row.get("createdAt") or ""
    if published < earliest:
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
    if clip:
        video_id = (row.get("video") or {}).get("id", "")
        offset = row.get("videoOffsetSeconds")
        parent = source_url(f"https://www.twitch.tv/videos/{video_id}")
        if parent and type(offset) in (int, float) and offset >= 0:
            item.parent = parent.key
            item.parent_offset = float(offset)
    if not clip and row.get("status") == "RECORDING":
        item.recording = True
        # Give Twitch time to publish the final segments of the current replay.
        item.duration = max(0, math.floor(item.duration) - 60)
        item.anchors.append(max(0, item.duration - 120))
    return item if item.duration >= 10 else None


def twitch_query(query: str) -> dict:
    try:
        from click import ClickException
        from httpx import HTTPError
        from twitchdl.twitch import gql_query
    except ImportError:
        raise ValueError("twitch-dl is unavailable for public Twitch search.") from None

    try:
        return gql_query(query)["data"]
    except (ClickException, HTTPError, KeyError, TypeError, ValueError):
        raise ValueError("Twitch did not return public search results.") from None


def twitch_search_channels() -> list[str]:
    fields = "channels { edges { item { ... on User { login stream { game { name } } } } } }"
    terms = [
        f'term{index}: searchFor(userQuery: {json.dumps(term)}, platform: "web") {{ {fields} }}'
        for index, term in enumerate(SEARCH_TERMS)
    ]
    channels: list[str] = []
    for result in twitch_query("{ " + "\n".join(terms) + " }").values():
        selected = 0
        for edge in result["channels"]["edges"]:
            item = edge.get("item") or {}
            channel = (item.get("login") or "").casefold()
            game = (((item.get("stream") or {}).get("game") or {}).get("name") or "")
            if GTA_VI.search(game) and re.fullmatch(r"[a-z0-9_]{2,25}", channel) and channel not in channels:
                channels.append(channel)
                selected += 1
                if selected == 2:
                    break
    return channels


def twitch_category() -> tuple[list[str], list[dict]]:
    query = """
    { game(name: "Grand Theft Auto VI") {
        streams(first: 8) { edges { node { broadcaster { login } } } }
        clips(first: 32, criteria: {period: LAST_DAY, sort: VIEWS_DESC}) {
            edges { node {
                title createdAt durationSeconds viewCount url videoOffsetSeconds game { name }
                broadcaster { login displayName }
                video { id title lengthSeconds viewCount publishedAt status game { name } owner { displayName } }
            } }
        }
    } }
    """
    game = twitch_query(query)["game"] or {}
    live_channels = []
    for edge in game.get("streams", {}).get("edges", []):
        channel = ((edge.get("node") or {}).get("broadcaster") or {}).get("login") or ""
        if re.fullmatch(r"[a-zA-Z0-9_]{2,25}", channel):
            live_channels.append(channel.casefold())
    return live_channels, [edge["node"] for edge in game.get("clips", {}).get("edges", []) if edge.get("node")]


def twitch_public_discovery(sources: dict[str, Source], report: list[str], days: int) -> None:
    earliest = source_cutoff(days)
    search_channels = []
    try:
        search_channels = twitch_search_channels()
        report.append(f"Twitch keyword searches found {len(search_channels)} live GTA VI channels.")
    except (KeyError, TypeError, ValueError) as error:
        report.append(f"Twitch keyword search: {error}")
    live_channels = []
    category_count = 0
    try:
        live_channels, clips = twitch_category()
        for row in clips:
            item = listed_twitch_source(row, "", clip=True, earliest=earliest)
            if item:
                add(sources, item)
                category_count += 1
                parent = listed_twitch_source(row.get("video") or {}, item.creator, clip=False, earliest=earliest)
                if parent:
                    add(sources, parent)
        report.append(f"Twitch GTA VI category listed {len(live_channels)} live channels and {category_count} recent clips.")
    except (KeyError, TypeError, ValueError) as error:
        report.append(f"Twitch category discovery: {error}")
    channels = list(dict.fromkeys((*search_channels, *live_channels)))

    def list_channel(channel: str) -> tuple[list[Source], list[str]]:
        items: list[Source] = []
        errors = []
        lists = ((True, "clips", 12, ["--period", "last_day"]),
                 (True, "clips", 24, ["--period", "last_week"]), (False, "videos", 12, []))
        for clip, command, limit, args in lists:
            if clip and any(item.platform == "twitch-clip" for item in items):
                continue
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
            except (KeyError, MediaError, TypeError, ValueError) as error:
                errors.append(f"Twitch channel {channel} {command}: {error}")
        return items, errors

    count = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for items, errors in pool.map(list_channel, channels):
            report.extend(errors)
            count += len(items)
            for item in items:
                add(sources, item)
    peaks: dict[str, list[float]] = defaultdict(list)
    for clip in sorted((item for item in sources.values() if item.parent_offset is not None),
                       key=lambda item: item.views, reverse=True):
        replay = sources.get(clip.parent)
        offset = clip.parent_offset
        if (replay and offset is not None and offset < replay.duration
                and all(abs(offset - prior) >= 90 for prior in peaks[replay.key])):
            peaks[replay.key].append(offset)
    for key, offsets in peaks.items():
        replay = sources[key]
        replay.anchors = list(dict.fromkeys([*offsets[:3], *replay.anchors]))
    report.append(f"Twitch checked {len(channels)} channels and found {count} recent GTA VI clips and replays.")
    if peaks:
        report.append(f"Viewer clips supplied replay timestamps for {len(peaks)} Twitch streams.")


def discover(gemini: Gemini, downloads: Downloads, report: list[str], *, days: int = 7) -> dict[str, Source]:
    earliest = source_cutoff(days)
    if dated() < earliest:
        report.append("No sources can qualify before the GTA VI publication cutoff.")
        return {}
    sources: dict[str, Source] = {}
    report.append("Search terms on YouTube and Twitch — " + ", ".join(SEARCH_TERMS) + ".")
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
    searched_youtube = False
    if youtube_key:
        try:
            youtube_discovery(sources, youtube_key, days)
            searched_youtube = True
        except (KeyError, ValueError) as error:
            report.append(f"YouTube discovery: {error}")
    if not searched_youtube:
        search_blocks = 0
        for term in SEARCH_TERMS:
            try:
                for item in youtube_search(downloads, term):
                    add(sources, item)
            except (MediaError, ValueError) as error:
                report.append(f"YouTube search {term!r}: {error}")
                search_blocks += youtube_session_blocked(error)
                if search_blocks >= 2:
                    report.append("Paused YouTube searching after repeated session challenges.")
                    break
    if twitch_client and twitch_secret:
        try:
            twitch_discovery(sources, twitch_client, twitch_secret, days)
        except (KeyError, ValueError) as error:
            report.append(f"Twitch discovery: {error}")
    twitch_public_discovery(sources, report, days)
    youtube_links = sum(item.platform == "youtube" for item in sources.values())
    link_label = "link" if youtube_links == 1 else "links"
    report.append(f"YouTube discovery collected {youtube_links} video {link_label}.")
    youtube_blocks = 0
    skipped = 0
    pending = [item for item in sources.values() if not item.duration or not item.published]

    def inspect(item: Source) -> Exception | None:
        try:
            downloads.metadata(item)
        except (MediaError, ValueError) as error:
            return error
        return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        for start in range(0, len(pending), 3):
            batch = []
            for item in pending[start:start + 3]:
                if item.platform == "youtube" and youtube_blocks >= 2:
                    skipped += 1
                    sources.pop(item.key, None)
                else:
                    batch.append(item)
            for item, error in zip(batch, pool.map(inspect, batch)):
                if error is not None:
                    if item.platform == "youtube" and youtube_session_blocked(error):
                        youtube_blocks += 1
                    report.append(f"{item.key} metadata: {error}")
                    sources.pop(item.key, None)
    if skipped:
        report.append(f"Skipped {skipped} further YouTube metadata calls after repeated session challenges.")
    latest = dated()
    recent = {key: item for key, item in sources.items() if item.published and earliest <= item.published <= latest}
    stale = sum(bool(item.published and item.published < earliest) for item in sources.values())
    undated = sum(not item.published for item in sources.values())
    if stale or undated:
        report.append(f"Skipped sources before the publication cutoff ({stale}) or with an unknown publication date ({undated}).")
    # YouTube's replay graph is optional; the API and coverage sweep supply the baseline.
    replay_targets = sorted((item for item in recent.values() if item.platform == "youtube" and item.duration > 420
                             and not item.anchors), key=priority, reverse=True)
    for item in replay_targets[:4] if youtube_blocks < 2 else ():
        try:
            duration = item.duration
            downloads.metadata(item)
            item.duration = item.duration or duration
        except (MediaError, ValueError):
            continue
    eligible = {key: item for key, item in recent.items() if item.duration >= 10}
    youtube_count = sum(item.site == "youtube" for item in eligible.values())
    report.append(f"Eligible sources by platform — YouTube {youtube_count}, Twitch {len(eligible) - youtube_count}.")
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
    return math.log1p(item.views) + 3 * (item.published >= dated(2)) + 1.5 * bool(GTA_VI.search(item.title)) + bool(item.anchors)

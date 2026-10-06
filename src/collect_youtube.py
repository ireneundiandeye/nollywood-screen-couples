"""Collect YouTube data for recurring Nollywood screen couples.

For each pair in config/pairs.yaml, the script searches YouTube for full-length
films featuring both actors, then fetches each video's statistics and the
releasing channel's subscriber count. It writes:

  data/raw/<snapshot_date>/<pair_id>.json      raw API responses (gitignored)
  data/processed/videos_<snapshot_date>.csv    one row per (pair, video)

Usage:
  python src/collect_youtube.py                 # all pairs
  python src/collect_youtube.py --pairs sam_uche arukwe_olawunmi
  python src/collect_youtube.py --pages 1       # cheaper test run

Quota: each search page costs 100 units (default limit is 10,000 per day);
video and channel lookups cost 1 unit per 50 items. Pairs already collected
today are skipped, so the script can be re-run after a quota error.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import yaml
from dotenv import load_dotenv

API = "https://www.googleapis.com/youtube/v3"
ROOT = Path(__file__).resolve().parents[1]


class QuotaExceeded(Exception):
    pass


def api_get(endpoint, params, key):
    params = {**params, "key": key}
    for attempt in range(3):
        r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
        if r.status_code == 200:
            return r.json()
        reason = ""
        try:
            reason = r.json()["error"]["errors"][0]["reason"]
        except Exception:
            pass
        if reason in ("quotaExceeded", "dailyLimitExceeded"):
            raise QuotaExceeded(reason)
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"{endpoint} failed ({r.status_code}): {r.text[:300]}")
    raise RuntimeError(f"{endpoint} failed after retries")


def parse_duration(iso):
    """Convert an ISO 8601 duration such as PT1H45M10S to seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def mentions(text, aliases):
    text = text.lower()
    return any(a.lower() in text for a in aliases)


def search_pair(pair, settings, pages, key):
    a, b = pair["actors"]
    query = f'"{a["name"]}" "{b["name"]}"'
    items, token = [], None
    for _ in range(pages):
        params = {
            "part": "snippet", "q": query, "type": "video", "maxResults": 50,
            "videoDuration": "long", "publishedAfter": settings["published_after"],
            "relevanceLanguage": "en",
        }
        if token:
            params["pageToken"] = token
        res = api_get("search", params, key)
        items += res.get("items", [])
        token = res.get("nextPageToken")
        if not token:
            break
    return query, [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]


def batched(seq, n=50):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_videos(ids, key):
    out = []
    for chunk in batched(list(dict.fromkeys(ids))):
        res = api_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)}, key)
        out += res.get("items", [])
    return out


def fetch_channels(ids, key):
    out = {}
    for chunk in batched(list(dict.fromkeys(ids))):
        res = api_get("channels", {"part": "statistics,snippet", "id": ",".join(chunk)}, key)
        for ch in res.get("items", []):
            out[ch["id"]] = ch
    return out


def to_rows(pair, videos, channels, settings, snapshot):
    a, b = pair["actors"]
    rows = []
    for v in videos:
        sn, st, cd = v["snippet"], v.get("statistics", {}), v.get("contentDetails", {})
        text = f'{sn.get("title", "")} {sn.get("description", "")}'
        ch = channels.get(sn["channelId"], {})
        ch_stats = ch.get("statistics", {})
        duration = parse_duration(cd.get("duration"))
        rows.append({
            "pair_id": pair["id"],
            "actor_a": a["name"],
            "actor_b": b["name"],
            "video_id": v["id"],
            "title": sn.get("title"),
            "description": (sn.get("description") or "")[:1000],
            "channel_id": sn["channelId"],
            "channel_title": sn.get("channelTitle"),
            "channel_subscribers": int(ch_stats["subscriberCount"]) if ch_stats.get("subscriberCount") else None,
            "channel_video_count": int(ch_stats["videoCount"]) if ch_stats.get("videoCount") else None,
            "published_at": sn.get("publishedAt"),
            "duration_minutes": round(duration / 60, 1) if duration else None,
            "view_count": int(st["viewCount"]) if st.get("viewCount") else None,
            "like_count": int(st["likeCount"]) if st.get("likeCount") else None,
            "comment_count": int(st["commentCount"]) if st.get("commentCount") else None,
            "mentions_a": mentions(text, a["aliases"]),
            "mentions_b": mentions(text, b["aliases"]),
            "is_full_film": bool(duration and duration >= settings["min_duration_minutes"] * 60),
            "snapshot_date": snapshot,
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=ROOT / "config" / "pairs.yaml")
    parser.add_argument("--pairs", nargs="*", help="only collect these pair ids")
    parser.add_argument("--pages", type=int, help="search pages per pair (overrides config)")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("YOUTUBE_API_KEY")
    if not key or key == "paste_your_key_here":
        sys.exit("No API key found. Add YOUTUBE_API_KEY=... to the .env file in the project folder.")

    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    settings = cfg["settings"]
    pages = args.pages or settings["pages_per_pair"]
    pairs = [p for p in cfg["pairs"] if not args.pairs or p["id"] in args.pairs]

    snapshot = dt.date.today().isoformat()
    raw_dir = ROOT / "data" / "raw" / snapshot
    raw_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)

    for pair in pairs:
        raw_file = raw_dir / f'{pair["id"]}.json'
        if raw_file.exists():
            print(f'[skip] {pair["id"]}: already collected today')
            continue
        try:
            query, ids = search_pair(pair, settings, pages, key)
            videos = fetch_videos(ids, key)
            channels = fetch_channels([v["snippet"]["channelId"] for v in videos], key)
        except QuotaExceeded:
            print("\nDaily YouTube quota reached. Progress is saved; run the script again tomorrow to continue.")
            break
        json.dump({"pair": pair, "query": query, "videos": videos, "channels": channels},
                  open(raw_file, "w", encoding="utf-8"), ensure_ascii=False)
        both = sum(mentions(f'{v["snippet"]["title"]} {v["snippet"].get("description", "")}', pair["actors"][0]["aliases"])
                   and mentions(f'{v["snippet"]["title"]} {v["snippet"].get("description", "")}', pair["actors"][1]["aliases"])
                   for v in videos)
        print(f'[done] {pair["id"]}: {len(videos)} videos found, {both} mention both actors')

    # Rebuild the processed table from every raw file collected today
    rows = []
    for f in sorted(raw_dir.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        rows += to_rows(d["pair"], d["videos"], d["channels"], settings, snapshot)
    if rows:
        out = ROOT / "data" / "processed" / f"videos_{snapshot}.csv"
        pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8")
        print(f"\nSaved {len(rows)} rows to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

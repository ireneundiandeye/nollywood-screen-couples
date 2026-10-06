"""Download the complete upload history of the official release channels.

For each channel in config/channels.yaml, the script fetches every uploaded
video with its statistics, then flags which of the actors in config/pairs.yaml
are named in each video's title or description. This gives complete
filmographies on the official channels, including each actor's films without
their screen partner (needed for individual baselines).

Output:
  data/raw/<snapshot_date>/channels/<channel_id>.json   raw API responses (gitignored)
  data/processed/channel_videos_<snapshot_date>.csv     one row per video

Usage:
  python src/collect_channels.py
  python src/collect_channels.py --channels UCneM4DHHUWMVwmYktz8X46g

Quota: about 1 unit per 50 videos for the upload list and 1 unit per 50 videos
for statistics, so the full channel list costs roughly 500 units.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_youtube import ROOT, QuotaExceeded, api_get, batched, mentions, parse_duration  # noqa: E402


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def actor_aliases(pairs_cfg):
    actors = {}
    for p in pairs_cfg["pairs"]:
        for a in p["actors"]:
            actors.setdefault(a["name"], set()).update(a["aliases"])
    return {name: sorted(al) for name, al in actors.items()}


def uploads_playlist(channel_id, key):
    res = api_get("channels", {"part": "contentDetails,statistics,snippet", "id": channel_id}, key)
    items = res.get("items", [])
    if not items:
        return None, None
    ch = items[0]
    return ch["contentDetails"]["relatedPlaylists"]["uploads"], ch


def playlist_video_ids(playlist_id, key):
    ids, token = [], None
    while True:
        params = {"part": "contentDetails", "playlistId": playlist_id, "maxResults": 50}
        if token:
            params["pageToken"] = token
        res = api_get("playlistItems", params, key)
        ids += [it["contentDetails"]["videoId"] for it in res.get("items", [])]
        token = res.get("nextPageToken")
        if not token:
            return ids


def video_details(ids, key):
    out = []
    for chunk in batched(ids):
        res = api_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)}, key)
        out += res.get("items", [])
    return out


def to_rows(videos, channel, actors, min_minutes, snapshot):
    ch_stats = channel.get("statistics", {})
    rows = []
    for v in videos:
        sn, st, cd = v["snippet"], v.get("statistics", {}), v.get("contentDetails", {})
        text = f'{sn.get("title", "")} {sn.get("description", "")}'
        duration = parse_duration(cd.get("duration"))
        row = {
            "video_id": v["id"],
            "title": sn.get("title"),
            "description": (sn.get("description") or "")[:1000],
            "channel_id": sn["channelId"],
            "channel_title": sn.get("channelTitle"),
            "channel_subscribers": int(ch_stats["subscriberCount"]) if ch_stats.get("subscriberCount") else None,
            "published_at": sn.get("publishedAt"),
            "duration_minutes": round(duration / 60, 1) if duration else None,
            "view_count": int(st["viewCount"]) if st.get("viewCount") else None,
            "like_count": int(st["likeCount"]) if st.get("likeCount") else None,
            "comment_count": int(st["commentCount"]) if st.get("commentCount") else None,
            "is_full_film": bool(duration and duration >= min_minutes * 60),
            "snapshot_date": snapshot,
        }
        for name, aliases in actors.items():
            row[f"has_{slug(name)}"] = mentions(text, aliases)
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channels", nargs="*", help="only collect these channel ids")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("YOUTUBE_API_KEY")
    if not key or key == "paste_your_key_here":
        sys.exit("No API key found. Add YOUTUBE_API_KEY=... to the .env file in the project folder.")

    pairs_cfg = yaml.safe_load(open(ROOT / "config" / "pairs.yaml", encoding="utf-8"))
    channels_cfg = yaml.safe_load(open(ROOT / "config" / "channels.yaml", encoding="utf-8"))
    actors = actor_aliases(pairs_cfg)
    min_minutes = pairs_cfg["settings"]["min_duration_minutes"]
    channels = [c for c in channels_cfg["channels"] if not args.channels or c["id"] in args.channels]

    snapshot = dt.date.today().isoformat()
    raw_dir = ROOT / "data" / "raw" / snapshot / "channels"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "processed").mkdir(parents=True, exist_ok=True)

    for c in channels:
        raw_file = raw_dir / f'{c["id"]}.json'
        if raw_file.exists():
            print(f'[skip] {c["title"]}: already collected today')
            continue
        try:
            playlist, channel = uploads_playlist(c["id"], key)
            if not playlist:
                print(f'[warn] {c["title"]}: channel not found')
                continue
            ids = playlist_video_ids(playlist, key)
            videos = video_details(ids, key)
        except QuotaExceeded:
            print("\nDaily YouTube quota reached. Progress is saved; run the script again tomorrow to continue.")
            break
        json.dump({"channel": channel, "videos": videos}, open(raw_file, "w", encoding="utf-8"), ensure_ascii=False)
        films = sum(1 for v in videos if (parse_duration(v.get("contentDetails", {}).get("duration")) or 0) >= min_minutes * 60)
        print(f'[done] {c["title"]}: {len(videos)} uploads, {films} full-length films')

    rows = []
    for f in sorted(raw_dir.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        rows += to_rows(d["videos"], d["channel"], actors, min_minutes, snapshot)
    if rows:
        out = ROOT / "data" / "processed" / f"channel_videos_{snapshot}.csv"
        pd.DataFrame(rows).drop_duplicates("video_id").to_csv(out, index=False, encoding="utf-8")
        print(f"\nSaved {len(rows)} videos to {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

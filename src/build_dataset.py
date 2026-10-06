"""Build the case-study dataset of screen-couple films and their pairing premium.

Case study: four recurring couples with the longest YouTube-era sequences
  Maurice Sam & Sonia Uche, Maurice Sam & Uche Montana,
  Timini Egbuson & Bimbo Ademoye, Uzor Arukwe & Bamike "BamBam" Olawunmi.

Inputs (from the collection scripts):
  data/processed/videos_<date>.csv          pair searches
  data/processed/channel_videos_<date>.csv  official channel uploads

Outputs:
  data/processed/films_<date>.csv          all deduplicated full-length films used to model expected views
  data/processed/couple_films_<date>.csv   the four couples' joint films with collaboration index and premium
"""

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import yaml

ROOT = Path(__file__).resolve().parents[1]
FOCUS = ["sam_uche", "sam_montana", "egbuson_ademoye", "arukwe_olawunmi"]
DUBBED_CHANNELS = {"FRENCHTV247", "Meilleur de French TV", "Uchenna Mbunabo French Tv", "FRENCHFILMSTV"}
MIN_CHANNEL_MEDIAN = 200_000   # excludes mass re-upload channels with very low typical views
MIN_AGE_DAYS = 30              # films need time to accumulate views
GENERIC = r"\b(new|latest|full|movie|movies|nigerian|nollywood|complete|film|films|trending|20\d\d|part\s*\d+)\b"


def norm_title(t):
    """Reduce a YouTube title to the film's name: text before the cast list, lower case, no filler words."""
    t = re.split(r"\s[-~|–:]\s|\||\(|\bstarring\b|\.\s|!", str(t), maxsplit=1, flags=re.I)[0]
    t = re.sub(GENERIC, " ", t.lower())
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def named(text, aliases):
    text = str(text).lower()
    return any(a.lower() in text for a in aliases)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="snapshot date (default: latest)")
    args = parser.parse_args()
    proc = ROOT / "data" / "processed"
    date = args.date or sorted(p.stem.split("_")[-1] for p in proc.glob("channel_videos_*.csv"))[-1]
    snapshot = pd.Timestamp(date, tz="UTC")

    cfg = yaml.safe_load(open(ROOT / "config" / "pairs.yaml", encoding="utf-8"))
    pairs = {p["id"]: p for p in cfg["pairs"] if p["id"] in FOCUS}
    actors = {}
    for p in pairs.values():
        for a in p["actors"]:
            actors[a["name"]] = a["aliases"]
    slug = lambda n: re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")

    cols = ["video_id", "title", "description", "channel_title", "channel_subscribers", "published_at",
            "duration_minutes", "view_count", "like_count", "comment_count", "is_full_film"]
    search = pd.read_csv(proc / f"videos_{date}.csv")[cols]
    channel = pd.read_csv(proc / f"channel_videos_{date}.csv")[cols]
    v = pd.concat([channel, search]).drop_duplicates("video_id")
    v["published_at"] = pd.to_datetime(v["published_at"], utc=True)

    # Full-length single films only (no series seasons or episodes), released 2019 onwards
    v = v[v.is_full_film & (v.published_at >= "2019-01-01")]
    v = v[~v.title.str.contains(r"\bseason\b|\bepisode\b", case=False, na=False)]
    v = v[~v.channel_title.isin(DUBBED_CHANNELS)]

    # Keep channels whose typical film is genuinely watched (drops mass re-upload channels)
    med = v.groupby("channel_title").view_count.median()
    v = v[v.channel_title.map(med) >= MIN_CHANNEL_MEDIAN].copy()

    # Lead billing: actor named in the title or the opening of the description
    billing = v.title.fillna("") + " " + v.description.fillna("").str[:300]
    for name, aliases in actors.items():
        v[f"has_{slug(name)}"] = billing.map(lambda t, al=aliases: named(t, al)).astype(int)
    manual = yaml.safe_load(open(ROOT / "config" / "manual_cast.yaml", encoding="utf-8")) or {}
    for entry in manual.get("videos", []):
        for name in entry["actors"]:
            v.loc[v.video_id == entry["video_id"], f"has_{slug(name)}"] = 1

    # Collapse multi-part uploads of the same film on one channel (first part = audience reach)
    v["norm_title"] = v.title.map(norm_title)
    v = v[v.norm_title != ""]
    has = [c for c in v.columns if c.startswith("has_")]
    agg = {"video_id": "first", "title": "first", "published_at": "min", "view_count": "max",
           "like_count": "max", "comment_count": "max", "channel_subscribers": "first"}
    agg.update({c: "max" for c in has})
    v = v.sort_values("published_at")
    films = v.groupby(["channel_title", "norm_title"], as_index=False).agg(agg)

    # Re-uploads across channels: same film name and same focus actors -> keep the earliest release
    films["cast_key"] = films[has].astype(str).agg("".join, axis=1)
    films = films.sort_values("published_at").drop_duplicates(["norm_title", "cast_key"], keep="first")

    films["age_days"] = (snapshot - films.published_at).dt.days
    films = films[films.age_days >= MIN_AGE_DAYS].copy()
    films["log_views"] = np.log1p(films.view_count)
    films["log_age"] = np.log(films.age_days)
    films["year"] = films.published_at.dt.year
    films["like_rate"] = films.like_count / films.view_count
    films["comment_rate"] = films.comment_count / films.view_count
    for pid, p in pairs.items():
        a, b = (slug(x["name"]) for x in p["actors"])
        films[f"pair_{pid}"] = (films[f"has_{a}"] & films[f"has_{b}"]).astype(int)

    # Expected views: channel reach, film age, release year and each actor's individual draw.
    # The pairing premium is what remains once those are accounted for.
    pair_cols = [f"pair_{p}" for p in pairs]
    formula = "log_views ~ log_age + C(channel_title) + C(year) + " + " + ".join(has + pair_cols)
    model = smf.ols(formula, films).fit(cov_type="HC1")
    no_pair = films.copy()
    no_pair[pair_cols] = 0
    films["expected_log_views"] = model.predict(no_pair)
    films["premium"] = films.log_views - films.expected_log_views

    couples = []
    for pid, p in pairs.items():
        cf = films[films[f"pair_{pid}"] == 1].sort_values("published_at").copy()
        cf.insert(0, "pair_id", pid)
        cf["couple"] = " & ".join(x["name"] for x in p["actors"])
        cf["k"] = np.arange(1, len(cf) + 1)
        cf["gap_days"] = cf.published_at.diff().dt.days
        couples.append(cf)
    couples = pd.concat(couples)

    keep = ["pair_id", "couple", "k", "title", "norm_title", "channel_title", "channel_subscribers", "published_at",
            "age_days", "gap_days", "view_count", "like_count", "comment_count", "like_rate", "comment_rate",
            "log_views", "expected_log_views", "premium", "video_id"]
    films.drop(columns=["cast_key"]).to_csv(proc / f"films_{date}.csv", index=False)
    couples[keep].to_csv(proc / f"couple_films_{date}.csv", index=False)

    print(f"Films used for expected views: {len(films):,}  (model R-squared {model.rsquared:.2f})")
    print("\nAverage pairing premium (log points) and joint films per couple:")
    summary = couples.groupby("couple").agg(films=("k", "max"), first=("published_at", "min"),
                                            last=("published_at", "max"), mean_premium=("premium", "mean"))
    summary["first"], summary["last"] = summary["first"].dt.date, summary["last"].dt.date
    print(summary.round(2).to_string())


if __name__ == "__main__":
    main()

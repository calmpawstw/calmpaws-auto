#!/usr/bin/env python3
"""
安寵 Calm Paws — 數據收集模組
從 YouTube Analytics API + Instagram Graph API 拉取成效數據，寫入 SQLite。

用法：
    python cp_analytics.py              # 收集昨日至今數據
    python cp_analytics.py --backfill 30  # 回補過去 30 天
"""
import os
import sys
import json
import sqlite3
import logging
import argparse
import datetime as dt
import urllib.request
import urllib.parse
from pathlib import Path

import yaml

BASE_DIR = Path(os.path.expanduser("~/calm_paws"))
DB_PATH = BASE_DIR / "data" / "metrics.db"
CONF_PATH = BASE_DIR / "config.yaml"
IG_API = "https://graph.instagram.com/v21.0"

logger = logging.getLogger("cp_analytics")


# ══════════════════════════════════════════════════════════
#  資料庫
# ══════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,
    published_at  TEXT,
    title         TEXT,
    scene         TEXT,
    title_formula TEXT,
    thumb_style   TEXT,
    duration_h    REAL,
    upload_slot   TEXT,
    music_sig     TEXT,     -- 音樂特徵指紋，用於差異化檢查
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS video_metrics (
    video_id      TEXT,
    snapshot_date TEXT,
    age_days      INTEGER,
    views         INTEGER DEFAULT 0,
    impressions   INTEGER DEFAULT 0,
    ctr           REAL    DEFAULT 0,
    avd_sec       REAL    DEFAULT 0,
    watch_hours   REAL    DEFAULT 0,
    subs_gained   INTEGER DEFAULT 0,
    likes         INTEGER DEFAULT 0,
    comments      INTEGER DEFAULT 0,
    PRIMARY KEY (video_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS reels (
    media_id      TEXT PRIMARY KEY,
    published_at  TEXT,
    permalink     TEXT,
    scene         TEXT,
    hook_style    TEXT,
    hashtag_set   TEXT,
    caption_style TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reel_metrics (
    media_id      TEXT,
    snapshot_date TEXT,
    age_days      INTEGER,
    reach         INTEGER DEFAULT 0,
    plays         INTEGER DEFAULT 0,
    saves         INTEGER DEFAULT 0,
    shares        INTEGER DEFAULT 0,
    likes         INTEGER DEFAULT 0,
    comments      INTEGER DEFAULT 0,
    PRIMARY KEY (media_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS channel_daily (
    date            TEXT PRIMARY KEY,
    subs_total      INTEGER DEFAULT 0,
    subs_gained     INTEGER DEFAULT 0,
    subs_lost       INTEGER DEFAULT 0,
    views           INTEGER DEFAULT 0,
    watch_hours     REAL    DEFAULT 0,
    watch_hours_365 REAL    DEFAULT 0,
    ig_followers    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS arms (
    dimension   TEXT,
    value       TEXT,
    alpha       REAL DEFAULT 1.0,
    beta        REAL DEFAULT 1.0,
    n           INTEGER DEFAULT 0,
    sum_reward  REAL DEFAULT 0,
    active      INTEGER DEFAULT 1,
    updated_at  TEXT,
    PRIMARY KEY (dimension, value)
);

CREATE TABLE IF NOT EXISTS decisions (
    ts          TEXT,
    dimension   TEXT,
    action      TEXT,
    detail      TEXT,
    auto_applied INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS policy_flags (
    ts       TEXT,
    severity TEXT,
    kind     TEXT,
    detail   TEXT
);

CREATE TABLE IF NOT EXISTS affiliate_clicks (
    date       TEXT,
    link_id    TEXT,
    source     TEXT,
    clicks     INTEGER DEFAULT 0,
    PRIMARY KEY (date, link_id, source)
);

CREATE TABLE IF NOT EXISTS costs (
    date    TEXT,
    service TEXT,
    usd     REAL DEFAULT 0,
    PRIMARY KEY (date, service)
);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_config() -> dict:
    with open(CONF_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ══════════════════════════════════════════════════════════
#  YouTube
# ══════════════════════════════════════════════════════════
def youtube_clients():
    """回傳 (data_api, analytics_api)，失敗回傳 (None, None)"""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        logger.error("缺少 google-api-python-client，請執行安裝腳本")
        return None, None

    token_path = BASE_DIR / "token.json"
    if not token_path.exists():
        logger.error("找不到 token.json，請先執行授權")
        return None, None

    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
        return (
            build("youtube", "v3", credentials=creds, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False),
        )
    except Exception as e:
        logger.error(f"YouTube 認證失敗：{e}")
        return None, None


def yt_channel_stats(data_api) -> dict:
    """訂閱數、總觀看數"""
    try:
        r = data_api.channels().list(part="statistics", mine=True).execute()
        s = r["items"][0]["statistics"]
        return {
            "subs_total": int(s.get("subscriberCount", 0)),
            "views_total": int(s.get("viewCount", 0)),
            "video_count": int(s.get("videoCount", 0)),
        }
    except Exception as e:
        logger.warning(f"取得頻道統計失敗：{e}")
        return {}


def yt_query(analytics_api, start: str, end: str, metrics: str,
             dimensions: str = None, filters: str = None, sort: str = None):
    """包裝 YouTube Analytics 查詢，失敗回傳 None"""
    kwargs = {
        "ids": "channel==MINE",
        "startDate": start,
        "endDate": end,
        "metrics": metrics,
    }
    if dimensions:
        kwargs["dimensions"] = dimensions
    if filters:
        kwargs["filters"] = filters
    if sort:
        kwargs["sort"] = sort
    try:
        return analytics_api.reports().query(**kwargs).execute()
    except Exception as e:
        logger.warning(f"Analytics 查詢失敗 ({metrics}): {e}")
        return None


def collect_youtube(conn, days_back: int = 2):
    data_api, analytics_api = youtube_clients()
    if not data_api or not analytics_api:
        return

    today = dt.date.today()
    start = (today - dt.timedelta(days=days_back)).isoformat()
    end = today.isoformat()
    year_ago = (today - dt.timedelta(days=365)).isoformat()

    stats = yt_channel_stats(data_api)

    # ── 頻道每日數據 ────────────────────────────────────
    r = yt_query(
        analytics_api, start, end,
        "views,estimatedMinutesWatched,subscribersGained,subscribersLost",
        dimensions="day",
    )
    if r and r.get("rows"):
        for row in r["rows"]:
            date, views, mins, gained, lost = row[0], row[1], row[2], row[3], row[4]
            conn.execute(
                """INSERT INTO channel_daily
                   (date, views, watch_hours, subs_gained, subs_lost, subs_total)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(date) DO UPDATE SET
                     views=excluded.views,
                     watch_hours=excluded.watch_hours,
                     subs_gained=excluded.subs_gained,
                     subs_lost=excluded.subs_lost,
                     subs_total=excluded.subs_total""",
                (date, views, round(mins / 60, 2), gained, lost,
                 stats.get("subs_total", 0)),
            )

    # ── YPP 用的 365 天滾動觀看時數 ────────────────────
    r365 = yt_query(analytics_api, year_ago, end, "estimatedMinutesWatched")
    if r365 and r365.get("rows"):
        wh365 = round(r365["rows"][0][0] / 60, 1)
        conn.execute(
            """INSERT INTO channel_daily (date, watch_hours_365, subs_total)
               VALUES (?,?,?)
               ON CONFLICT(date) DO UPDATE SET
                 watch_hours_365=excluded.watch_hours_365,
                 subs_total=excluded.subs_total""",
            (end, wh365, stats.get("subs_total", 0)),
        )
        logger.info(f"365 天觀看時數：{wh365}h / 4000h")

    # ── 各影片數據 ──────────────────────────────────────
    rows = conn.execute("SELECT video_id, published_at FROM videos").fetchall()
    for v in rows:
        vid = v["video_id"]
        f = f"video=={vid}"

        base = yt_query(
            analytics_api, start, end,
            "views,estimatedMinutesWatched,averageViewDuration,"
            "subscribersGained,likes,comments",
            filters=f,
        )
        if not base or not base.get("rows"):
            continue
        b = base["rows"][0]
        views, mins, avd, subs, likes, comments = b[0], b[1], b[2], b[3], b[4], b[5]

        # 曝光與點閱率（部分頻道可能無權限）
        impressions, ctr = 0, 0.0
        imp = yt_query(
            analytics_api, start, end,
            "impressions,impressionClickThroughRate", filters=f,
        )
        if imp and imp.get("rows"):
            impressions = imp["rows"][0][0] or 0
            ctr = imp["rows"][0][1] or 0.0

        age = 0
        if v["published_at"]:
            try:
                pub = dt.datetime.fromisoformat(
                    v["published_at"].replace("Z", "+00:00")).date()
                age = (today - pub).days
            except Exception:
                pass

        conn.execute(
            """INSERT INTO video_metrics
               (video_id, snapshot_date, age_days, views, impressions, ctr,
                avd_sec, watch_hours, subs_gained, likes, comments)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(video_id, snapshot_date) DO UPDATE SET
                 age_days=excluded.age_days, views=excluded.views,
                 impressions=excluded.impressions, ctr=excluded.ctr,
                 avd_sec=excluded.avd_sec, watch_hours=excluded.watch_hours,
                 subs_gained=excluded.subs_gained, likes=excluded.likes,
                 comments=excluded.comments""",
            (vid, end, age, views, impressions, ctr, avd,
             round(mins / 60, 2), subs, likes, comments),
        )
        logger.info(f"影片 {vid}: {views} 次觀看, {round(mins/60,1)}h, CTR {ctr}%")

    conn.commit()


def sync_video_list(conn):
    """把頻道上還沒進 DB 的影片補進來"""
    data_api, _ = youtube_clients()
    if not data_api:
        return
    try:
        ch = data_api.channels().list(part="contentDetails", mine=True).execute()
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        page = None
        while True:
            pl = data_api.playlistItems().list(
                part="snippet,contentDetails", playlistId=uploads,
                maxResults=50, pageToken=page,
            ).execute()
            for it in pl.get("items", []):
                vid = it["contentDetails"]["videoId"]
                sn = it["snippet"]
                conn.execute(
                    """INSERT OR IGNORE INTO videos
                       (video_id, published_at, title) VALUES (?,?,?)""",
                    (vid, it["contentDetails"].get("videoPublishedAt"),
                     sn.get("title", "")),
                )
            page = pl.get("nextPageToken")
            if not page:
                break
        conn.commit()
    except Exception as e:
        logger.warning(f"同步影片清單失敗：{e}")


# ══════════════════════════════════════════════════════════
#  Instagram
# ══════════════════════════════════════════════════════════
def ig_get(path: str, token: str, **params):
    params["access_token"] = token
    url = f"{IG_API}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": {"message": str(e)}}


def collect_instagram(conn, config: dict):
    keys = config.get("api_keys", {})
    token = keys.get("instagram_access_token", "")
    ig_id = keys.get("instagram_user_id", "")
    if not token or token.startswith("YOUR_"):
        logger.warning("Instagram token 未設定，跳過")
        return

    today = dt.date.today().isoformat()

    # 帳號層級
    me = ig_get("me", token, fields="followers_count,media_count,username")
    if "followers_count" in me:
        conn.execute(
            """INSERT INTO channel_daily (date, ig_followers) VALUES (?,?)
               ON CONFLICT(date) DO UPDATE SET ig_followers=excluded.ig_followers""",
            (today, me["followers_count"]),
        )
        logger.info(f"IG @{me.get('username')}: {me['followers_count']} 粉絲")

    # 近期貼文
    media = ig_get(f"{ig_id}/media", token,
                   fields="id,media_type,permalink,timestamp,like_count,comments_count",
                   limit=30)
    for m in media.get("data", []):
        mid = m["id"]
        conn.execute(
            """INSERT OR IGNORE INTO reels (media_id, published_at, permalink)
               VALUES (?,?,?)""",
            (mid, m.get("timestamp"), m.get("permalink")),
        )

        ins = ig_get(f"{mid}/insights", token,
                     metric="reach,saved,shares,views")
        vals = {}
        for item in ins.get("data", []):
            try:
                vals[item["name"]] = item["values"][0]["value"]
            except (KeyError, IndexError):
                pass

        age = 0
        if m.get("timestamp"):
            try:
                pub = dt.datetime.fromisoformat(
                    m["timestamp"].replace("+0000", "+00:00")).date()
                age = (dt.date.today() - pub).days
            except Exception:
                pass

        conn.execute(
            """INSERT INTO reel_metrics
               (media_id, snapshot_date, age_days, reach, plays, saves,
                shares, likes, comments)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(media_id, snapshot_date) DO UPDATE SET
                 age_days=excluded.age_days, reach=excluded.reach,
                 plays=excluded.plays, saves=excluded.saves,
                 shares=excluded.shares, likes=excluded.likes,
                 comments=excluded.comments""",
            (mid, today, age, vals.get("reach", 0), vals.get("views", 0),
             vals.get("saved", 0), vals.get("shares", 0),
             m.get("like_count", 0), m.get("comments_count", 0)),
        )

    conn.commit()


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=2,
                    help="回補天數（預設 2）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    conn = db()
    config = load_config()

    logger.info("同步影片清單...")
    sync_video_list(conn)

    logger.info("收集 YouTube 數據...")
    collect_youtube(conn, args.backfill)

    logger.info("收集 Instagram 數據...")
    collect_instagram(conn, config)

    conn.close()
    logger.info("✅ 數據收集完成")


if __name__ == "__main__":
    main()

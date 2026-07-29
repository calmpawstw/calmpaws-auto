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

# 本機跑在 ~/calm_paws，GitHub Actions 跑在 workspace 目錄。
# 用 CP_HOME 環境變數切換，沒設就沿用本機路徑。
BASE_DIR = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
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
    """讀設定檔。雲端上若尚未產生就回空 dict，不要讓整個流程掛掉。"""
    if not CONF_PATH.exists():
        logger.warning(f"找不到 {CONF_PATH}，使用空設定")
        return {}
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"讀取設定失敗：{e}")
        return {}


ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


def check_token_scopes() -> tuple:
    """
    檢查 token.json 有沒有 Analytics 讀取權限。
    只有上傳權限的 token 拿不到觀看時數與 CTR，優化引擎會沒有數據可學。
    回傳 (是否足夠, 訊息)
    """
    p = BASE_DIR / "token.json"
    if not p.exists():
        return False, f"找不到 {p}"
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        return False, f"token.json 解析失敗：{e}"

    scopes = d.get("scopes") or []
    if ANALYTICS_SCOPE in scopes:
        return True, "Token 權限完整"
    return False, (
        "Token 缺少 yt-analytics.readonly 權限。\n"
        "      目前權限：" + (", ".join(scopes) or "（無記錄）") + "\n"
        "      影響：拿不到觀看時數、CTR、留存率，優化引擎沒有數據可學。\n"
        "      解法：在 Mac 上執行 修復雲端.command，\n"
        "            它會重新授權並自動更新 GitHub Secret YT_TOKEN_JSON。"
    )


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


def collect_youtube(conn, days_back: int = 2) -> bool:
    data_api, analytics_api = youtube_clients()
    if not data_api or not analytics_api:
        raise RuntimeError("YouTube 認證失敗，無法取得數據")

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
    return True


def sync_video_list(conn) -> bool:
    """把頻道上還沒進 DB 的影片補進來"""
    data_api, _ = youtube_clients()
    if not data_api:
        raise RuntimeError("YouTube 認證失敗，無法同步影片清單")
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
        return True
    except Exception as e:
        raise RuntimeError(f"同步影片清單失敗：{e}")


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


def collect_instagram(conn, config: dict) -> bool:
    keys = config.get("api_keys", {})
    token = keys.get("instagram_access_token", "") or os.environ.get("IG_ACCESS_TOKEN", "")
    ig_id = keys.get("instagram_user_id", "") or os.environ.get("IG_USER_ID", "")
    if not token or token.startswith("YOUR_"):
        raise RuntimeError("Instagram token 未設定")

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
    if "error" in media:
        raise RuntimeError(f"Instagram API 錯誤：{media['error'].get('message')}")
    return True


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=2,
                    help="回補天數（預設 2）")
    ap.add_argument("--strict", action="store_true",
                    help="任一來源失敗即回傳非零（預設容忍部分失敗）")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )

    logger.info(f"工作目錄：{BASE_DIR}")
    conn = db()
    config = load_config()

    ok, msg = check_token_scopes()
    if ok:
        logger.info(f"✅ {msg}")
    else:
        logger.warning(f"⚠️  {msg}")

    results = {}

    # 每個來源獨立處理，一個掛掉不影響其他來源
    for name, fn in (
        ("同步影片清單", lambda: sync_video_list(conn)),
        ("YouTube 數據", lambda: collect_youtube(conn, args.backfill)),
        ("Instagram 數據", lambda: collect_instagram(conn, config)),
    ):
        logger.info(f"── {name} ──")
        try:
            fn()
            results[name] = True
        except Exception as e:
            logger.error(f"{name} 失敗：{e}")
            results[name] = False

    # 摘要
    counts = {}
    for t in ("videos", "video_metrics", "reels", "reel_metrics", "channel_daily"):
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:
            counts[t] = "?"
    conn.close()

    logger.info("── 結果 ──")
    for k, v in results.items():
        logger.info(f"  {'✅' if v else '❌'} {k}")
    logger.info(f"  資料庫：{counts}")

    if not any(results.values()):
        logger.error("所有來源都失敗")
        sys.exit(1)
    if args.strict and not all(results.values()):
        sys.exit(1)
    logger.info("✅ 數據收集完成")


if __name__ == "__main__":
    main()

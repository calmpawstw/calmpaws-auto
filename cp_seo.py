#!/usr/bin/env python3
"""
安寵 Calm Paws — 關鍵字探勘與競品分析

沒有預算的情況下，唯一能用的成長槓桿是「讓已經在搜尋的人找到你」。
這支用兩個免費來源做關鍵字研究：

  1. YouTube 自動完成建議（suggestqueries）
     使用者實際打過的字。有建議 = 有人在搜。免費、免驗證。

  2. YouTube Data API search.list
     該關鍵字下現有影片的競爭強度。已有授權，每次 100 單位配額。

機會分數的設計取捨：
  搜尋需求高、但既有影片的頻道規模小 → 有機會擠進去
  搜尋需求高、但全是百萬訂閱大頻道   → 別碰，打不贏
  完全沒有建議詞                      → 沒人搜，做了也沒人看

配額：免費額度每天 10,000 單位，search.list 每次 100 單位。
週跑一次、每次約 20 個關鍵字 = 2000 單位，很安全。

用法：
    python cp_seo.py --research        探勘關鍵字並寫入資料庫
    python cp_seo.py --competitors     分析競品頻道
    python cp_seo.py --report          印出目前的機會清單
"""
import os
import re
import sys
import json
import time
import sqlite3
import logging
import argparse
import datetime as dt
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
DB_PATH = BASE_DIR / "data" / "metrics.db"

logger = logging.getLogger("cp_seo")

# 這個品牌的核心語意範圍。自動完成會從這些種子往外長。
SEED_TERMS = [
    "狗狗音樂", "貓咪音樂", "寵物音樂", "寵物放鬆",
    "狗狗焦慮", "狗狗睡覺音樂", "貓咪助眠",
    "分離焦慮 狗", "狗狗怕打雷", "寵物 白噪音",
    "dog calming music", "cat relaxing music",
    "pet anxiety music", "dog sleep music",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords (
    term            TEXT PRIMARY KEY,
    source          TEXT,
    lang            TEXT,
    suggest_rank    INTEGER,
    result_count    INTEGER DEFAULT 0,
    top_median_views INTEGER DEFAULT 0,
    top_median_subs  INTEGER DEFAULT 0,
    opportunity     REAL DEFAULT 0,
    checked_at      TEXT
);

CREATE TABLE IF NOT EXISTS competitors (
    channel_id      TEXT PRIMARY KEY,
    title           TEXT,
    subs            INTEGER DEFAULT 0,
    video_count     INTEGER DEFAULT 0,
    total_views     INTEGER DEFAULT 0,
    recent_uploads  INTEGER DEFAULT 0,
    avg_recent_views INTEGER DEFAULT 0,
    checked_at      TEXT
);

CREATE TABLE IF NOT EXISTS competitor_videos (
    video_id      TEXT PRIMARY KEY,
    channel_id    TEXT,
    title         TEXT,
    published_at  TEXT,
    views         INTEGER DEFAULT 0,
    duration_s    INTEGER DEFAULT 0,
    checked_at    TEXT
);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ══════════════════════════════════════════════════════════
#  1. 自動完成建議
# ══════════════════════════════════════════════════════════
def fetch_suggestions(term: str, lang: str = "zh-TW") -> list:
    """
    抓 YouTube 搜尋建議。有建議代表真的有人在搜這個字。
    這個端點免費且不需驗證，但沒有官方文件，所以錯誤一律吞掉。
    """
    url = ("https://suggestqueries.google.com/complete/search?"
           + urllib.parse.urlencode({
               "client": "youtube", "ds": "yt", "hl": lang, "q": term}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.warning(f"建議查詢失敗 {term}：{e}")
        return []

    # 回應是 JSONP：window.google.ac.h([...])
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return [s[0] for s in data[1] if isinstance(s, list) and s]
    except Exception:
        return []


def expand_keywords(seeds: list, lang: str = "zh-TW") -> dict:
    """種子詞 → 建議詞。回傳 {建議詞: 排名}"""
    found = {}
    for seed in seeds:
        sugg = fetch_suggestions(seed, lang)
        for i, s in enumerate(sugg):
            if s not in found or i < found[s]:
                found[s] = i
        # 再往下一層：用前三個建議繼續展開
        for s in sugg[:3]:
            for i, s2 in enumerate(fetch_suggestions(s, lang)):
                if s2 not in found:
                    found[s2] = i + 10
        time.sleep(0.3)      # 對免費端點客氣一點
    return found


# ══════════════════════════════════════════════════════════
#  2. 競爭強度
# ══════════════════════════════════════════════════════════
def youtube_client():
    try:
        sys.path.insert(0, str(BASE_DIR))
        from cp_analytics import youtube_clients
        data_api, _ = youtube_clients()
        return data_api
    except Exception as e:
        logger.error(f"YouTube 認證失敗：{e}")
        return None


def median(xs: list) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    n = len(s)
    return int(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def analyse_term(yt, term: str, max_results: int = 10) -> dict:
    """回傳該關鍵字的競爭概況"""
    try:
        r = yt.search().list(
            part="snippet", q=term, type="video",
            maxResults=max_results, order="relevance",
            regionCode="TW", relevanceLanguage="zh-Hant",
        ).execute()
    except Exception as e:
        logger.warning(f"搜尋失敗 {term}：{e}")
        return {}

    items = r.get("items", [])
    total = r.get("pageInfo", {}).get("totalResults", 0)
    vids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
    chans = list({i["snippet"]["channelId"] for i in items})
    if not vids:
        return {"result_count": total, "top_median_views": 0,
                "top_median_subs": 0, "titles": []}

    views, titles = [], []
    try:
        vr = yt.videos().list(part="statistics,snippet",
                              id=",".join(vids)).execute()
        for v in vr.get("items", []):
            views.append(int(v["statistics"].get("viewCount", 0)))
            titles.append(v["snippet"]["title"])
    except Exception as e:
        logger.warning(f"影片統計失敗：{e}")

    subs = []
    try:
        cr = yt.channels().list(part="statistics",
                                id=",".join(chans[:50])).execute()
        for c in cr.get("items", []):
            if not c["statistics"].get("hiddenSubscriberCount"):
                subs.append(int(c["statistics"].get("subscriberCount", 0)))
    except Exception as e:
        logger.warning(f"頻道統計失敗：{e}")

    return {
        "result_count": total,
        "top_median_views": median(views),
        "top_median_subs": median(subs),
        "titles": titles,
    }


def opportunity_score(suggest_rank: int, med_views: int, med_subs: int) -> float:
    """
    機會分數 0-1。

    三個因子：
      需求  自動完成排名越前面代表越多人搜
      可及  前排影片的觀看數不能高到搆不著，但也不能是死字
      門檻  競爭頻道訂閱數越小，新頻道越有機會擠進去

    這是啟發式的，不是精確模型 —— 沒有真實搜尋量數據可用，
    只能用這些代理指標。當成排序參考，不要當成預測。
    """
    demand = max(0.0, 1.0 - suggest_rank / 20.0)

    if med_views <= 0:
        reach = 0.1                       # 沒有影片 = 可能是死字
    elif med_views < 1_000:
        reach = 0.5
    elif med_views < 100_000:
        reach = 1.0                       # 甜蜜區
    elif med_views < 1_000_000:
        reach = 0.6
    else:
        reach = 0.25                      # 全是爆款，很難擠

    if med_subs <= 0:
        barrier = 0.6
    elif med_subs < 10_000:
        barrier = 1.0
    elif med_subs < 100_000:
        barrier = 0.7
    elif med_subs < 1_000_000:
        barrier = 0.4
    else:
        barrier = 0.15

    return round(0.4 * demand + 0.3 * reach + 0.3 * barrier, 3)


def research(limit: int = 25):
    conn = db()
    yt = youtube_client()
    if not yt:
        logger.error("沒有 YouTube 授權，無法分析競爭強度")
        return

    logger.info("展開關鍵字...")
    zh = expand_keywords(SEED_TERMS[:8], "zh-TW")
    en = expand_keywords(SEED_TERMS[8:], "en")
    logger.info(f"中文 {len(zh)} 個，英文 {len(en)} 個")

    now = dt.datetime.now().isoformat()
    pool = [(t, r, "zh-TW") for t, r in zh.items()]
    pool += [(t, r, "en") for t, r in en.items()]
    pool.sort(key=lambda x: x[1])

    # 只分析前 N 個，控制 API 配額
    checked = 0
    for term, rank, lang in pool[:limit]:
        info = analyse_term(yt, term)
        if not info:
            continue
        score = opportunity_score(rank, info["top_median_views"],
                                  info["top_median_subs"])
        conn.execute(
            """INSERT INTO keywords
               (term, source, lang, suggest_rank, result_count,
                top_median_views, top_median_subs, opportunity, checked_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(term) DO UPDATE SET
                 suggest_rank=excluded.suggest_rank,
                 result_count=excluded.result_count,
                 top_median_views=excluded.top_median_views,
                 top_median_subs=excluded.top_median_subs,
                 opportunity=excluded.opportunity,
                 checked_at=excluded.checked_at""",
            (term, "youtube_suggest", lang, rank, info["result_count"],
             info["top_median_views"], info["top_median_subs"], score, now))
        checked += 1
        logger.info(f"  {term[:34]:36s} 機會 {score:.2f}  "
                    f"中位觀看 {info['top_median_views']:>9,}  "
                    f"中位訂閱 {info['top_median_subs']:>9,}")

    # 其餘的先記下來，不做競爭分析（省配額）
    for term, rank, lang in pool[limit:]:
        conn.execute(
            """INSERT OR IGNORE INTO keywords
               (term, source, lang, suggest_rank, checked_at)
               VALUES (?,?,?,?,?)""",
            (term, "youtube_suggest", lang, rank, now))

    conn.commit()
    conn.close()
    logger.info(f"✅ 分析了 {checked} 個關鍵字，另記錄 {len(pool)-limit} 個待查")


# ══════════════════════════════════════════════════════════
#  3. 競品頻道
# ══════════════════════════════════════════════════════════
def competitors(max_channels: int = 12):
    conn = db()
    yt = youtube_client()
    if not yt:
        return

    now = dt.datetime.now().isoformat()
    seen = {}

    for term in ["寵物舒緩音樂", "狗狗放鬆音樂", "貓咪助眠音樂",
                 "dog calming music", "pet relaxation music"]:
        try:
            r = yt.search().list(part="snippet", q=term, type="video",
                                 maxResults=10, order="viewCount",
                                 regionCode="TW").execute()
        except Exception as e:
            logger.warning(f"搜尋 {term} 失敗：{e}")
            continue
        for it in r.get("items", []):
            cid = it["snippet"]["channelId"]
            seen.setdefault(cid, it["snippet"]["channelTitle"])

    ids = list(seen)[:max_channels]
    if not ids:
        logger.warning("沒有找到競品頻道")
        return

    try:
        cr = yt.channels().list(part="statistics,contentDetails,snippet",
                                id=",".join(ids)).execute()
    except Exception as e:
        logger.error(f"頻道查詢失敗：{e}")
        return

    for c in cr.get("items", []):
        st = c["statistics"]
        cid = c["id"]
        subs = 0 if st.get("hiddenSubscriberCount") else int(
            st.get("subscriberCount", 0))

        recent, rviews = 0, []
        try:
            pl = c["contentDetails"]["relatedPlaylists"]["uploads"]
            pi = yt.playlistItems().list(part="contentDetails",
                                         playlistId=pl,
                                         maxResults=10).execute()
            vids = [x["contentDetails"]["videoId"] for x in pi.get("items", [])]
            cutoff = (dt.datetime.now(dt.timezone.utc)
                      - dt.timedelta(days=30))
            if vids:
                vr = yt.videos().list(part="statistics,snippet,contentDetails",
                                      id=",".join(vids)).execute()
                for v in vr.get("items", []):
                    vw = int(v["statistics"].get("viewCount", 0))
                    rviews.append(vw)
                    pub = v["snippet"]["publishedAt"]
                    try:
                        if dt.datetime.fromisoformat(
                                pub.replace("Z", "+00:00")) > cutoff:
                            recent += 1
                    except Exception:
                        pass
                    conn.execute(
                        """INSERT OR REPLACE INTO competitor_videos
                           (video_id, channel_id, title, published_at,
                            views, checked_at) VALUES (?,?,?,?,?,?)""",
                        (v["id"], cid, v["snippet"]["title"], pub, vw, now))
        except Exception as e:
            logger.warning(f"取得 {cid} 近期影片失敗：{e}")

        conn.execute(
            """INSERT INTO competitors
               (channel_id, title, subs, video_count, total_views,
                recent_uploads, avg_recent_views, checked_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 subs=excluded.subs, video_count=excluded.video_count,
                 total_views=excluded.total_views,
                 recent_uploads=excluded.recent_uploads,
                 avg_recent_views=excluded.avg_recent_views,
                 checked_at=excluded.checked_at""",
            (cid, c["snippet"]["title"], subs,
             int(st.get("videoCount", 0)), int(st.get("viewCount", 0)),
             recent, int(sum(rviews) / len(rviews)) if rviews else 0, now))

        logger.info(f"  {c['snippet']['title'][:26]:28s} "
                    f"訂閱 {subs:>10,}  近 30 天上片 {recent:>2}  "
                    f"近期均觀看 {int(sum(rviews)/len(rviews)) if rviews else 0:>8,}")

    conn.commit()
    conn.close()
    logger.info("✅ 競品分析完成")


# ══════════════════════════════════════════════════════════
def report():
    conn = db()
    print("\n【關鍵字機會排行】")
    rows = conn.execute(
        """SELECT * FROM keywords WHERE opportunity > 0
           ORDER BY opportunity DESC LIMIT 15""").fetchall()
    if rows:
        print(f"  {'關鍵字':38s} {'機會':>5s} {'中位觀看':>11s} {'中位訂閱':>11s}")
        for r in rows:
            print(f"  {r['term'][:36]:38s} {r['opportunity']:>5.2f} "
                  f"{r['top_median_views']:>11,} {r['top_median_subs']:>11,}")
    else:
        print("  尚無資料，請先執行 --research")

    print("\n【競品頻道】")
    rows = conn.execute(
        "SELECT * FROM competitors ORDER BY subs DESC LIMIT 12").fetchall()
    for r in rows:
        print(f"  {r['title'][:26]:28s} 訂閱 {r['subs']:>10,}  "
              f"近 30 天 {r['recent_uploads']:>2} 支  "
              f"均觀看 {r['avg_recent_views']:>8,}")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--research", action="store_true")
    ap.add_argument("--competitors", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.research:
        research(args.limit)
    if args.competitors:
        competitors()
    if args.report or not (args.research or args.competitors):
        report()


if __name__ == "__main__":
    main()

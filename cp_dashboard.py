#!/usr/bin/env python3
"""
安寵 Calm Paws — 產生真實數據版的 dashboard.html

dashboard.html 原本整支都是 mock 資料，寫死在 JS 裡（87 位新訂閱、
NT$285 收益、假留言 pomeranian_mom88...），從來沒有接上 metrics.db。
早期開發時當 UI 雛形做的，後來 cp_report.py 做出真正的資料收集
邏輯，但沒人回頭把這支接上——它就一直獨立躺在 repo 裡，長得像
「正在運作中的真實面板」，其實跟真實數據完全無關。

這支：
  1. 從 metrics.db 撈真實數據（重用 cp_report.gather() 的邏輯，
     另外補一段 8 週趨勢查詢）
  2. 把 dashboard.html 裡「REAL_DATA 標記區塊」整段換成真實 JSON
  3. 沒有留言自動回覆的真實資料來源（那個功能寫了程式碼，
     但從來沒有被排進 reel.yml 執行過，是死代碼）——原本那張卡片
     改成顯示真的 Reels 表現，而不是繼續編假留言

用法：
    python cp_dashboard.py                 產生並寫回 dashboard.html
    python cp_dashboard.py --check          只印資料，不寫檔
"""
import os
import re
import sys
import json
import sqlite3
import logging
import argparse
import datetime as dt
from pathlib import Path

BASE_DIR = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
DB_PATH = BASE_DIR / "data" / "metrics.db"
HTML_PATH = BASE_DIR / "dashboard.html"

sys.path.insert(0, str(BASE_DIR))
logger = logging.getLogger("cp_dashboard")

START_MARK = "// ⟦REAL_DATA_START⟧"
END_MARK = "// ⟦REAL_DATA_END⟧"

SCENE_LABEL = {
    "separation_anxiety": "分離焦慮", "sleep": "夜間助眠",
    "sleep_night": "夜間助眠", "relax": "日常放鬆",
    "vet_visit": "就醫紓解", "thunderstorm": "雷聲煙火",
    "kitten_calm": "幼貓安定", "senior_pet": "高齡犬貓",
    "car_travel": "車程焦慮",
}
SCENE_PILL = {
    "separation_anxiety": "sa", "sleep": "sl", "sleep_night": "sl",
    "relax": "rx", "vet_visit": "vet",
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def q1(conn, sql, params=()):
    rows = q(conn, sql, params)
    return dict(rows[0]) if rows else {}


def delta_pct(a, b):
    if not b:
        return None
    return round((a - b) / b * 100, 1)


# ══════════════════════════════════════════════════════════
def gather_dashboard(conn) -> dict:
    today = dt.date.today()
    w0 = (today - dt.timedelta(days=7)).isoformat()
    w1 = (today - dt.timedelta(days=14)).isoformat()

    d = {"lastUpdated": dt.datetime.now().isoformat()}

    # ── 累計 ──
    latest = q1(conn, """SELECT subs_total, watch_hours_365, ig_followers
                         FROM channel_daily
                         WHERE subs_total > 0
                         ORDER BY date DESC LIMIT 1""")
    total_views = q1(conn, "SELECT COALESCE(SUM(views),0) v FROM channel_daily")
    d["cumulative"] = {
        "totalViews": int(total_views.get("v", 0) or 0),
        "watchHours": round(latest.get("watch_hours_365", 0) or 0, 1),
        "subscribers": int(latest.get("subs_total", 0) or 0),
    }

    ig_latest = q1(conn, """SELECT ig_followers FROM channel_daily
                            WHERE ig_followers > 0 ORDER BY date DESC LIMIT 1""")

    # ── 本週 vs 上週 ──
    def window(start, end):
        return q1(conn, """SELECT COALESCE(SUM(views),0) views,
                                  COALESCE(SUM(watch_hours),0) wh,
                                  COALESCE(SUM(subs_gained),0) subs
                           FROM channel_daily WHERE date >= ? AND date < ?""",
                  (start, end))

    cur = window(w0, today.isoformat())
    prev = window(w1, w0)

    ig_cur = q1(conn, """SELECT COALESCE(SUM(reach),0) r FROM reel_metrics
                         WHERE snapshot_date >= ?""", (w0,))
    ig_prev = q1(conn, """SELECT COALESCE(SUM(reach),0) r FROM reel_metrics
                          WHERE snapshot_date >= ? AND snapshot_date < ?""",
                 (w1, w0))

    d["weekly"] = {
        "views": int(cur["views"]),
        "viewsDelta": delta_pct(cur["views"], prev["views"]),
        "watchHoursNew": round(cur["wh"], 1),
        "subscribers": int(cur["subs"]),
        "subscribersDelta": delta_pct(cur["subs"], prev["subs"]),
        "igReach": int(ig_cur.get("r", 0) or 0),
        "igDelta": delta_pct(ig_cur.get("r", 0) or 0, ig_prev.get("r", 0) or 0),
    }

    # ── 收益（真實估算，來自 cp_revenue） ──
    d["revenue"] = {"ytAdEstimate": 0, "cpm": 0}
    try:
        from cp_revenue import revenue_summary
        rev = revenue_summary(conn)
        d["revenue"]["ytAdEstimate"] = round(rev.get("yt_estimate_usd", 0) or 0, 0)
        d["revenue"]["cpm"] = round(rev.get("cpm", 0) or 0, 2)
    except Exception as e:
        logger.warning(f"營收資料讀取失敗，收益顯示為 0：{e}")

    # ── 本週爆款 Top 5（真實影片數據，沒有就是空的） ──
    videos = q(conn, """
        SELECT v.title, v.scene, m.views
        FROM videos v
        JOIN video_metrics m ON m.video_id = v.video_id
        WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM video_metrics
                                 WHERE video_id = v.video_id)
        ORDER BY m.views DESC LIMIT 5""")
    d["topVideos"] = [
        {"title": r["title"], "views": int(r["views"] or 0),
         "scene": SCENE_PILL.get(r["scene"], "rx")}
        for r in videos
    ]

    # ── 場景分佈（真實觀看數佔比） ──
    scene_rows = q(conn, """
        SELECT v.scene, SUM(m.views) v
        FROM videos v
        JOIN video_metrics m ON m.video_id = v.video_id
        WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM video_metrics
                                 WHERE video_id = v.video_id)
        GROUP BY v.scene HAVING v > 0 ORDER BY v DESC""")
    total_scene_views = sum(r["v"] for r in scene_rows) or 1
    d["sceneStats"] = {
        "labels": [SCENE_LABEL.get(r["scene"], r["scene"]) for r in scene_rows],
        "views": [round(r["v"] / total_scene_views * 100) for r in scene_rows],
    }

    # ── 8 週趨勢（真實：YouTube 用 channel_daily，IG 用 reel_metrics） ──
    labels, yt_trend, ig_trend = [], [], []
    for i in range(7, -1, -1):
        wk_end = today - dt.timedelta(days=i * 7)
        wk_start = wk_end - dt.timedelta(days=7)
        labels.append(f"{wk_start.month}/{wk_start.day}")
        v = q1(conn, """SELECT COALESCE(SUM(views),0) v FROM channel_daily
                        WHERE date >= ? AND date < ?""",
               (wk_start.isoformat(), wk_end.isoformat()))
        r = q1(conn, """SELECT COALESCE(SUM(reach),0) r FROM reel_metrics
                        WHERE snapshot_date >= ? AND snapshot_date < ?""",
               (wk_start.isoformat(), wk_end.isoformat()))
        yt_trend.append(int(v.get("v", 0) or 0))
        ig_trend.append(int(r.get("r", 0) or 0))
    d["weeklyTrend"] = {"labels": labels, "youtube": yt_trend, "instagram": ig_trend}

    # ── 最近發佈的 Reels（真實，取代原本編造的留言） ──
    reels = q(conn, """
        SELECT r.permalink, r.hook_style, r.published_at,
               m.reach, m.likes, m.saves
        FROM reels r
        JOIN reel_metrics m ON m.media_id = r.media_id
        WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM reel_metrics
                                 WHERE media_id = r.media_id)
        ORDER BY r.published_at DESC LIMIT 5""")
    d["recentReels"] = [
        {"permalink": r["permalink"] or "", "hook": r["hook_style"] or "",
         "reach": int(r["reach"] or 0), "likes": int(r["likes"] or 0),
         "saves": int(r["saves"] or 0)}
        for r in reels
    ]

    # ── 已發布影片數（真實，取代原本假的自動回覆計數） ──
    vc = q1(conn, "SELECT COUNT(*) c FROM videos")
    rc = q1(conn, "SELECT COUNT(*) c FROM reels")
    d["published"] = {"videos": vc.get("c", 0) or 0, "reels": rc.get("c", 0) or 0}

    # ── 本週實際自動決策（真實，來自 decisions 表） ──
    dec = q(conn, """SELECT ts, dimension, action, detail FROM decisions
                     WHERE ts >= ? ORDER BY ts DESC LIMIT 8""", (w0,))
    d["runLog"] = [
        {"time": r["ts"][:16].replace("T", " "),
         "status": "ok" if r["action"] != "停用失效選項" else "warn",
         "msg": f"{r['dimension']}：{r['action']}（{r['detail']}）"}
        for r in dec
    ]

    return d


# ══════════════════════════════════════════════════════════
def render(data: dict, html_path: Path = HTML_PATH) -> bool:
    html = html_path.read_text(encoding="utf-8")
    if START_MARK not in html or END_MARK not in html:
        logger.error("dashboard.html 裡找不到 REAL_DATA 標記，無法安全替換，中止")
        return False

    before, rest = html.split(START_MARK, 1)
    _, after = rest.split(END_MARK, 1)

    block = (f"{START_MARK}\nconst REAL_DATA = "
             f"{json.dumps(data, ensure_ascii=False, indent=2)};\n{END_MARK}")

    new_html = before + block + after
    html_path.write_text(new_html, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只印資料，不寫檔")
    ap.add_argument("--html", default=str(HTML_PATH))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not DB_PATH.exists():
        logger.error(f"找不到資料庫：{DB_PATH}")
        sys.exit(1)

    conn = db()
    data = gather_dashboard(conn)
    conn.close()

    if args.check:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    ok = render(data, Path(args.html))
    if ok:
        logger.info(f"✅ 已用真實數據更新 {args.html}")
        logger.info(f"   累計訂閱 {data['cumulative']['subscribers']}，"
                    f"累計觀看時數 {data['cumulative']['watchHours']}h，"
                    f"已發布 {data['published']['videos']} 支長片 / "
                    f"{data['published']['reels']} 支 Reel")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()

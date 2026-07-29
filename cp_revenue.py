#!/usr/bin/env python3
"""
安寵 Calm Paws — 營收追蹤與獲利路徑推估

兩條獲利線：
  1. YouTube 廣告分潤 — 需達 1000 訂閱 + 12 個月內 4000 小時公開觀看
  2. 聯盟行銷 — 無門檻，但需要流量

關於聯盟點擊追蹤的誠實說明：
  真正的點擊追蹤需要一台伺服器來接轉址並計數，那是要花錢的。
  這裡改用聯盟平台自己的歸因機制 —— 產生帶 sub-ID 的連結，
  平台後台就能分辨流量來自哪支影片。成效數字要從對方後台抄回來，
  這支提供記錄與彙整，週報會呈現趨勢。

  這是無預算下的正確做法，不是妥協 ——
  平台後台的轉換數據本來就比自建追蹤準確。

用法：
    python cp_revenue.py --status              目前營收與 YPP 進度
    python cp_revenue.py --project             推估達標時間
    python cp_revenue.py --link <平台> <商品網址> --video <id>
    python cp_revenue.py --record <平台> --clicks N --orders N --revenue N
"""
import os
import sys
import json
import sqlite3
import logging
import argparse
import datetime as dt
import urllib.parse
from pathlib import Path

BASE_DIR = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
DB_PATH = BASE_DIR / "data" / "metrics.db"

logger = logging.getLogger("cp_revenue")

YPP_SUBS = 1000
YPP_HOURS = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS affiliate_links (
    link_id     TEXT PRIMARY KEY,
    platform    TEXT,
    product     TEXT,
    target_url  TEXT,
    tracked_url TEXT,
    video_id    TEXT,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS affiliate_performance (
    period      TEXT,
    platform    TEXT,
    clicks      INTEGER DEFAULT 0,
    orders      INTEGER DEFAULT 0,
    revenue_twd REAL DEFAULT 0,
    note        TEXT,
    recorded_at TEXT,
    PRIMARY KEY (period, platform)
);

CREATE TABLE IF NOT EXISTS revenue_ledger (
    period      TEXT,
    source      TEXT,
    amount_twd  REAL DEFAULT 0,
    note        TEXT,
    recorded_at TEXT,
    PRIMARY KEY (period, source)
);
"""

# 各平台的 sub-ID 參數名稱。用來讓對方後台分辨流量來源。
PLATFORMS = {
    "momo": {"param": "utm_content", "name": "momo 購物網"},
    "shopee": {"param": "utm_content", "name": "蝦皮分潤計畫"},
    "pchome": {"param": "utm_content", "name": "PChome"},
    "amazon": {"param": "ascsubtag", "name": "Amazon Associates"},
    "other": {"param": "utm_content", "name": "其他"},
}


def db() -> sqlite3.Connection:
    """
    連線並建表。

    本模組讀取 channel_daily / costs 等表，但那些是 cp_analytics 定義的。
    全新環境下若只建本模組的表，讀取時會 no such table 而中斷，
    所以優先沿用 cp_analytics 的完整 schema。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        sys.path.insert(0, str(BASE_DIR))
        from cp_analytics import SCHEMA as BASE_SCHEMA
        conn.executescript(BASE_SCHEMA)
    except Exception:
        # 至少把本模組會讀到的表建起來，避免查詢時炸掉
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channel_daily (
                date TEXT PRIMARY KEY, subs_total INTEGER DEFAULT 0,
                subs_gained INTEGER DEFAULT 0, subs_lost INTEGER DEFAULT 0,
                views INTEGER DEFAULT 0, watch_hours REAL DEFAULT 0,
                watch_hours_365 REAL DEFAULT 0, ig_followers INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS costs (
                date TEXT, service TEXT, usd REAL DEFAULT 0,
                PRIMARY KEY (date, service));
        """)
    conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


# ══════════════════════════════════════════════════════════
#  YPP 進度與推估
# ══════════════════════════════════════════════════════════
def ypp_status(conn) -> dict:
    row = conn.execute(
        """SELECT subs_total, watch_hours_365, date FROM channel_daily
           WHERE watch_hours_365 > 0 OR subs_total > 0
           ORDER BY date DESC LIMIT 1""").fetchone()
    subs = (row["subs_total"] if row else 0) or 0
    hours = (row["watch_hours_365"] if row else 0) or 0
    return {
        "subs": subs, "hours": round(hours, 1),
        "subs_pct": round(min(100, subs / YPP_SUBS * 100), 1),
        "hours_pct": round(min(100, hours / YPP_HOURS * 100), 1),
        "as_of": row["date"] if row else None,
    }


def growth_rate(conn, days: int = 28) -> dict:
    """回傳每日平均增長。資料不足就回 0 並標記不可靠。"""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT date, subs_gained, watch_hours FROM channel_daily
           WHERE date >= ? ORDER BY date""", (since,)).fetchall()
    if len(rows) < 7:
        return {"subs_per_day": 0.0, "hours_per_day": 0.0,
                "days": len(rows), "reliable": False}
    subs = sum((r["subs_gained"] or 0) for r in rows)
    hrs = sum((r["watch_hours"] or 0) for r in rows)
    n = len(rows)
    return {
        "subs_per_day": round(subs / n, 2),
        "hours_per_day": round(hrs / n, 2),
        "days": n,
        "reliable": n >= 21 and (subs > 0 or hrs > 0),
    }


def project_ypp(conn) -> dict:
    """
    推估達標時間。

    這是線性外推，不是預測模型。頻道成長通常不是線性的 ——
    可能某支影片突然被推播就全變了，也可能一直停在原地。
    數字當成「照目前速度會怎樣」，不要當成承諾。
    """
    st = ypp_status(conn)
    gr = growth_rate(conn)

    out = {"status": st, "rate": gr}
    if not gr["reliable"]:
        out["verdict"] = "資料不足，至少需要 21 天且有實際成長才能推估"
        return out

    need_subs = max(0, YPP_SUBS - st["subs"])
    need_hours = max(0, YPP_HOURS - st["hours"])

    d_subs = (need_subs / gr["subs_per_day"]) if gr["subs_per_day"] > 0 else None
    d_hours = (need_hours / gr["hours_per_day"]) if gr["hours_per_day"] > 0 else None

    out["days_to_subs"] = round(d_subs) if d_subs else None
    out["days_to_hours"] = round(d_hours) if d_hours else None

    cands = [d for d in (d_subs, d_hours) if d]
    if not cands:
        out["verdict"] = "目前成長率為零，照這個速度不會達標"
    else:
        worst = max(cands)
        eta = dt.date.today() + dt.timedelta(days=int(worst))
        out["eta"] = eta.isoformat()
        out["days_total"] = round(worst)
        if worst > 730:
            out["verdict"] = (f"照目前速度約需 {round(worst/365,1)} 年。"
                              f"這個時程下，廣告分潤不該是主要獲利指望")
        elif worst > 365:
            out["verdict"] = f"照目前速度約需 {round(worst/30)} 個月"
        else:
            out["verdict"] = f"照目前速度約 {eta.isoformat()} 達標"

        # 哪一項是瓶頸
        if d_subs and d_hours:
            out["bottleneck"] = "訂閱數" if d_subs > d_hours else "觀看時數"
    return out


# ══════════════════════════════════════════════════════════
#  聯盟連結
# ══════════════════════════════════════════════════════════
def make_link(conn, platform: str, url: str, product: str,
              video_id: str = "") -> str:
    """產生帶 sub-ID 的追蹤連結"""
    p = PLATFORMS.get(platform, PLATFORMS["other"])
    stamp = dt.datetime.now().strftime("%Y%m%d")
    link_id = f"cp_{platform}_{video_id or 'gen'}_{stamp}"

    parsed = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parsed.query))
    q[p["param"]] = link_id
    q.setdefault("utm_source", "calmpaws")
    q.setdefault("utm_medium", "youtube" if video_id else "social")
    tracked = urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(q)))

    conn.execute(
        """INSERT OR REPLACE INTO affiliate_links
           (link_id, platform, product, target_url, tracked_url,
            video_id, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (link_id, platform, product, url, tracked, video_id,
         dt.datetime.now().isoformat()))
    conn.commit()
    return tracked


def record_performance(conn, platform: str, clicks: int, orders: int,
                       revenue: float, period: str = None, note: str = ""):
    period = period or dt.date.today().strftime("%Y-%m")
    conn.execute(
        """INSERT INTO affiliate_performance
           (period, platform, clicks, orders, revenue_twd, note, recorded_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(period, platform) DO UPDATE SET
             clicks=excluded.clicks, orders=excluded.orders,
             revenue_twd=excluded.revenue_twd, note=excluded.note,
             recorded_at=excluded.recorded_at""",
        (period, platform, clicks, orders, revenue, note,
         dt.datetime.now().isoformat()))
    conn.execute(
        """INSERT INTO revenue_ledger (period, source, amount_twd, note, recorded_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(period, source) DO UPDATE SET
             amount_twd=excluded.amount_twd, recorded_at=excluded.recorded_at""",
        (period, f"affiliate_{platform}", revenue, note,
         dt.datetime.now().isoformat()))
    conn.commit()


def revenue_summary(conn, months: int = 6) -> dict:
    since = (dt.date.today().replace(day=1)
             - dt.timedelta(days=31 * months)).strftime("%Y-%m")
    rows = conn.execute(
        """SELECT period, source, SUM(amount_twd) amt FROM revenue_ledger
           WHERE period >= ? GROUP BY period, source
           ORDER BY period DESC""", (since,)).fetchall()
    by_period = {}
    for r in rows:
        by_period.setdefault(r["period"], {})[r["source"]] = round(r["amt"], 2)

    costs = conn.execute(
        """SELECT substr(date,1,7) period, SUM(usd) usd FROM costs
           WHERE date >= ? GROUP BY substr(date,1,7)""",
        (since + "-01",)).fetchall()
    cost_by_period = {c["period"]: round((c["usd"] or 0) * 32, 0)
                      for c in costs}     # 粗估匯率，僅供參考

    total_rev = sum(sum(v.values()) for v in by_period.values())
    total_cost = sum(cost_by_period.values())
    return {
        "by_period": by_period,
        "cost_by_period": cost_by_period,
        "total_revenue_twd": round(total_rev, 2),
        "total_cost_twd": round(total_cost, 2),
        "net_twd": round(total_rev - total_cost, 2),
    }


# ══════════════════════════════════════════════════════════
def cmd_status(conn):
    st = ypp_status(conn)
    print("\n【YouTube 合作夥伴計畫進度】")
    print(f"  訂閱數     {st['subs']:>8,} / {YPP_SUBS:,}   {st['subs_pct']:>5.1f}%")
    print(f"  觀看時數   {st['hours']:>8,.1f} / {YPP_HOURS:,}   {st['hours_pct']:>5.1f}%")
    if st["as_of"]:
        print(f"  資料日期   {st['as_of']}")

    rev = revenue_summary(conn)
    print("\n【營收】")
    if rev["by_period"]:
        for period in sorted(rev["by_period"], reverse=True)[:6]:
            srcs = rev["by_period"][period]
            cost = rev["cost_by_period"].get(period, 0)
            total = sum(srcs.values())
            print(f"  {period}  收入 NT${total:>8,.0f}  "
                  f"成本 NT${cost:>7,.0f}  淨 NT${total-cost:>8,.0f}")
            for s, a in srcs.items():
                print(f"           {s:24s} NT${a:>8,.0f}")
    else:
        print("  尚無營收記錄")
        if rev["cost_by_period"]:
            print("  已投入成本：")
            for p, c in sorted(rev["cost_by_period"].items(), reverse=True)[:6]:
                print(f"    {p}  NT${c:,.0f}")
    print(f"\n  累計   收入 NT${rev['total_revenue_twd']:,.0f}   "
          f"成本 NT${rev['total_cost_twd']:,.0f}   "
          f"淨 NT${rev['net_twd']:,.0f}")

    links = conn.execute(
        "SELECT COUNT(*) n FROM affiliate_links").fetchone()["n"]
    print(f"\n【聯盟連結】已產生 {links} 條")


def cmd_project(conn):
    p = project_ypp(conn)
    st, gr = p["status"], p["rate"]
    print("\n【達標推估】")
    print(f"  近 {gr['days']} 天平均：每日 +{gr['subs_per_day']} 訂閱，"
          f"+{gr['hours_per_day']} 觀看時數")
    print()
    if not gr["reliable"]:
        print(f"  {p['verdict']}")
        print()
        print("  這不是壞消息 —— 新頻道本來就需要時間累積曝光。")
        print("  現階段該看的是曝光次數有沒有成長，而不是訂閱數。")
        return
    if p.get("days_to_subs"):
        print(f"  訂閱達標   約 {p['days_to_subs']:,} 天")
    if p.get("days_to_hours"):
        print(f"  時數達標   約 {p['days_to_hours']:,} 天")
    if p.get("bottleneck"):
        print(f"  瓶頸       {p['bottleneck']}")
    print()
    print(f"  {p['verdict']}")
    print()
    print("  提醒：這是線性外推，不是預測。頻道成長通常不是線性的，")
    print("  一支影片被推播就可能完全改變曲線。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--project", action="store_true")
    ap.add_argument("--link", nargs=2, metavar=("平台", "網址"))
    ap.add_argument("--product", default="")
    ap.add_argument("--video", default="")
    ap.add_argument("--record", metavar="平台")
    ap.add_argument("--clicks", type=int, default=0)
    ap.add_argument("--orders", type=int, default=0)
    ap.add_argument("--revenue", type=float, default=0)
    ap.add_argument("--period", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = db()

    if args.link:
        platform, url = args.link
        tracked = make_link(conn, platform, url,
                            args.product or url[:60], args.video)
        print(f"\n平台：{PLATFORMS.get(platform, PLATFORMS['other'])['name']}")
        print(f"追蹤連結：\n{tracked}\n")
        print("把這條連結放進影片說明欄或 IG 簡介。")
        print("成效請到該平台後台查看，再用 --record 記錄回來。")
    elif args.record:
        record_performance(conn, args.record, args.clicks, args.orders,
                           args.revenue, args.period)
        print(f"✅ 已記錄 {args.record}：{args.clicks} 點擊、"
              f"{args.orders} 筆訂單、NT${args.revenue:,.0f}")
    elif args.project:
        if args.json:
            print(json.dumps(project_ypp(conn), ensure_ascii=False, indent=2))
        else:
            cmd_project(conn)
    else:
        if args.json:
            print(json.dumps({"ypp": ypp_status(conn),
                              "revenue": revenue_summary(conn),
                              "projection": project_ypp(conn)},
                             ensure_ascii=False, indent=2, default=str))
        else:
            cmd_status(conn)
            cmd_project(conn)

    conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
安寵 Calm Paws — 每週一營運報告

產出 HTML 報告並自動開啟。內容：
  1. 北極星指標（YPP 進度）
  2. 本週 vs 上週 vs 4 週均值
  3. 各內容表現
  4. 優化引擎目前信念與本週自動決策
  5. 風險燈號
  6. 下週產出計畫

用法：python cp_report.py [--no-open]
"""
import os
import sys
import json
import sqlite3
import logging
import argparse
import datetime as dt
import subprocess
from pathlib import Path

# 本機跑在 ~/calm_paws，GitHub Actions 跑在 workspace 目錄
BASE_DIR = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
DB_PATH = BASE_DIR / "data" / "metrics.db"
REPORT_DIR = BASE_DIR / "reports"

sys.path.insert(0, str(BASE_DIR))
try:
    from cp_optimizer import arm_status, check_health, pick, DIMENSIONS
except ImportError:
    arm_status = check_health = pick = None
    DIMENSIONS = {}

logger = logging.getLogger("cp_report")

DIM_LABEL = {
    "scene": "主題情境",
    "title_formula": "標題公式",
    "thumb_style": "縮圖風格",
    "duration_h": "影片長度",
    "upload_slot": "發布時段",
    "reel_hook": "Reel 開場",
    "hashtag_set": "標籤組合",
}

VAL_LABEL = {
    "separation_anxiety": "分離焦慮", "sleep_night": "夜間助眠",
    "thunderstorm": "雷聲煙火", "vet_visit": "就醫緊張",
    "kitten_calm": "幼貓安定", "senior_pet": "高齡犬貓",
    "car_travel": "車程焦慮",
    "problem_first": "問題導向", "scenario_first": "情境導向",
    "outcome_first": "效果導向", "duration_first": "時長導向",
    "big_text_closeup": "大字特寫", "minimal_illust": "極簡插畫",
    "realistic_sleep": "寫實睡眠", "split_beforeafter": "前後對比",
    "mon_08": "週一 08:00", "wed_20": "週三 20:00", "sat_10": "週六 10:00",
    "pet_sleeping_immediate": "直接入睡畫面", "text_question": "文字提問",
    "owner_pov": "飼主視角", "before_after": "焦慮→放鬆",
    "broad_tw": "大眾台灣標籤", "niche_behavior": "行為利基標籤",
    "mixed": "混合標籤",
}


def lab(v):
    return VAL_LABEL.get(str(v), str(v))


def db():
    # row_factory 必須在任何查詢前就設好；check_health 會先跑，
    # 它內部用 dict(row)，拿到純 tuple 會炸掉。
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def q1(conn, sql, params=()):
    rows = q(conn, sql, params)
    return dict(rows[0]) if rows else {}


# ══════════════════════════════════════════════════════════
def gather(conn) -> dict:
    today = dt.date.today()
    w0 = (today - dt.timedelta(days=7)).isoformat()
    w1 = (today - dt.timedelta(days=14)).isoformat()
    w4 = (today - dt.timedelta(days=28)).isoformat()
    tod = today.isoformat()

    d = {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
         "week_of": today.strftime("%Y 年 %m 月 %d 日")}

    # ── 北極星：YPP 進度 ──
    latest = q1(conn, """SELECT subs_total, watch_hours_365, ig_followers
                         FROM channel_daily
                         WHERE watch_hours_365 > 0
                         ORDER BY date DESC LIMIT 1""")
    subs = latest.get("subs_total", 0) or 0
    wh365 = latest.get("watch_hours_365", 0) or 0

    ig = q1(conn, """SELECT ig_followers FROM channel_daily
                     WHERE ig_followers > 0 ORDER BY date DESC LIMIT 1""")

    d["ypp"] = {
        "subs": subs, "subs_target": 1000,
        "subs_pct": min(100, round(subs / 10, 1)),
        "wh": round(wh365, 1), "wh_target": 4000,
        "wh_pct": min(100, round(wh365 / 40, 1)),
        "ig_followers": ig.get("ig_followers", 0) or 0,
    }

    # ── 週對比 ──
    def window(start, end):
        r = q1(conn, """SELECT COALESCE(SUM(views),0) views,
                               COALESCE(SUM(watch_hours),0) wh,
                               COALESCE(SUM(subs_gained),0) subs
                        FROM channel_daily WHERE date >= ? AND date < ?""",
               (start, end))
        return r

    cur = window(w0, tod)
    prev = window(w1, w0)
    four = q1(conn, """SELECT COALESCE(SUM(views),0)/4.0 views,
                              COALESCE(SUM(watch_hours),0)/4.0 wh,
                              COALESCE(SUM(subs_gained),0)/4.0 subs
                       FROM channel_daily WHERE date >= ? AND date < ?""",
              (w4, tod))

    def delta(a, b):
        if not b:
            return None
        return round((a - b) / b * 100, 1)

    d["weekly"] = {
        "views": {"cur": int(cur["views"]), "prev": int(prev["views"]),
                  "avg4": round(four["views"], 1),
                  "delta": delta(cur["views"], prev["views"])},
        "watch_hours": {"cur": round(cur["wh"], 1), "prev": round(prev["wh"], 1),
                        "avg4": round(four["wh"], 1),
                        "delta": delta(cur["wh"], prev["wh"])},
        "subs": {"cur": int(cur["subs"]), "prev": int(prev["subs"]),
                 "avg4": round(four["subs"], 1),
                 "delta": delta(cur["subs"], prev["subs"])},
    }

    # ── 影片表現 ──
    d["videos"] = [dict(r) for r in q(conn, """
        SELECT v.video_id, v.title, v.scene, v.title_formula, v.thumb_style,
               v.published_at, m.views, m.watch_hours, m.ctr, m.avd_sec,
               m.subs_gained, m.age_days
        FROM videos v
        JOIN video_metrics m ON m.video_id = v.video_id
        WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM video_metrics
                                 WHERE video_id = v.video_id)
        ORDER BY v.published_at DESC LIMIT 12""")]

    # ── Reel 表現 ──
    d["reels"] = [dict(r) for r in q(conn, """
        SELECT r.media_id, r.permalink, r.hook_style, r.published_at,
               m.reach, m.plays, m.saves, m.shares, m.likes, m.comments
        FROM reels r
        JOIN reel_metrics m ON m.media_id = r.media_id
        WHERE m.snapshot_date = (SELECT MAX(snapshot_date) FROM reel_metrics
                                 WHERE media_id = r.media_id)
        ORDER BY r.published_at DESC LIMIT 10""")]

    # ── 優化信念 ──
    d["arms"] = arm_status(conn) if arm_status else {}

    # ── 本週決策 ──
    d["decisions"] = [dict(r) for r in q(conn, """
        SELECT ts, dimension, action, detail, auto_applied
        FROM decisions WHERE ts >= ? ORDER BY ts DESC""", (w0,))]

    # ── 風險 ──
    d["flags"] = [dict(r) for r in q(conn, """
        SELECT ts, severity, kind, detail FROM policy_flags
        WHERE ts >= ? ORDER BY
        CASE severity WHEN 'critical' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END""",
        (w0,))]

    # ── 成本 ──
    d["costs"] = [dict(r) for r in q(conn, """
        SELECT service, SUM(usd) usd FROM costs
        WHERE date >= ? GROUP BY service""", (w0,))]
    d["cost_total"] = round(sum(c["usd"] or 0 for c in d["costs"]), 2)

    # ── 下週計畫 ──
    d["plan"] = []
    if pick:
        try:
            for i in range(3):
                p = pick(conn, "youtube")
                d["plan"].append({k: v for k, v in p.items()
                                  if not k.startswith("_")})
        except Exception as e:
            logger.warning(f"產生計畫失敗：{e}")

    return d


# ══════════════════════════════════════════════════════════
def render(d: dict) -> str:
    def arrow(v):
        if v is None:
            return '<span class="flat">—</span>'
        if v > 0:
            return f'<span class="up">▲ {v}%</span>'
        if v < 0:
            return f'<span class="down">▼ {abs(v)}%</span>'
        return '<span class="flat">— 0%</span>'

    ypp = d["ypp"]
    wk = d["weekly"]

    # 風險燈號
    crit = [f for f in d["flags"] if f["severity"] == "critical"]
    warn = [f for f in d["flags"] if f["severity"] == "warn"]
    if crit:
        light, light_txt = "red", "需要立即處理"
    elif warn:
        light, light_txt = "amber", "有待觀察項目"
    else:
        light, light_txt = "green", "運作正常"

    # 影片列
    vrows = ""
    for v in d["videos"]:
        dur = v.get("avd_sec") or 0
        vrows += f"""<tr>
          <td class="ttl"><a href="https://youtu.be/{v['video_id']}" target="_blank">
            {(v.get('title') or v['video_id'])[:42]}</a>
            <div class="sub">{lab(v.get('scene'))} · {lab(v.get('title_formula'))} · {lab(v.get('thumb_style'))}</div></td>
          <td class="num">{v.get('age_days', 0)}d</td>
          <td class="num">{v.get('views', 0):,}</td>
          <td class="num">{round(v.get('watch_hours') or 0, 1)}</td>
          <td class="num">{round(v.get('ctr') or 0, 1)}%</td>
          <td class="num">{int(dur // 60)}分{int(dur % 60)}秒</td>
          <td class="num">{v.get('subs_gained', 0)}</td>
        </tr>"""
    if not vrows:
        vrows = '<tr><td colspan="7" class="empty">尚無影片數據</td></tr>'

    # Reel 列
    rrows = ""
    for r in d["reels"]:
        link = r.get("permalink") or "#"
        rrows += f"""<tr>
          <td class="ttl"><a href="{link}" target="_blank">
            {(r.get('published_at') or '')[:10]}</a>
            <div class="sub">{lab(r.get('hook_style'))}</div></td>
          <td class="num">{r.get('reach', 0):,}</td>
          <td class="num">{r.get('plays', 0):,}</td>
          <td class="num">{r.get('saves', 0)}</td>
          <td class="num">{r.get('shares', 0)}</td>
          <td class="num">{r.get('likes', 0)}</td>
        </tr>"""
    if not rrows:
        rrows = '<tr><td colspan="6" class="empty">尚無 Reel 數據</td></tr>'

    # 優化信念
    arms_html = ""
    for dim, arms in d["arms"].items():
        if not arms:
            continue
        bars = ""
        for a in arms:
            w = int(a["mean"] * 100)
            lo, hi = int(a["ci_low"] * 100), int(a["ci_high"] * 100)
            cls = "" if a["active"] else " muted"
            conf = "低" if a["n"] < 4 else ("中" if a["n"] < 10 else "高")
            bars += f"""<div class="arm{cls}">
              <div class="arm-name">{lab(a['value'])}{'' if a['active'] else ' （已停用）'}</div>
              <div class="bar-wrap">
                <div class="ci" style="left:{lo}%;width:{max(hi-lo,1)}%"></div>
                <div class="bar" style="width:{w}%"></div>
              </div>
              <div class="arm-val">{a['mean']:.2f} <span class="n">n={a['n']} 信心{conf}</span></div>
            </div>"""
        arms_html += f"""<div class="dim">
          <h4>{DIM_LABEL.get(dim, dim)}</h4>{bars}</div>"""
    if not arms_html:
        arms_html = '<p class="empty">優化引擎尚未累積數據</p>'

    # 決策
    dec_html = ""
    for x in d["decisions"]:
        tag = "自動執行" if x["auto_applied"] else "待確認"
        cls = "auto" if x["auto_applied"] else "pending"
        dec_html += f"""<li><span class="tag {cls}">{tag}</span>
          <b>{DIM_LABEL.get(x['dimension'], x['dimension'])}</b> — {x['action']}
          <div class="sub">{x['detail']}</div></li>"""
    if not dec_html:
        dec_html = '<li class="empty">本週無變更</li>'

    # 風險
    flag_html = ""
    for f in d["flags"]:
        cls = "crit" if f["severity"] == "critical" else "warn"
        flag_html += f"""<li class="{cls}"><b>{f['kind']}</b>
          <div class="sub">{f['detail']}</div></li>"""
    if not flag_html:
        flag_html = '<li class="ok">未偵測到異常</li>'

    # 計畫
    plan_html = ""
    for i, p in enumerate(d["plan"], 1):
        parts = " · ".join(
            f"{DIM_LABEL.get(k, k)}：{lab(v)}" for k, v in p.items())
        plan_html += f'<li><b>第 {i} 支</b><div class="sub">{parts}</div></li>'
    if not plan_html:
        plan_html = '<li class="empty">尚未產生計畫</li>'

    cost_html = " · ".join(
        f"{c['service']} ${round(c['usd'] or 0, 2)}" for c in d["costs"]
    ) or "無記錄"

    return f"""<!DOCTYPE html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>安寵 Calm Paws 週報 — {d['week_of']}</title>
<style>
:root {{
  --bg:#0f1115; --card:#171a21; --line:#252932; --tx:#e6e8ec;
  --dim:#8b919e; --acc:#7aa2f7; --up:#7ec699; --down:#e0796f; --amber:#e0b877;
}}
*{{box-sizing:border-box}}
body{{margin:0;padding:28px 20px 60px;background:var(--bg);color:var(--tx);
  font-family:-apple-system,"PingFang TC","Noto Sans TC",sans-serif;line-height:1.6}}
.wrap{{max-width:1080px;margin:0 auto}}
header{{margin-bottom:28px;padding-bottom:20px;border-bottom:1px solid var(--line)}}
h1{{margin:0 0 6px;font-size:26px;letter-spacing:.3px}}
.meta{{color:var(--dim);font-size:13px}}
.light{{display:inline-block;padding:5px 14px;border-radius:20px;font-size:13px;
  font-weight:600;margin-left:10px}}
.light.green{{background:rgba(126,198,153,.15);color:var(--up)}}
.light.amber{{background:rgba(224,184,119,.15);color:var(--amber)}}
.light.red{{background:rgba(224,121,111,.15);color:var(--down)}}
section{{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:22px;margin-bottom:18px}}
h2{{margin:0 0 16px;font-size:16px;color:var(--acc);letter-spacing:.5px}}
h4{{margin:16px 0 8px;font-size:13px;color:var(--dim)}}
.goals{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}
@media(max-width:640px){{.goals{{grid-template-columns:1fr}}}}
.goal-label{{display:flex;justify-content:space-between;font-size:14px;margin-bottom:8px}}
.goal-num{{font-variant-numeric:tabular-nums;color:var(--tx);font-weight:600}}
.track{{height:10px;background:#0b0d11;border-radius:6px;overflow:hidden}}
.fill{{height:100%;background:linear-gradient(90deg,var(--acc),#9ece6a);border-radius:6px}}
.goal-note{{font-size:12px;color:var(--dim);margin-top:6px}}
.kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}
@media(max-width:640px){{.kpis{{grid-template-columns:1fr}}}}
.kpi{{background:#0b0d11;border-radius:10px;padding:16px}}
.kpi .k{{font-size:12px;color:var(--dim)}}
.kpi .v{{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;margin:4px 0}}
.kpi .c{{font-size:12px;color:var(--dim)}}
.up{{color:var(--up)}} .down{{color:var(--down)}} .flat{{color:var(--dim)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:var(--dim);font-weight:500;padding:8px 6px;
  border-bottom:1px solid var(--line);font-size:12px}}
td{{padding:10px 6px;border-bottom:1px solid #1c1f27;vertical-align:top}}
td.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
td.ttl a{{color:var(--tx);text-decoration:none}}
td.ttl a:hover{{color:var(--acc)}}
.sub{{font-size:11px;color:var(--dim);margin-top:3px}}
.empty{{color:var(--dim);font-style:italic;padding:14px 6px}}
.dim{{margin-bottom:18px}}
.arm{{display:grid;grid-template-columns:130px 1fr 130px;gap:10px;
  align-items:center;margin-bottom:6px;font-size:12px}}
@media(max-width:640px){{.arm{{grid-template-columns:1fr}}}}
.arm.muted{{opacity:.38}}
.arm-name{{color:var(--tx)}}
.bar-wrap{{position:relative;height:14px;background:#0b0d11;border-radius:4px}}
.ci{{position:absolute;top:0;height:100%;background:rgba(122,162,247,.18);border-radius:4px}}
.bar{{position:absolute;top:4px;height:6px;background:var(--acc);border-radius:3px}}
.arm-val{{text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}}
.n{{font-size:10px;opacity:.75}}
ul{{margin:0;padding-left:0;list-style:none}}
li{{padding:10px 0;border-bottom:1px solid #1c1f27}}
li:last-child{{border-bottom:none}}
li.crit{{border-left:3px solid var(--down);padding-left:12px}}
li.warn{{border-left:3px solid var(--amber);padding-left:12px}}
li.ok{{color:var(--up)}}
.tag{{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;
  margin-right:8px;vertical-align:middle}}
.tag.auto{{background:rgba(126,198,153,.15);color:var(--up)}}
.tag.pending{{background:rgba(224,184,119,.15);color:var(--amber)}}
.note{{font-size:12px;color:var(--dim);margin-top:14px;padding-top:14px;
  border-top:1px solid var(--line)}}
</style></head><body><div class="wrap">

<header>
  <h1>安寵 Calm Paws 營運週報
    <span class="light {light}">{light_txt}</span></h1>
  <div class="meta">{d['week_of']} · 產生於 {d['generated']}</div>
</header>

<section>
  <h2>北極星指標 — YouTube 合作夥伴計畫進度</h2>
  <div class="goals">
    <div>
      <div class="goal-label"><span>訂閱數</span>
        <span class="goal-num">{ypp['subs']:,} / 1,000</span></div>
      <div class="track"><div class="fill" style="width:{ypp['subs_pct']}%"></div></div>
      <div class="goal-note">達成 {ypp['subs_pct']}%</div>
    </div>
    <div>
      <div class="goal-label"><span>近 12 個月公開觀看時數</span>
        <span class="goal-num">{ypp['wh']:,} / 4,000 小時</span></div>
      <div class="track"><div class="fill" style="width:{ypp['wh_pct']}%"></div></div>
      <div class="goal-note">達成 {ypp['wh_pct']}%</div>
    </div>
  </div>
  <div class="note">Instagram 追蹤者 {ypp['ig_followers']:,} 人。
    兩項門檻都達成才能申請廣告分潤；8 小時長片的優勢在於單次觀看即可累積大量時數，
    但前提是留存率不能太低。</div>
</section>

<section>
  <h2>本週表現</h2>
  <div class="kpis">
    <div class="kpi"><div class="k">觀看次數</div>
      <div class="v">{wk['views']['cur']:,}</div>
      <div class="c">{arrow(wk['views']['delta'])} 對比上週 {wk['views']['prev']:,}
        · 4 週均 {wk['views']['avg4']:,}</div></div>
    <div class="kpi"><div class="k">觀看時數</div>
      <div class="v">{wk['watch_hours']['cur']:,}</div>
      <div class="c">{arrow(wk['watch_hours']['delta'])} 對比上週 {wk['watch_hours']['prev']:,}
        · 4 週均 {wk['watch_hours']['avg4']:,}</div></div>
    <div class="kpi"><div class="k">新增訂閱</div>
      <div class="v">{wk['subs']['cur']:,}</div>
      <div class="c">{arrow(wk['subs']['delta'])} 對比上週 {wk['subs']['prev']:,}
        · 4 週均 {wk['subs']['avg4']:,}</div></div>
  </div>
</section>

<section>
  <h2>影片表現</h2>
  <table><thead><tr>
    <th>影片</th><th class="num">天數</th><th class="num">觀看</th>
    <th class="num">時數</th><th class="num">CTR</th>
    <th class="num">平均觀看</th><th class="num">訂閱</th>
  </tr></thead><tbody>{vrows}</tbody></table>
</section>

<section>
  <h2>Instagram Reel 表現</h2>
  <table><thead><tr>
    <th>貼文</th><th class="num">觸及</th><th class="num">播放</th>
    <th class="num">收藏</th><th class="num">分享</th><th class="num">讚</th>
  </tr></thead><tbody>{rrows}</tbody></table>
</section>

<section>
  <h2>優化引擎目前信念</h2>
  {arms_html}
  <div class="note">橫條為預估平均報酬，淡色區間是 95% 信賴區間。
    區間越寬代表樣本越少、越不可信。n 少於 4 時請把排序當成雜訊，
    不要據此下結論。</div>
</section>

<section>
  <h2>本週自動執行的調整</h2>
  <ul>{dec_html}</ul>
</section>

<section>
  <h2>風險與健康度</h2>
  <ul>{flag_html}</ul>
  <div class="note">差異化不足是最需要警覺的項目。
    YouTube 對大量製造、彼此高度相似的內容審查趨嚴，
    一旦被判定為重複內容會直接影響營利資格。</div>
</section>

<section>
  <h2>下週產出計畫</h2>
  <ul>{plan_html}</ul>
  <div class="note">本週 API 成本：${d['cost_total']} （{cost_html}）</div>
</section>

</div></body></html>"""


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    conn = db()

    # 產報告前先跑一次健康檢查
    if check_health:
        try:
            check_health(conn)
        except Exception as e:
            logger.warning(f"健康檢查失敗：{e}")

    data = gather(conn)
    html = render(data)

    stamp = dt.date.today().isoformat()
    path = REPORT_DIR / f"weekly_{stamp}.html"
    path.write_text(html, encoding="utf-8")

    latest = REPORT_DIR / "latest.html"
    latest.write_text(html, encoding="utf-8")

    # JSON 存檔，方便日後分析
    (REPORT_DIR / f"weekly_{stamp}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    conn.close()
    logger.info(f"✅ 報告已產生：{path}")

    if not args.no_open:
        subprocess.run(["open", str(path)], check=False)

    print(str(path))


if __name__ == "__main__":
    main()

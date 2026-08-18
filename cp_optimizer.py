#!/usr/bin/env python3
"""
安寵 Calm Paws — 優化引擎

用 Thompson Sampling（Beta-Bernoulli 多臂拉霸）在多個維度上探索／利用，
每次產出前決定要用哪組參數，並在數據回來後更新信念。

設計取捨（重要）：
  • 各維度獨立採樣，不建交互作用模型。樣本量不足以支撐完整因子設計，
    這是刻意的偏誤換變異數。
  • 報酬正規化到 [0,1] 後餵給 Beta，屬近似做法，非嚴格 Bernoulli。
  • 任何維度永遠保留 ≥2 個活躍臂，避免過早收斂。
  • 差異化守門優先於報酬最大化 — 政策風險比短期成效重要。

用法：
    python cp_optimizer.py --pick youtube   # 產出前：決定本次參數
    python cp_optimizer.py --update         # 每週：用新數據更新信念
    python cp_optimizer.py --status         # 印出目前信念
"""
import os
import sys
import json
import random
import sqlite3
import logging
import argparse
import datetime as dt
from pathlib import Path

# 本機跑在 ~/calm_paws，GitHub Actions 跑在 workspace 目錄
BASE_DIR = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
DB_PATH = BASE_DIR / "data" / "metrics.db"

logger = logging.getLogger("cp_optimizer")

# ══════════════════════════════════════════════════════════
#  可調整的維度與選項（臂）
# ══════════════════════════════════════════════════════════
def load_scenes_from_config() -> list:
    """
    從 config.yaml 讀出實際存在的場景 ID。

    絕對不能寫死。orchestrator 的 select_scene() 找不到指定 ID 時會
    raise ValueError，整支流程當場中斷 —— 不是學到雜訊，是根本跑不完。
    只要這裡跟 config.yaml 有任何一個字不一樣就會炸。
    """
    conf = BASE_DIR / "config.yaml"
    if not conf.exists():
        return []
    try:
        import re as _re
        txt = conf.read_text(encoding="utf-8")
        m = _re.search(r"^scenes:\s*$", txt, _re.M)
        if not m:
            return []
        rest = txt[m.end():]
        nxt = _re.search(r"^\w[\w_]*:", rest, _re.M)
        block = rest[:nxt.start()] if nxt else rest
        ids = _re.findall(r"^\s*-\s*id:\s*[\"']?([\w_-]+)[\"']?\s*$",
                          block, _re.M)
        return ids
    except Exception:
        return []


# 場景以 config.yaml 為準；讀不到才退回這份預設
_CONFIG_SCENES = load_scenes_from_config()

DIMENSIONS = {
    # ── YouTube 長片 ──
    "scene": _CONFIG_SCENES or [
        "separation_anxiety",
        "sleep",
        "relax",
        "vet_visit",
    ],
    "title_formula": [
        "problem_first",   # 狗狗分離焦慮？8小時舒緩音樂
        "scenario_first",  # 給獨自在家的毛孩｜8小時陪伴音樂
        "outcome_first",   # 3分鐘讓毛孩放鬆入睡｜8小時
        "duration_first",  # 8小時不間斷｜寵物深度放鬆音樂
    ],
    "thumb_style": [
        "big_text_closeup",   # 大字 + 寵物特寫
        "minimal_illust",     # 極簡插畫
        "realistic_sleep",    # 寫實睡眠場景
        "split_beforeafter",  # 左右對比
    ],
    # 原本是 ["3", "8", "10"]。實測結果：8/10 小時的長片全部卡在
    # YouTube「處理中」，最長卡了 17 天沒完成；同一批次的 1 小時
    # 測試片秒過。新頻道對長片似乎有審核/處理門檻，先退回確定能
    # 過的時數區間，讓頻道累積正常紀錄，之後有更多數據再考慮拉長。
    "duration_h": ["1", "2", "3"],
    "upload_slot": ["mon_08", "wed_20", "sat_10"],
    # ── Instagram Reel ──
    "reel_hook": [
        "pet_sleeping_immediate",  # 開頭直接寵物入睡畫面
        "text_question",           # 문字提問開場
        "owner_pov",               # 飼主視角
        "before_after",            # 焦慮→放鬆對比
    ],
    "hashtag_set": ["broad_tw", "niche_behavior", "mixed"],
}

YOUTUBE_DIMS = ["scene", "title_formula", "thumb_style", "duration_h", "upload_slot"]
REEL_DIMS = ["scene", "reel_hook", "hashtag_set"]

# 守門參數
MIN_N_BEFORE_PRUNE = 4      # 至少觀測 4 次才考慮停用某臂
MIN_ACTIVE_ARMS = 2         # 每維度最少保留活躍臂數
EXPLORE_FLOOR = 0.15        # 至少 15% 機率純隨機探索
DIFF_LOOKBACK = 5           # 差異化檢查回看幾支
MIN_DIFF_DIMS = 2           # 與近期影片至少要差幾個維度


def db() -> sqlite3.Connection:
    """
    連線並確保資料表存在。

    不能只做 connect —— 全新環境（首次安裝、雲端 runner 上資料庫
    還沒建立）會直接 no such table: arms。schema 定義在 cp_analytics，
    這裡沿用同一份，避免兩邊定義漂移。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        sys.path.insert(0, str(BASE_DIR))
        from cp_analytics import SCHEMA
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
    except Exception:
        # cp_analytics 不在時，至少把本模組會用到的表建起來
        conn = sqlite3.connect(DB_PATH)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS arms (
                dimension TEXT, value TEXT,
                alpha REAL DEFAULT 1.0, beta REAL DEFAULT 1.0,
                n INTEGER DEFAULT 0, sum_reward REAL DEFAULT 0,
                active INTEGER DEFAULT 1, updated_at TEXT,
                PRIMARY KEY (dimension, value));
            CREATE TABLE IF NOT EXISTS decisions (
                ts TEXT, dimension TEXT, action TEXT,
                detail TEXT, auto_applied INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS policy_flags (
                ts TEXT, severity TEXT, kind TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS videos (
                video_id TEXT PRIMARY KEY, published_at TEXT, title TEXT,
                scene TEXT, title_formula TEXT, thumb_style TEXT,
                duration_h REAL, upload_slot TEXT, music_sig TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
    conn.row_factory = sqlite3.Row
    return conn


def norm(dim: str, val) -> str:
    """
    把 DB 取出的值正規化成臂的字串鍵。
    duration_h 在 videos 表是 REAL（8.0），但臂是 "8"，
    不做這層轉換會導致該維度永遠對不上、n 永遠是 0。
    """
    if val is None:
        return ""
    if dim == "duration_h":
        try:
            f = float(val)
            return str(int(f)) if f == int(f) else str(f)
        except (TypeError, ValueError):
            return str(val)
    return str(val)


def ensure_arms(conn):
    """
    把所有維度的臂寫進 DB，並停用已經不存在的臂。

    停用那步很重要：DB 裡可能殘留舊版本寫死的場景 ID
    （例如 config.yaml 沒有的 thunderstorm）。這些臂若還是 active，
    Thompson Sampling 仍會抽到，抽中就讓整支流程 ValueError。
    """
    now = dt.datetime.now().isoformat()
    for dim, vals in DIMENSIONS.items():
        for v in vals:
            conn.execute(
                """INSERT OR IGNORE INTO arms
                   (dimension, value, alpha, beta, n, sum_reward, active, updated_at)
                   VALUES (?,?,1.0,1.0,0,0,1,?)""",
                (dim, v, now),
            )
        # 停用不在清單內的臂
        placeholders = ",".join("?" * len(vals)) if vals else "''"
        stale = conn.execute(
            f"""SELECT value FROM arms
                WHERE dimension=? AND active=1
                  AND value NOT IN ({placeholders})""",
            (dim, *vals),
        ).fetchall()
        for s in stale:
            conn.execute(
                "UPDATE arms SET active=0, updated_at=? "
                "WHERE dimension=? AND value=?",
                (now, dim, s["value"]),
            )
            conn.execute(
                "INSERT INTO decisions (ts, dimension, action, detail, auto_applied) "
                "VALUES (?,?,?,?,1)",
                (now, dim, "停用失效選項",
                 f"{s['value']} 已不存在於 config.yaml，抽中會導致流程中斷"),
            )
            logger.warning(f"停用失效的 {dim}={s['value']}")
    conn.commit()


# ══════════════════════════════════════════════════════════
#  Thompson Sampling
# ══════════════════════════════════════════════════════════
def sample_arm(conn, dimension: str) -> str:
    """從指定維度採一個臂"""
    rows = conn.execute(
        "SELECT value, alpha, beta, n FROM arms WHERE dimension=? AND active=1",
        (dimension,),
    ).fetchall()
    if not rows:
        return DIMENSIONS[dimension][0]

    # 探索地板：一定比例純隨機，避免鎖死在局部最佳
    if random.random() < EXPLORE_FLOOR:
        return random.choice([r["value"] for r in rows])

    best_val, best_draw = None, -1.0
    for r in rows:
        draw = random.betavariate(max(r["alpha"], 0.01), max(r["beta"], 0.01))
        if draw > best_draw:
            best_draw, best_val = draw, r["value"]
    return best_val


def recent_configs(conn, n: int = DIFF_LOOKBACK) -> list:
    rows = conn.execute(
        """SELECT scene, title_formula, thumb_style, duration_h, upload_slot
           FROM videos WHERE scene IS NOT NULL
           ORDER BY published_at DESC LIMIT ?""",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


def differentiation_ok(candidate: dict, recents: list) -> bool:
    """與近期每一支相比，至少要有 MIN_DIFF_DIMS 個維度不同"""
    for prev in recents:
        diff = sum(
            1 for k in candidate
            if k in prev and norm(k, prev[k]) != norm(k, candidate[k])
        )
        if diff < MIN_DIFF_DIMS:
            return False
    return True


def pick(conn, mode: str = "youtube") -> dict:
    """決定下一次產出的參數組合"""
    ensure_arms(conn)
    dims = YOUTUBE_DIMS if mode == "youtube" else REEL_DIMS
    recents = recent_configs(conn) if mode == "youtube" else []

    # 重採樣直到通過差異化守門（最多 30 次，之後強制隨機）
    for attempt in range(30):
        cand = {d: sample_arm(conn, d) for d in dims}
        if mode != "youtube" or differentiation_ok(cand, recents):
            cand["_diff_forced"] = False
            return cand

    logger.warning("差異化守門連續失敗，改用純隨機組合")
    cand = {d: random.choice(DIMENSIONS[d]) for d in dims}
    cand["_diff_forced"] = True
    return cand


# ══════════════════════════════════════════════════════════
#  報酬計算與信念更新
# ══════════════════════════════════════════════════════════
def video_reward(row: sqlite3.Row, percentiles: dict) -> float:
    """
    YouTube 報酬 = 觀看時數為主（直接對應 YPP 目標），
    輔以 CTR 與留存率。正規化到 [0,1]。
    """
    wh = row["watch_hours"] or 0
    ctr = row["ctr"] or 0
    avd = row["avd_sec"] or 0
    dur_s = (row["duration_h"] or 8) * 3600

    wh_n = min(wh / percentiles["wh_p90"], 1.0) if percentiles["wh_p90"] > 0 else 0
    ctr_n = min(ctr / 10.0, 1.0)                    # CTR 10% 視為滿分
    ret_n = min(avd / dur_s, 1.0) if dur_s else 0   # 留存比例

    return 0.6 * wh_n + 0.25 * ctr_n + 0.15 * ret_n


def reel_reward(row: sqlite3.Row, percentiles: dict) -> float:
    """
    Reel 報酬 = 觸及 + 加權互動。分享與收藏權重高，
    因為它們比按讚更能預測演算法推播。
    """
    reach = row["reach"] or 0
    saves = row["saves"] or 0
    shares = row["shares"] or 0
    likes = row["likes"] or 0

    score = reach + 8 * saves + 12 * shares + 1 * likes
    p90 = percentiles["reel_p90"]
    return min(score / p90, 1.0) if p90 > 0 else 0


def percentile(values: list, p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def update_beliefs(conn, min_age_days: int = 7):
    """
    用已滿 min_age_days 的內容更新各臂信念。
    只用「成熟」數據，避免剛發布的影片污染判斷。
    """
    ensure_arms(conn)
    now = dt.datetime.now().isoformat()

    # ── YouTube ──
    vids = conn.execute(
        """SELECT v.video_id, v.scene, v.title_formula, v.thumb_style,
                  v.duration_h, v.upload_slot,
                  m.watch_hours, m.ctr, m.avd_sec, m.age_days
           FROM videos v
           JOIN video_metrics m ON m.video_id = v.video_id
           WHERE v.scene IS NOT NULL AND m.age_days >= ?
             AND m.snapshot_date = (
                 SELECT MAX(snapshot_date) FROM video_metrics
                 WHERE video_id = v.video_id)""",
        (min_age_days,),
    ).fetchall()

    updated = 0
    if vids:
        pct = {"wh_p90": percentile([v["watch_hours"] or 0 for v in vids], 0.9)}
        for v in vids:
            r = video_reward(v, pct)
            for dim in YOUTUBE_DIMS:
                val = norm(dim, v[dim])
                if not val:
                    continue
                conn.execute(
                    """UPDATE arms
                       SET alpha = alpha + ?, beta = beta + ?,
                           n = n + 1, sum_reward = sum_reward + ?, updated_at = ?
                       WHERE dimension=? AND value=?""",
                    (r, 1 - r, r, now, dim, val),
                )
            updated += 1

    # ── Instagram ──
    reels = conn.execute(
        """SELECT r.media_id, r.scene, r.hook_style, r.hashtag_set,
                  m.reach, m.saves, m.shares, m.likes, m.age_days
           FROM reels r
           JOIN reel_metrics m ON m.media_id = r.media_id
           WHERE r.hook_style IS NOT NULL AND m.age_days >= 3
             AND m.snapshot_date = (
                 SELECT MAX(snapshot_date) FROM reel_metrics
                 WHERE media_id = r.media_id)""",
    ).fetchall()

    if reels:
        scores = [
            (x["reach"] or 0) + 8 * (x["saves"] or 0)
            + 12 * (x["shares"] or 0) + (x["likes"] or 0)
            for x in reels
        ]
        pct = {"reel_p90": percentile(scores, 0.9)}
        for x in reels:
            r = reel_reward(x, pct)
            for dim, raw in (("scene", x["scene"]),
                             ("reel_hook", x["hook_style"]),
                             ("hashtag_set", x["hashtag_set"])):
                val = norm(dim, raw)
                if not val:
                    continue
                conn.execute(
                    """UPDATE arms
                       SET alpha = alpha + ?, beta = beta + ?,
                           n = n + 1, sum_reward = sum_reward + ?, updated_at = ?
                       WHERE dimension=? AND value=?""",
                    (r, 1 - r, r, now, dim, str(val)),
                )
            updated += 1

    conn.commit()
    logger.info(f"更新了 {updated} 筆內容的信念")
    prune_arms(conn)
    return updated


def prune_arms(conn):
    """
    停用明顯落後的臂，但永遠保留至少 MIN_ACTIVE_ARMS 個。
    判準：平均報酬低於該維度最佳臂的 40%，且已觀測 ≥ MIN_N_BEFORE_PRUNE 次。
    """
    now = dt.datetime.now().isoformat()
    for dim in DIMENSIONS:
        rows = conn.execute(
            """SELECT value, n, sum_reward, active FROM arms
               WHERE dimension=? ORDER BY
               CASE WHEN n>0 THEN sum_reward/n ELSE 0 END DESC""",
            (dim,),
        ).fetchall()
        if len(rows) <= MIN_ACTIVE_ARMS:
            continue

        means = {r["value"]: (r["sum_reward"] / r["n"] if r["n"] else 0) for r in rows}
        best = max(means.values()) if means else 0
        if best <= 0:
            continue

        active_count = sum(1 for r in rows if r["active"])
        for r in rows:
            if not r["active"] or r["n"] < MIN_N_BEFORE_PRUNE:
                continue
            if active_count <= MIN_ACTIVE_ARMS:
                break
            if means[r["value"]] < 0.4 * best:
                conn.execute(
                    "UPDATE arms SET active=0, updated_at=? WHERE dimension=? AND value=?",
                    (now, dim, r["value"]),
                )
                conn.execute(
                    "INSERT INTO decisions (ts, dimension, action, detail, auto_applied) "
                    "VALUES (?,?,?,?,1)",
                    (now, dim, "停用臂",
                     f"{r['value']} 平均報酬 {means[r['value']]:.3f} "
                     f"低於最佳 {best:.3f} 的 40%（n={r['n']}）"),
                )
                active_count -= 1
                logger.info(f"停用 {dim}={r['value']}")
    conn.commit()


# ══════════════════════════════════════════════════════════
#  政策風險與健康度
# ══════════════════════════════════════════════════════════
def check_health(conn) -> list:
    """回傳警示清單。任何 critical 都應該讓自動化暫停。"""
    flags = []
    now = dt.datetime.now().isoformat()

    # 1. 差異化：近 5 支若有任兩支完全相同組合 → 高風險
    recents = recent_configs(conn, 8)
    for i in range(len(recents)):
        for j in range(i + 1, len(recents)):
            same = sum(1 for k in recents[i]
                       if str(recents[i][k]) == str(recents[j][k]))
            if same >= len(recents[i]) - 1:
                flags.append({
                    "severity": "critical", "kind": "差異化不足",
                    "detail": "近期有兩支影片參數幾乎完全相同，"
                              "有被判定為重複內容的風險",
                })
                break

    # 2. 留存率崩壞：最近影片留存低於 2% → 內容或 TA 錯位
    row = conn.execute(
        """SELECT AVG(m.avd_sec) a, AVG(v.duration_h) d
           FROM video_metrics m JOIN videos v ON v.video_id=m.video_id
           WHERE m.age_days BETWEEN 7 AND 30"""
    ).fetchone()
    if row and row["a"] and row["d"]:
        ret = row["a"] / (row["d"] * 3600)
        if ret < 0.02:
            flags.append({
                "severity": "warn", "kind": "留存率偏低",
                "detail": f"平均留存 {ret*100:.1f}%，長片觀眾流失快，"
                          f"建議檢查開頭 30 秒與音樂實際品質",
            })

    # 3. CTR 崩壞
    row = conn.execute(
        "SELECT AVG(ctr) c FROM video_metrics WHERE age_days BETWEEN 7 AND 30"
    ).fetchone()
    if row and row["c"] is not None and 0 < row["c"] < 2.0:
        flags.append({
            "severity": "warn", "kind": "點閱率偏低",
            "detail": f"平均 CTR {row['c']:.1f}%，縮圖與標題吸引力不足",
        })

    # 4. 訂閱數停滯
    rows = conn.execute(
        "SELECT subs_gained FROM channel_daily ORDER BY date DESC LIMIT 14"
    ).fetchall()
    if len(rows) >= 14 and sum(r["subs_gained"] or 0 for r in rows) == 0:
        flags.append({
            "severity": "warn", "kind": "訂閱停滯",
            "detail": "連續 14 天 0 新訂閱，需檢視頻道定位與導流",
        })

    for f in flags:
        conn.execute(
            "INSERT INTO policy_flags (ts, severity, kind, detail) VALUES (?,?,?,?)",
            (now, f["severity"], f["kind"], f["detail"]),
        )
    conn.commit()
    return flags


def arm_status(conn) -> dict:
    ensure_arms(conn)
    out = {}
    for dim in DIMENSIONS:
        rows = conn.execute(
            "SELECT value, alpha, beta, n, sum_reward, active FROM arms WHERE dimension=?",
            (dim,),
        ).fetchall()
        arms = []
        for r in rows:
            a, b = max(r["alpha"], 0.01), max(r["beta"], 0.01)
            mean = a / (a + b)
            var = (a * b) / ((a + b) ** 2 * (a + b + 1))
            sd = var ** 0.5
            arms.append({
                "value": r["value"],
                "n": r["n"],
                "mean": round(mean, 3),
                "ci_low": round(max(0, mean - 1.96 * sd), 3),
                "ci_high": round(min(1, mean + 1.96 * sd), 3),
                "active": bool(r["active"]),
            })
        arms.sort(key=lambda x: -x["mean"])
        out[dim] = arms
    return out


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick", choices=["youtube", "reel"])
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    conn = db()

    if args.pick:
        cfg = pick(conn, args.pick)
        print(json.dumps(cfg, ensure_ascii=False))
    elif args.update:
        n = update_beliefs(conn)
        flags = check_health(conn)
        print(json.dumps({"updated": n, "flags": flags}, ensure_ascii=False, indent=2))
    elif args.status:
        st = arm_status(conn)
        if args.json:
            print(json.dumps(st, ensure_ascii=False, indent=2))
        else:
            for dim, arms in st.items():
                print(f"\n【{dim}】")
                for a in arms:
                    mark = "✓" if a["active"] else "✗"
                    print(f"  {mark} {a['value']:<24} 平均 {a['mean']:.3f} "
                          f"[{a['ci_low']:.2f}–{a['ci_high']:.2f}] n={a['n']}")
    else:
        ap.print_help()

    conn.close()


if __name__ == "__main__":
    main()

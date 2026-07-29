#!/usr/bin/env python3
"""
安寵 Calm Paws — 雲端產出包裝層

把優化引擎選出的參數傳給既有的 orchestrator.py，
產出完成後把「用了哪組參數」寫回 metrics.db，優化引擎才學得到東西。

參數傳遞用三種方式並行，因為 orchestrator.py 可能只支援其中一種：
  1. 環境變數 CP_SCENE / CP_TITLE_FORMULA / ...
  2. 覆寫檔 scene_override.json
  3. 命令列參數（若 orchestrator 支援）

若三種都沒被採用，會在輸出明確警告 —— 這很重要，
因為參數沒生效的話，優化引擎學到的全是雜訊。
"""
import os
import re
import sys
import json
import glob
import sqlite3
import logging
import argparse
import subprocess
import datetime as dt
from pathlib import Path

BASE = Path(os.environ.get("CP_HOME", ".")).resolve()
DB_PATH = BASE / "data" / "metrics.db"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cp_generate")


# ══════════════════════════════════════════════════════════
def orchestrator_supports(flag: str) -> bool:
    """檢查 orchestrator.py 是否接受某個命令列參數"""
    orch = BASE / "orchestrator.py"
    if not orch.exists():
        return False
    try:
        src = orch.read_text(encoding="utf-8", errors="ignore")
        return f'"{flag}"' in src or f"'{flag}'" in src
    except Exception:
        return False


def write_override(params: dict):
    """寫出覆寫檔，供 orchestrator 讀取（若它支援）"""
    path = BASE / "scene_override.json"
    path.write_text(json.dumps(params, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    logger.info(f"已寫出 {path.name}：{params}")


def export_env(params: dict) -> dict:
    env = os.environ.copy()
    for k, v in params.items():
        if v:
            env[f"CP_{k.upper()}"] = str(v)
    return env


def newest_video_id() -> str:
    """從 YouTube 抓最新一支影片 ID（產出後確認用）"""
    try:
        sys.path.insert(0, str(BASE))
        from cp_analytics import youtube_clients
        data_api, _ = youtube_clients()
        if not data_api:
            return ""
        ch = data_api.channels().list(part="contentDetails", mine=True).execute()
        pl_id = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl = data_api.playlistItems().list(
            part="contentDetails", playlistId=pl_id, maxResults=1).execute()
        items = pl.get("items", [])
        return items[0]["contentDetails"]["videoId"] if items else ""
    except Exception as e:
        logger.warning(f"取得最新影片 ID 失敗：{e}")
        return ""


def record_video(video_id: str, params: dict, title: str = ""):
    if not video_id:
        logger.warning("沒有 video_id，跳過記錄（優化引擎將學不到這次產出）")
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        sys.path.insert(0, str(BASE))
        from cp_analytics import db
        conn = db()
        conn.execute(
            """INSERT INTO videos
               (video_id, published_at, title, scene, title_formula,
                thumb_style, duration_h, upload_slot)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(video_id) DO UPDATE SET
                 scene=excluded.scene,
                 title_formula=excluded.title_formula,
                 thumb_style=excluded.thumb_style,
                 duration_h=excluded.duration_h,
                 upload_slot=excluded.upload_slot""",
            (video_id,
             dt.datetime.now(dt.timezone.utc).isoformat(),
             title,
             params.get("scene"),
             params.get("title_formula"),
             params.get("thumb_style"),
             float(params.get("duration_h") or 8),
             params.get("upload_slot")))
        conn.commit()
        conn.close()
        logger.info(f"✅ 已記錄 {video_id} 的參數組合")
    except Exception as e:
        logger.error(f"記錄失敗：{e}")


def record_cost(service: str, usd: float):
    try:
        sys.path.insert(0, str(BASE))
        from cp_analytics import db
        conn = db()
        today = dt.date.today().isoformat()
        conn.execute(
            """INSERT INTO costs (date, service, usd) VALUES (?,?,?)
               ON CONFLICT(date, service) DO UPDATE SET usd = usd + excluded.usd""",
            (today, service, usd))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["youtube", "reel"], required=True)
    ap.add_argument("--scene", default="")
    ap.add_argument("--title-formula", default="")
    ap.add_argument("--thumb-style", default="")
    ap.add_argument("--duration-h", default="8")
    ap.add_argument("--upload-slot", default="")
    ap.add_argument("--reel-hook", default="")
    ap.add_argument("--hashtag-set", default="")
    args = ap.parse_args()

    params = {
        "scene": args.scene,
        "title_formula": args.title_formula,
        "thumb_style": args.thumb_style,
        "duration_h": args.duration_h,
        "upload_slot": args.upload_slot,
        "reel_hook": args.reel_hook,
        "hashtag_set": args.hashtag_set,
    }
    params = {k: v for k, v in params.items() if v}
    logger.info(f"本次參數：{params}")

    write_override(params)
    env = export_env(params)

    # 組出 orchestrator 指令
    cmd = [sys.executable, str(BASE / "orchestrator.py"), "--mode", args.mode]
    passed_via_cli = False
    for flag, key in (("--scene", "scene"),
                      ("--title-formula", "title_formula"),
                      ("--thumb-style", "thumb_style"),
                      ("--duration-h", "duration_h")):
        if params.get(key) and orchestrator_supports(flag):
            cmd += [flag, str(params[key])]
            passed_via_cli = True

    if not passed_via_cli:
        logger.warning(
            "orchestrator.py 不接受參數旗標，改靠環境變數與 scene_override.json。"
            "若它也不讀這兩者，優化引擎學到的會是雜訊 —— "
            "請確認 orchestrator.py 有讀取 CP_SCENE 或 scene_override.json。")

    logger.info(f"執行：{' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env, cwd=str(BASE),
                          capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    print(out[-6000:])

    if proc.returncode != 0:
        logger.error(f"orchestrator 失敗（return code {proc.returncode}）")
        sys.exit(proc.returncode)

    if args.mode == "youtube":
        vid = ""
        m = re.search(r"(?:Video ID|video_id|youtu\.be/)[:\s=/]*([A-Za-z0-9_-]{11})", out)
        if m:
            vid = m.group(1)
            logger.info(f"從輸出取得 video_id：{vid}")
        else:
            vid = newest_video_id()
            logger.info(f"從 API 取得最新 video_id：{vid}")

        title = ""
        tm = re.search(r"標題[:：]\s*(.+)", out)
        if tm:
            title = tm.group(1).strip()[:200]

        record_video(vid, params, title)
        print(f"VIDEO_ID={vid}")

    # 粗估成本（實際金額請以各服務帳單為準）
    record_cost("Replicate", 1.2 if args.mode == "youtube" else 0.3)
    record_cost("Anthropic", 0.15)


if __name__ == "__main__":
    main()

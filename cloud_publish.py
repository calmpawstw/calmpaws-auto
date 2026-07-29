#!/usr/bin/env python3
"""
安寵 Calm Paws — 雲端發佈模組

在 GitHub Actions 上取代本機的 Cloudflare Tunnel：
影片先以 GitHub Release 附件上傳（公開 URL），再交給 Instagram 抓取。

用法：
    python cloud_publish.py --reel output/_ig_upload.mp4 --url <public_url>
    python cloud_publish.py --build-config      # 由環境變數組出 config.yaml
"""
import os
import sys
import json
import time
import glob
import base64
import sqlite3
import logging
import argparse
import subprocess
import datetime as dt
import urllib.request
import urllib.parse
from pathlib import Path

BASE = Path(os.environ.get("CP_HOME", "."))
IG_API = "https://graph.instagram.com/v21.0"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud_publish")


# ══════════════════════════════════════════════════════════
#  由環境變數組出 config.yaml（金鑰不進 repo）
# ══════════════════════════════════════════════════════════
def build_config():
    import yaml

    tmpl_path = BASE / "config.template.yaml"
    if tmpl_path.exists():
        cfg = yaml.safe_load(tmpl_path.read_text(encoding="utf-8")) or {}
    else:
        cfg = {}

    cfg.setdefault("api_keys", {})
    env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "replicate": "REPLICATE_API_TOKEN",
        "instagram_access_token": "IG_ACCESS_TOKEN",
        "instagram_user_id": "IG_USER_ID",
    }
    for key, env in env_map.items():
        val = os.environ.get(env, "")
        if val:
            cfg["api_keys"][key] = val

    (BASE / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    logger.info("config.yaml 已由環境變數產生")

    # YouTube 憑證
    tok = os.environ.get("YT_TOKEN_JSON", "")
    if tok:
        (BASE / "token.json").write_text(tok, encoding="utf-8")
        logger.info("token.json 已寫入")
    sec = os.environ.get("YT_CLIENT_SECRET_JSON", "")
    if sec:
        (BASE / "client_secret.json").write_text(sec, encoding="utf-8")
        logger.info("client_secret.json 已寫入")


# ══════════════════════════════════════════════════════════
#  Instagram
# ══════════════════════════════════════════════════════════
def ig_call(path, token, method="GET", **params):
    params["access_token"] = token
    if method == "POST":
        req = urllib.request.Request(
            f"{IG_API}/{path}", urllib.parse.urlencode(params).encode())
    else:
        req = urllib.request.Request(
            f"{IG_API}/{path}?{urllib.parse.urlencode(params)}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": {"message": str(e)}}


def verify_public_url(url: str, tries: int = 6) -> bool:
    """
    確認 Instagram 真的抓得到這個網址。
    GitHub Release 附件剛建立時可能有數秒延遲，所以重試。
    """
    for i in range(tries):
        r = subprocess.run(
            ["curl", "-sIL", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", "30", url],
            capture_output=True, text=True)
        code = r.stdout.strip()
        logger.info(f"URL 檢查 [{i+1}/{tries}]：HTTP {code}")
        if code == "200":
            return True
        time.sleep(5)
    return False


def build_caption(scene: str = None, youtube_id: str = None) -> str:
    scene_txt = {
        "separation_anxiety": "緩解分離焦慮",
        "sleep_night": "幫助夜間安睡",
        "thunderstorm": "安撫雷聲與煙火驚嚇",
        "vet_visit": "舒緩就醫緊張",
        "kitten_calm": "安定幼貓情緒",
        "senior_pet": "陪伴高齡毛孩",
        "car_travel": "減輕車程不安",
    }.get(scene or "", "幫助毛孩放鬆")

    yt = youtube_id or os.environ.get("CP_LATEST_YT", "")
    yt_line = f"\n▶ 完整長版在 YouTube：https://youtu.be/{yt}\n" if yt else "\n"

    return (
        f"🐾 寵物舒緩音樂 | 安寵 Calm Paws\n\n"
        f"專為毛孩設計的放鬆音樂 🌙\n"
        f"{scene_txt} 💤\n"
        f"{yt_line}\n"
        "#安寵 #CalmPaws #寵物音樂 #舒緩音樂 #貓咪 #狗狗\n"
        "#台灣寵物 #寵物放鬆 #毛孩日常"
    )


def publish_reel(video_url: str, caption: str) -> str:
    token = os.environ.get("IG_ACCESS_TOKEN", "")
    ig_id = os.environ.get("IG_USER_ID", "")
    if not token or not ig_id:
        raise RuntimeError("缺少 IG_ACCESS_TOKEN 或 IG_USER_ID")

    logger.info("建立 Reel container...")
    c = ig_call(f"{ig_id}/media", token, "POST",
                media_type="REELS", video_url=video_url,
                caption=caption, share_to_feed="true")
    if "error" in c:
        raise RuntimeError(f"container 建立失敗：{c['error']}")
    cid = c["id"]
    logger.info(f"container：{cid}")

    logger.info("等待 Instagram 處理...")
    for i in range(60):
        time.sleep(5)
        st = ig_call(cid, token, fields="status_code")
        code = st.get("status_code", "?")
        if i % 4 == 0:
            logger.info(f"  [{i*5}s] {code}")
        if code == "FINISHED":
            break
        if code == "ERROR":
            raise RuntimeError(f"Instagram 處理失敗：{st}")
    else:
        raise RuntimeError("等待逾時（5 分鐘）")

    logger.info("發佈...")
    p = ig_call(f"{ig_id}/media_publish", token, "POST", creation_id=cid)
    if "error" in p:
        raise RuntimeError(f"發佈失敗：{p['error']}")

    mid = p["id"]
    d = ig_call(mid, token, fields="permalink")
    logger.info(f"✅ 發佈成功：{mid}")
    if d.get("permalink"):
        logger.info(f"   {d['permalink']}")
    return mid


def record_reel(media_id: str, scene: str = None, hook: str = None,
                hashtag_set: str = None):
    """把這次發佈的參數寫進 metrics.db，優化引擎才有東西學"""
    db_path = BASE / "data" / "metrics.db"
    if not db_path.exists():
        logger.warning("找不到 metrics.db，跳過記錄")
        return
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT OR REPLACE INTO reels
               (media_id, published_at, scene, hook_style, hashtag_set)
               VALUES (?,?,?,?,?)""",
            (media_id, dt.datetime.now(dt.timezone.utc).isoformat(),
             scene, hook, hashtag_set))
        conn.commit()
        conn.close()
        logger.info("已記錄至 metrics.db")
    except Exception as e:
        logger.warning(f"記錄失敗：{e}")


# ══════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-config", action="store_true")
    ap.add_argument("--reel")
    ap.add_argument("--url")
    ap.add_argument("--scene")
    ap.add_argument("--hook")
    ap.add_argument("--hashtag-set")
    ap.add_argument("--youtube-id")
    args = ap.parse_args()

    if args.build_config:
        build_config()
        return

    if not args.url:
        logger.error("需要 --url（GitHub Release 附件網址）")
        sys.exit(1)

    if not verify_public_url(args.url):
        logger.error("公開 URL 無法存取，Instagram 也抓不到，中止")
        sys.exit(1)

    caption = build_caption(args.scene, args.youtube_id)
    try:
        mid = publish_reel(args.url, caption)
    except Exception as e:
        logger.error(str(e))
        sys.exit(1)

    record_reel(mid, args.scene, args.hook, args.hashtag_set)
    print(mid)


if __name__ == "__main__":
    main()

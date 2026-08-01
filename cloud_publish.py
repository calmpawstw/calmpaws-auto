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


def _yt_token_path() -> Path:
    """
    取得 upload_youtube.py 裡 TOKEN_FILE 的實際位置。

    用 AST 讀原始碼而不是 import，因為 import upload_youtube 會連帶
    載入 google 那一票套件；build-config 連 reel.yml 也會跑，
    不該為了讀一個常數就把整條路徑綁在 google 套件裝好上面。

    讀不到就退回已知預設值，並記錄警告 —— 這兩個位置必須一致，
    不一致正是這次 YouTube 上傳全掛的原因。
    """
    default = Path.home() / ".calm_paws_yt_token.json"
    src_file = BASE / "upload_youtube.py"
    if not src_file.exists():
        return default
    try:
        import ast
        tree = ast.parse(src_file.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "TOKEN_FILE"
                       for t in node.targets):
                continue
            # 預期形如 Path.home() / ".calm_paws_yt_token.json"
            if (isinstance(node.value, ast.BinOp)
                    and isinstance(node.value.op, ast.Div)
                    and isinstance(node.value.right, ast.Constant)):
                return Path.home() / node.value.right.value
    except Exception as e:
        logger.warning(f"讀取 upload_youtube.TOKEN_FILE 失敗（{type(e).__name__}），"
                       f"使用預設路徑")
    return default


# ══════════════════════════════════════════════════════════
#  由環境變數組出 config.yaml（金鑰不進 repo）
# ══════════════════════════════════════════════════════════
# config.yaml 的欄位名 → GitHub Secret 名稱
SECRET_ALIASES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "replicate": "REPLICATE_API_TOKEN",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "openai": "OPENAI_API_KEY",
    "pexels": "PEXELS_API_KEY",
    "instagram_access_token": "IG_ACCESS_TOKEN",
    "instagram_user_id": "IG_USER_ID",
}


def build_config():
    import yaml

    tmpl_path = BASE / "config.template.yaml"
    if tmpl_path.exists():
        cfg = yaml.safe_load(tmpl_path.read_text(encoding="utf-8")) or {}
    else:
        cfg = {}

    cfg.setdefault("api_keys", {})

    # ⚠️ 原本這裡是用 toJSON(secrets) 把所有 Secret 一次倒進 ALL_SECRETS。
    # 那個寫法很方便，但 GitHub 的濫用偵測會把它判定為
    # 「可能是惡意的 workflow」並拒絕執行 ——
    #     "GitHub detected that this workflow file may be malicious."
    # 因為「把全部 Secret 序列化成一個環境變數」正是竊取金鑰的典型手法。
    # 這就是所有 workflow 卡在 action_required 的真正原因。
    #
    # 改成由 workflow 明確列出需要的 Secret 逐一傳入。
    # 保留 ALL_SECRETS 的讀取只是為了相容舊執行，正常情況不會用到。
    all_secrets = {}
    raw = os.environ.get("ALL_SECRETS", "")
    if raw:
        try:
            all_secrets = json.loads(raw)
            logger.warning("偵測到 ALL_SECRETS —— 這個寫法會被 GitHub 判定為可疑，"
                           "請改用明確列出 Secret 的 workflow")
        except Exception as e:
            logger.warning(f"ALL_SECRETS 解析失敗：{e}")

    def lookup(field: str) -> str:
        alias = SECRET_ALIASES.get(field, field.upper())
        for src in (all_secrets, os.environ):
            for name in (alias, field.upper(), field):
                v = src.get(name)
                if v:
                    return v
        return ""

    # 補齊 template 裡列出的欄位
    filled, missing = [], []
    for field in list(cfg["api_keys"].keys()):
        val = lookup(field)
        if val:
            cfg["api_keys"][field] = val
            filled.append(field)
        elif not cfg["api_keys"].get(field):
            missing.append(field)

    # template 沒列到、但 Secret 有提供的也一併填入
    for field, alias in SECRET_ALIASES.items():
        if field not in cfg["api_keys"]:
            val = lookup(field)
            if val:
                cfg["api_keys"][field] = val
                filled.append(field)

    logger.info(f"已填入金鑰：{', '.join(filled) or '（無）'}")
    if missing:
        logger.warning(f"⚠️  以下金鑰缺少對應 Secret：{', '.join(missing)}")

    # 路徑改成相對於 workspace。
    # 本機寫的是 ~/calm_paws/logs，在 runner 上會展開成
    # /home/runner/calm_paws/logs —— 那個目錄不存在。
    paths = cfg.get("paths") or {}
    for k, v in list(paths.items()):
        if not isinstance(v, str):
            continue
        nv = v.replace("~/calm_paws/", "").replace("~/calm_paws", ".")
        paths[k] = nv

        # paths 底下不一定都是目錄 —— existing_music 指向的是 .mp3 檔案。
        # 對檔案路徑呼叫 mkdir 會 FileExistsError，
        # 所以有副檔名就只建它的上層目錄。
        p = Path(nv)
        try:
            if p.suffix:
                p.parent.mkdir(parents=True, exist_ok=True)
            else:
                p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"建立目錄 {nv} 失敗：{e}")
    if paths:
        cfg["paths"] = paths
        logger.info(f"路徑已轉為相對（{len(paths)} 項）")

    for k, v in list(cfg.items()):
        if isinstance(v, str) and "~/calm_paws/" in v:
            cfg[k] = v.replace("~/calm_paws/", "")

    # 缺少關鍵區塊就直接中止，並說清楚原因。
    # 讓它繼續跑只會在深處炸出 KeyError，難以判讀。
    required = ["paths", "scenes", "youtube"]
    absent = [k for k in required if not cfg.get(k)]
    if absent:
        logger.error(
            f"❌ config 缺少必要區塊：{', '.join(absent)}\n"
            f"   多半是 config.template.yaml 沒有正確產生。\n"
            f"   請在 Mac 上執行 完成雲端.command，它會重新產生並推送。")
        raise SystemExit(1)
    logger.info(f"設定完整：{len(cfg.get('scenes', []))} 個場景")

    # ── YouTube 憑證 ──────────────────────────────────────
    #
    # ⚠️ 這裡曾經有一個讓 YouTube 上傳完全失效的路徑不匹配：
    #   本檔把 token 寫到 BASE/token.json，
    #   但 upload_youtube.py 讀的是 Path.home()/".calm_paws_yt_token.json"。
    # 結果 runner 上永遠找不到 token，程式就退回互動式 OAuth
    # （InstalledAppFlow），而 client_secrets 又是空字串，
    # 於是拋 FileNotFoundError: ''。
    #
    # 更糟的情況是：如果 client_secrets 剛好存在，run_local_server()
    # 會在無人的 runner 上等一個永遠不會發生的瀏覽器授權，
    # 一路卡到 workflow 逾時（180 分鐘）才死。
    #
    # 所以這裡做三件事：
    #   1. token 同時寫到 upload_youtube.py 真正會讀的位置
    #   2. client_secret 寫成絕對路徑，並回填進 config
    #   3. 缺 token 時直接失敗並說清楚，不要讓它去卡互動式流程
    YT_TOKEN_PATH = _yt_token_path()

    tok = os.environ.get("YT_TOKEN_JSON", "")
    if tok:
        (BASE / "token.json").write_text(tok, encoding="utf-8")
        YT_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        YT_TOKEN_PATH.write_text(tok, encoding="utf-8")
        try:
            t = json.loads(tok)
            if not t.get("refresh_token"):
                logger.warning("YT_TOKEN_JSON 沒有 refresh_token，"
                               "token 過期後將無法自動續期")
        except json.JSONDecodeError:
            raise SystemExit("❌ YT_TOKEN_JSON 不是有效的 JSON，請重新設定這個 Secret")
        logger.info(f"YouTube token 已寫入：{YT_TOKEN_PATH}")
    else:
        logger.warning("⚠️ 沒有 YT_TOKEN_JSON —— YouTube 上傳一定會失敗")

    sec = os.environ.get("YT_CLIENT_SECRET_JSON", "")
    sec_path = BASE / "client_secret.json"
    if sec:
        sec_path.write_text(sec, encoding="utf-8")
        logger.info("client_secret.json 已寫入")

    # 回填絕對路徑，避免 config 裡是空字串或相對路徑找不到
    cfg.setdefault("api_keys", {})["youtube_client_secrets"] = str(sec_path.resolve())

    (BASE / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    logger.info("config.yaml 已由環境變數產生")


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

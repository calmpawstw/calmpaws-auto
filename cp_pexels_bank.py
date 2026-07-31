#!/usr/bin/env python3
"""
安寵 Calm Paws — 用 Pexels 免費素材建立圖庫（零成本）

為什麼有這支：
  Replicate 是按次收費的 AI 生圖 API，帳戶餘額用完就會 402、
  自動退回醜醜的純色佔位圖。Pexels 提供免費的真實照片與影片素材，
  授權允許商業使用、不強制標註，而且免費額度是每小時 200 次請求 /
  每月 20,000 次，對我們每月幾十張的需求綽綽有餘。

  取得金鑰：https://www.pexels.com/api/ 註冊後即時發給你，完全免費。

跟 AI 生圖的差別（誠實說明）：
  Pexels 是別人拍的真實照片，畫質通常比 AI 生圖更自然、更像「真的寵物」，
  但沒辦法完全客製化（例如「在台北公寓裡的柴犬」這種指定情境）。
  對「寵物放鬆音樂」這種背景畫面用途來說，真實素材通常反而更討喜。

用法：
  python cp_pexels_bank.py --check                    # 只測試金鑰能不能用
  python cp_pexels_bank.py                            # 建立全部場景的圖庫
  python cp_pexels_bank.py --scenes sleep relax       # 只建指定場景
  python cp_pexels_bank.py --count 20                 # 每場景抓幾張背景
  python cp_pexels_bank.py --video                    # 連影片素材一起抓（Reel 用，更生動）
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml
from PIL import Image

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

API_BASE = "https://api.pexels.com/v1"
IMAGE_BANK_DIRNAME = "image_bank"
VIDEO_BANK_DIRNAME = "video_bank"

# 場景 → Pexels 搜尋關鍵字（英文搜尋結果遠多於中文）。
# 每個場景給多組關鍵字，輪流搜可以拿到更多樣的畫面，
# 避免整個場景的背景圖長得都一樣。
SCENE_QUERIES = {
    "separation_anxiety": ["dog waiting home", "dog resting sofa", "lonely dog window"],
    "sleep":              ["sleeping dog", "sleeping cat", "pet sleeping bed night"],
    "relax":              ["relaxed cat", "dog relaxing", "calm pet sunlight"],
    "vet_visit":          ["calm dog portrait", "cat being held", "gentle pet care"],
    "thunderstorm":       ["dog hiding blanket", "scared dog indoor", "cozy pet rainy day"],
    "kitten_calm":        ["kitten sleeping", "cute kitten resting", "baby cat cozy"],
    "senior_pet":         ["old dog resting", "senior dog portrait", "elderly cat sleeping"],
    "car_travel":         ["dog in car", "pet car travel", "dog car window"],
}
DEFAULT_QUERIES = ["sleeping pet", "calm dog", "calm cat"]


def api_key(config: dict) -> str:
    """金鑰優先序：環境變數 → config.yaml。雲端用環境變數，本機用設定檔。"""
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    if key:
        return key
    return str(config.get("api_keys", {}).get("pexels", "")).strip()


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get(url: str, key: str, params: dict) -> dict:
    r = requests.get(url, headers={"Authorization": key}, params=params, timeout=30)
    if r.status_code == 401:
        raise SystemExit("❌ Pexels 金鑰無效。請到 https://www.pexels.com/api/ 重新取得")
    if r.status_code == 429:
        raise SystemExit("❌ 已達 Pexels 速率上限（每小時 200 次），請過一小時再試")
    r.raise_for_status()
    return r.json()


def check_key(key: str) -> bool:
    try:
        d = _get(f"{API_BASE}/search", key, {"query": "dog", "per_page": 1})
        n = d.get("total_results", 0)
        print(f"✅ 金鑰有效（測試搜尋 'dog' 得到 {n:,} 筆結果）")
        return True
    except SystemExit as e:
        print(e)
        return False
    except Exception as e:
        print(f"❌ 測試失敗：{type(e).__name__}: {e}")
        return False


def crop_to(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """置中裁切到指定比例，再縮放到目標尺寸（不變形、不留黑邊）"""
    img = img.convert("RGB")
    src_ratio = img.width / img.height
    dst_ratio = target_w / target_h

    if src_ratio > dst_ratio:      # 來源太寬，裁掉左右
        new_w = int(img.height * dst_ratio)
        left = (img.width - new_w) // 2
        img = img.crop((left, 0, left + new_w, img.height))
    else:                          # 來源太高，裁掉上下
        new_h = int(img.width / dst_ratio)
        top = (img.height - new_h) // 2
        img = img.crop((0, top, img.width, top + new_h))

    return img.resize((target_w, target_h), Image.LANCZOS)


def fetch_photos(key: str, queries: list[str], orientation: str, want: int) -> list[dict]:
    """輪流用多組關鍵字搜尋，收集到足夠張數為止，並去除重複的照片 ID"""
    seen, out = set(), []
    per_query = max(5, want // max(1, len(queries)) + 3)

    for q in queries:
        if len(out) >= want:
            break
        try:
            data = _get(f"{API_BASE}/search", key, {
                "query": q, "orientation": orientation,
                "per_page": min(80, per_query * 2), "size": "large",
            })
        except SystemExit:
            raise
        except Exception as e:
            print(f"   ⚠️  搜尋「{q}」失敗（{type(e).__name__}），跳過")
            continue

        for p in data.get("photos", []):
            if p["id"] in seen:
                continue
            seen.add(p["id"])
            out.append(p)
            if len(out) >= want:
                break
        time.sleep(0.3)   # 對免費 API 客氣一點

    return out


def fetch_videos(key: str, queries: list[str], want: int) -> list[dict]:
    seen, out = set(), []
    for q in queries:
        if len(out) >= want:
            break
        try:
            data = _get(f"{API_BASE}/videos/search", key, {
                "query": q, "orientation": "portrait", "per_page": 30,
            })
        except SystemExit:
            raise
        except Exception as e:
            print(f"   ⚠️  影片搜尋「{q}」失敗（{type(e).__name__}），跳過")
            continue

        for v in data.get("videos", []):
            if v["id"] in seen:
                continue
            # 只要 6 秒以上的，太短的循環起來很突兀
            if v.get("duration", 0) < 6:
                continue
            seen.add(v["id"])
            out.append(v)
            if len(out) >= want:
                break
        time.sleep(0.3)
    return out


def best_video_file(v: dict) -> str | None:
    """挑最接近 1080x1920 的直式檔案，優先 mp4"""
    files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"]
    if not files:
        return None
    portrait = [f for f in files if (f.get("height") or 0) > (f.get("width") or 0)]
    pool = portrait or files
    # 挑高度最接近 1920 但不要超過太多（省流量）
    pool.sort(key=lambda f: abs((f.get("height") or 0) - 1920))
    return pool[0].get("link")


def build_scene(key: str, scene: dict, assets_dir: Path,
                count: int, thumb_count: int, want_video: bool) -> dict:
    sid = scene["id"]
    queries = SCENE_QUERIES.get(sid, DEFAULT_QUERIES)

    bank = assets_dir / IMAGE_BANK_DIRNAME / sid
    reel_dir, thumb_dir = bank / "reel", bank / "thumb"
    reel_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    have_reel = len(list(reel_dir.glob("*.jpg")))
    have_thumb = len(list(thumb_dir.glob("*.jpg")))
    need_reel = max(0, count - have_reel)
    need_thumb = max(0, thumb_count - have_thumb)

    print(f"\n▶ {sid}（{scene.get('name', '')}）")
    print(f"   現有 {have_reel} 背景 / {have_thumb} 縮圖，需補 {need_reel} / {need_thumb}")

    credits = []

    if need_reel:
        photos = fetch_photos(key, queries, "portrait", need_reel)
        if not photos:
            print("   ⚠️  找不到直式素材")
        for i, p in enumerate(photos):
            try:
                raw = requests.get(p["src"]["large2x"], timeout=60).content
                img = crop_to(Image.open(io.BytesIO(raw)), 1080, 1920)
                img.save(reel_dir / f"bg_{have_reel + i:03d}.jpg", "JPEG", quality=88)
                credits.append({"type": "photo", "id": p["id"],
                                "photographer": p.get("photographer"),
                                "url": p.get("url")})
            except Exception as e:
                print(f"   ⚠️  下載失敗（{type(e).__name__}），跳過這張")
        print(f"   ✅ 背景圖 +{len(photos)}")

    if need_thumb:
        photos = fetch_photos(key, queries, "landscape", need_thumb)
        for i, p in enumerate(photos):
            try:
                raw = requests.get(p["src"]["large2x"], timeout=60).content
                img = crop_to(Image.open(io.BytesIO(raw)), 1280, 720)
                img.save(thumb_dir / f"thumb_{have_thumb + i:03d}.jpg", "JPEG", quality=90)
                credits.append({"type": "photo", "id": p["id"],
                                "photographer": p.get("photographer"),
                                "url": p.get("url")})
            except Exception as e:
                print(f"   ⚠️  下載失敗（{type(e).__name__}），跳過這張")
        print(f"   ✅ 縮圖 +{len(photos)}")

    if want_video:
        vdir = assets_dir / VIDEO_BANK_DIRNAME / sid
        vdir.mkdir(parents=True, exist_ok=True)
        have_v = len(list(vdir.glob("*.mp4")))
        need_v = max(0, 5 - have_v)
        if need_v:
            vids = fetch_videos(key, queries, need_v)
            for i, v in enumerate(vids):
                link = best_video_file(v)
                if not link:
                    continue
                try:
                    raw = requests.get(link, timeout=180).content
                    (vdir / f"clip_{have_v + i:03d}.mp4").write_bytes(raw)
                    credits.append({"type": "video", "id": v["id"],
                                    "photographer": v.get("user", {}).get("name"),
                                    "url": v.get("url")})
                except Exception as e:
                    print(f"   ⚠️  影片下載失敗（{type(e).__name__}），跳過")
            print(f"   ✅ 影片 +{len(vids)}")

    return {"scene": sid, "credits": credits}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", nargs="*", help="只建指定場景，留空＝全部")
    ap.add_argument("--count", type=int, default=15, help="每場景背景圖張數")
    ap.add_argument("--thumb-count", type=int, default=3, help="每場景縮圖張數")
    ap.add_argument("--video", action="store_true", help="連影片素材一起下載（檔案較大）")
    ap.add_argument("--check", action="store_true", help="只測試金鑰")
    args = ap.parse_args()

    config = load_config()
    key = api_key(config)
    if not key:
        print("❌ 找不到 Pexels 金鑰。")
        print("   免費申請：https://www.pexels.com/api/ （註冊後即時發給你）")
        print("   然後設環境變數 PEXELS_API_KEY，或寫進 config.yaml 的 api_keys.pexels")
        sys.exit(1)

    if args.check:
        sys.exit(0 if check_key(key) else 1)

    if not check_key(key):
        sys.exit(1)

    scenes = config["scenes"]
    if args.scenes:
        scenes = [s for s in scenes if s["id"] in args.scenes]
        if not scenes:
            print(f"❌ 找不到場景：{args.scenes}")
            sys.exit(1)

    assets_dir = Path(config["paths"]["assets_dir"]).expanduser()
    all_credits = []
    for scene in scenes:
        all_credits.append(build_scene(key, scene, assets_dir,
                                       args.count, args.thumb_count, args.video))

    # 保存出處記錄。授權不強制標註，但這是對攝影師的基本尊重，
    # 而且萬一日後有人問素材來源，你拿得出來。
    cred_file = assets_dir / IMAGE_BANK_DIRNAME / "CREDITS.json"
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps(all_credits, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    print(f"\n✅ 完成，出處記錄：{cred_file}")
    print("   素材來自 Pexels，授權允許商業使用且不強制標註。")
    print("   記得推送到雲端 repo 才會生效。")


if __name__ == "__main__":
    main()

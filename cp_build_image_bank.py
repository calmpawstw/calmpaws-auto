#!/usr/bin/env python3
"""
安寵 Calm Paws — 一次性圖庫建立工具

背景：Reels 每次發文都即時呼叫 Replicate 生一張新背景圖，
是按次收費的 API，帳戶餘額用完就會 402 Payment Required、
自動退回不好看的純色佔位圖。

解法：每個場景一次生一批圖（存成 JPEG 省空間），存進
assets/image_bank/<scene_id>/{reel,thumb}/ 並 commit 進 git repo，
之後 generate_images.py 會優先從圖庫隨機挑圖，不再即時呼叫
Replicate —— 圖庫建好之後，日常運作是零成本的。

用法：
  python cp_build_image_bank.py                  # 顯示預估花費，需輸入 yes 才會真的花錢
  python cp_build_image_bank.py --scenes sleep    # 只建指定場景
  python cp_build_image_bank.py --reel-count 20 --thumb-count 3
  python cp_build_image_bank.py --dry-run         # 只顯示會做什麼，不呼叫 API

價格是 Replicate 官網公開費率的粗估，實際請以帳單為準：
  flux-schnell（背景圖）≈ US$0.003 / 張
  flux-dev（縮圖，品質較高）≈ US$0.03 / 張
"""

import argparse
import io
import sys
from pathlib import Path

import yaml
from PIL import Image

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from generate_images import ImageGenerator, IMAGE_BANK_DIRNAME  # noqa: E402

PRICE_REEL = 0.003   # flux-schnell
PRICE_THUMB = 0.03   # flux-dev


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def existing_count(bank_dir: Path, scene_id: str, kind: str) -> int:
    d = bank_dir / scene_id / kind
    return len(list(d.glob("*.jpg"))) if d.exists() else 0


def build_scene(generator: ImageGenerator, scene: dict, config: dict,
                 bank_dir: Path, reel_count: int, thumb_count: int, dry_run: bool):
    scene_id = scene["id"]
    reel_dir = bank_dir / scene_id / "reel"
    thumb_dir = bank_dir / scene_id / "thumb"
    reel_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    have_reel = existing_count(bank_dir, scene_id, "reel")
    have_thumb = existing_count(bank_dir, scene_id, "thumb")
    need_reel = max(0, reel_count - have_reel)
    need_thumb = max(0, thumb_count - have_thumb)

    print(f"\n▶ {scene_id}：已有 {have_reel} 背景 / {have_thumb} 縮圖，"
          f"還要生 {need_reel} 背景 / {need_thumb} 縮圖")

    if dry_run:
        return need_reel, need_thumb

    for i in range(need_reel):
        idx = have_reel + i
        prompt = (scene["image_prompt"] +
                  ", vertical composition 9:16, Instagram Reels style"
                  ", no text, no watermark")
        img_bytes = generator.generate(prompt, width=1080, height=1920, num_outputs=1)[0]
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.save(reel_dir / f"bg_{idx:03d}.jpg", "JPEG", quality=88)
        print(f"   背景 {idx+1}/{reel_count} 完成")

    for i in range(need_thumb):
        idx = have_thumb + i
        prompt = (scene["image_prompt"] +
                  ", youtube thumbnail style, vibrant but calm colors, "
                  "professional photography, highly detailed, golden hour lighting, "
                  "Taiwan aesthetic, no text, no watermark")
        img_bytes = generator.generate(prompt, width=1280, height=720,
                                        num_outputs=1, high_quality=True)[0]
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.save(thumb_dir / f"thumb_{idx:03d}.jpg", "JPEG", quality=90)
        print(f"   縮圖 {idx+1}/{thumb_count} 完成")

    return need_reel, need_thumb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenes", nargs="*", help="只建指定場景 ID，留空＝全部場景")
    ap.add_argument("--reel-count", type=int, default=15, help="每場景背景圖張數")
    ap.add_argument("--thumb-count", type=int, default=3, help="每場景縮圖候選張數")
    ap.add_argument("--dry-run", action="store_true", help="只算預估花費，不呼叫 API")
    ap.add_argument("--yes", action="store_true", help="跳過確認提示（給腳本自動化用）")
    args = ap.parse_args()

    config = load_config()
    api_token = config["api_keys"].get("replicate", "")
    if not api_token or api_token.startswith("r8_YOUR"):
        print("❌ config.yaml 未設定 Replicate API token")
        sys.exit(1)

    scenes = config["scenes"]
    if args.scenes:
        scenes = [s for s in scenes if s["id"] in args.scenes]
        if not scenes:
            print(f"❌ 找不到指定的場景：{args.scenes}")
            sys.exit(1)

    bank_dir = Path(config["paths"]["assets_dir"]).expanduser() / IMAGE_BANK_DIRNAME

    # 先算一輪，顯示總預估花費
    print("══════════════════════════════════════════════════════")
    print("  預估花費（依 Replicate 公開費率粗估，實際以帳單為準）")
    print("══════════════════════════════════════════════════════")
    total_reel = total_thumb = 0
    for scene in scenes:
        need_reel, need_thumb = build_scene(
            None, scene, config, bank_dir,
            args.reel_count, args.thumb_count, dry_run=True)
        total_reel += need_reel
        total_thumb += need_thumb

    cost = total_reel * PRICE_REEL + total_thumb * PRICE_THUMB
    print(f"\n共需生成：{total_reel} 張背景圖 + {total_thumb} 張縮圖")
    print(f"預估花費：US${cost:.2f}（一次性，之後重複使用不再花錢）")
    print("══════════════════════════════════════════════════════\n")

    if total_reel == 0 and total_thumb == 0:
        print("✅ 圖庫已經足夠，不需要再生成")
        return

    if args.dry_run:
        print("（--dry-run，不會真的呼叫 API）")
        return

    if not args.yes:
        ans = input(f"確定要花約 US${cost:.2f} 生成這些圖嗎？輸入 yes 繼續：")
        if ans.strip().lower() != "yes":
            print("已取消")
            return

    generator = ImageGenerator(api_token)
    for scene in scenes:
        build_scene(generator, scene, config, bank_dir,
                    args.reel_count, args.thumb_count, dry_run=False)

    print("\n✅ 圖庫建立完成，記得推送到雲端 repo 才會生效")


if __name__ == "__main__":
    main()

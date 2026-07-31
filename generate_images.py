#!/usr/bin/env python3
"""
安寵 Calm Paws — 圖像生成模組
使用 Replicate Flux Schnell 生成療癒寵物圖像（YouTube縮圖 + Reels背景）
"""
from __future__ import annotations  # 讓 `str | None` 這類寫法在 Python 3.9 也能用

import os
import time
import logging
import requests
import replicate
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import io

logger = logging.getLogger(__name__)

# Flux Schnell（快速高品質，性價比最高）
FLUX_SCHNELL_MODEL = "black-forest-labs/flux-schnell"
# Flux Dev（品質更高，用於YouTube縮圖）
FLUX_DEV_MODEL = "black-forest-labs/flux-dev"

# 中文字型候選路徑：依序嘗試，第一個存在的檔案就用。
# macOS 本機用 PingFang；雲端 runner（Ubuntu）用 apt 裝的 Noto Sans CJK
# （workflow 的「安裝相依套件」步驟會先 apt-get install fonts-noto-cjk）。
# 之前這裡只寫死 PingFang 的路徑，雲端上該檔案不存在，PIL 會悄悄退回
# ImageFont.load_default()——那是不支援中文字元的點陣字型，中文字會變成
# 一格一格的方塊（tofu），而且尺寸固定很小，這就是貼文文字亂碼又過小的原因。
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",                                  # macOS
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",                 # Ubuntu (fonts-noto-cjk)
    "/usr/share/fonts/opentype/noto/NotoSansCJKtc-Bold.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",                # fallback：至少能顯示中文
]


def _resolve_font_path(config: dict) -> str | None:
    """回傳第一個實際存在的中文字型路徑；都找不到回傳 None。"""
    configured = config.get("youtube", {}).get("thumbnail_font")
    candidates = ([configured] if configured else []) + _FONT_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            return path
    logger.error(
        "找不到任何中文字型（試過：%s）。文字疊加會用 PIL 內建字型，"
        "中文字元會顯示為方塊。雲端環境請確認 workflow 已安裝 fonts-noto-cjk。",
        ", ".join(c for c in candidates if c),
    )
    return None


class ImageGenerator:
    def __init__(self, api_token: str):
        os.environ["REPLICATE_API_TOKEN"] = api_token

    def generate(
        self,
        prompt: str,
        width: int = 1280,
        height: int = 720,
        num_outputs: int = 1,
        high_quality: bool = False,
    ) -> list[Path]:
        """
        生成圖像，返回暫存的本地路徑列表
        """
        model = FLUX_DEV_MODEL if high_quality else FLUX_SCHNELL_MODEL

        logger.info(f"Replicate Flux 生成圖像 ({width}x{height})...")

        output = replicate.run(
            model,
            input={
                "prompt": prompt,
                "width": width,
                "height": height,
                "num_outputs": num_outputs,
                "output_format": "png",
                "output_quality": 95,
                "num_inference_steps": 4 if not high_quality else 28,
                "guidance_scale": 3.5,
            }
        )

        paths = []
        for i, file_output in enumerate(output):
            # 讀取圖像資料
            img_bytes = file_output.read()
            paths.append(img_bytes)

        return paths


def generate_youtube_thumbnail(
    scene: dict,
    config: dict,
    output_path: Path,
    generator: ImageGenerator,
) -> Path:
    """
    生成 YouTube 縮圖（1280x720）
    在圖像上疊加品牌文字
    """
    output_path = Path(output_path)
    if output_path.exists():
        logger.info(f"縮圖已存在：{output_path}")
        return output_path

    logger.info(f"生成 YouTube 縮圖：{scene['id']}")

    # 加強版 prompt（縮圖需要更吸睛）
    thumbnail_prompt = (
        scene["image_prompt"] +
        ", youtube thumbnail style, vibrant but calm colors, "
        "professional photography, highly detailed, golden hour lighting, "
        "Taiwan aesthetic, no text, no watermark"
    )

    img_bytes_list = generator.generate(
        prompt=thumbnail_prompt,
        width=1280,
        height=720,
        num_outputs=1,
        high_quality=True,
    )

    img = Image.open(io.BytesIO(img_bytes_list[0]))

    # 疊加品牌文字
    img = _add_thumbnail_overlay(img, scene, config)

    img.save(output_path, "PNG", quality=95)
    logger.info(f"縮圖已儲存：{output_path}")
    return output_path


def generate_reels_backgrounds(
    scene: dict,
    config: dict,
    output_dir: Path,
    generator: ImageGenerator,
    count: int = 5,
) -> list[Path]:
    """
    生成 Reels 背景圖像（1080x1920 垂直格式），返回路徑列表
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for i in range(count):
        output_path = output_dir / f"{scene['id']}_reel_bg_{i:02d}.png"
        if output_path.exists():
            paths.append(output_path)
            continue

        # 每張用略微不同的 prompt 增加多樣性
        variation_suffix = [
            ", close-up shot, bokeh background",
            ", wide angle, cozy atmosphere",
            ", golden hour sunlight",
            ", soft morning light",
            ", peaceful evening ambiance",
        ][i % 5]

        reel_prompt = (
            scene["image_prompt"] +
            ", vertical composition 9:16, Instagram Reels style" +
            variation_suffix +
            ", no text, no watermark"
        )

        img_bytes_list = generator.generate(
            prompt=reel_prompt,
            width=1080,
            height=1920,
            num_outputs=1,
        )

        img = Image.open(io.BytesIO(img_bytes_list[0]))
        img.save(output_path, "PNG")
        logger.info(f"Reels 背景 {i+1}/{count}：{output_path}")
        paths.append(output_path)
        time.sleep(1)  # 避免 rate limit

    return paths


def _add_thumbnail_overlay(img: Image.Image, scene: dict, config: dict) -> Image.Image:
    """在縮圖上加入品牌標示（右下角半透明）"""
    draw = ImageDraw.Draw(img)
    w, h = img.size

    brand = config["brand"]["name"]
    tagline = config["brand"]["tagline"]

    # 右下角半透明黑底
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle(
        [(w - 320, h - 90), (w, h)],
        fill=(0, 0, 0, 140)
    )
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 嘗試載入中文字型
    font_path = _resolve_font_path(config)
    try:
        if not font_path:
            raise OSError("no CJK font available")
        font_brand = ImageFont.truetype(font_path, 26)
        font_tagline = ImageFont.truetype(font_path, 16)
    except Exception:
        font_brand = ImageFont.load_default()
        font_tagline = font_brand

    draw.text((w - 315, h - 82), brand, fill="white", font=font_brand)
    draw.text((w - 315, h - 52), tagline, fill=(220, 220, 220), font=font_tagline)

    return img


def _create_placeholder_image(path: Path, width: int, height: int, scene: dict, config: dict):
    """建立佔位圖（Replicate 不可用時使用）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    # 用漸層色建立佔位圖
    img = Image.new("RGB", (width, height), color=(180, 200, 210))
    draw = ImageDraw.Draw(img)
    brand = config["brand"]["name"]
    scene_name = scene["name"]
    # 簡單的品牌文字
    font_path = _resolve_font_path(config)
    font_size = max(28, min(64, height // 24))
    try:
        if not font_path:
            raise OSError("no CJK font available")
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()
        font_size = 11  # PIL 內建字型的實際高度，用來算行距
        logger.warning("佔位圖無可用中文字型，文字可能無法正確顯示：%s", path)
    # 用 anchor="mm" 置中，行距依字型大小縮放，避免兩行文字疊在一起
    line_gap = int(font_size * 0.75)
    draw.text((width // 2, height // 2 - line_gap), brand, fill=(50, 50, 80), font=font, anchor="mm")
    draw.text((width // 2, height // 2 + line_gap), scene_name, fill=(80, 80, 100), font=font, anchor="mm")
    img.save(path, "PNG")
    logger.info(f"佔位圖已建立：{path}")



# 圖庫：用 cp_build_image_bank.py 一次性花錢生成一批圖，之後每次發文
# 從裡面隨機挑，不必每次都呼叫 Replicate（避免像這次一樣半路錢用完，
# 也符合「現階段不想再花錢」的原則 —— 圖庫建好之後日常運作是零成本的）。
IMAGE_BANK_DIRNAME = "image_bank"


def _bank_pick(assets_dir: Path, scene: dict, config: dict) -> dict | None:
    """從圖庫隨機挑一組圖；圖庫該場景是空的就回傳 None（呼叫端會退回原本邏輯）"""
    import random
    import shutil

    bank_scene_dir = assets_dir / IMAGE_BANK_DIRNAME / scene["id"]
    reel_pool = sorted((bank_scene_dir / "reel").glob("*.jpg")) if (bank_scene_dir / "reel").exists() else []
    thumb_pool = sorted((bank_scene_dir / "thumb").glob("*.jpg")) if (bank_scene_dir / "thumb").exists() else []
    if not reel_pool or not thumb_pool:
        return None

    thumb_dst = assets_dir / "images" / f"{scene['id']}_thumbnail.png"
    thumb_dst.parent.mkdir(parents=True, exist_ok=True)
    if not thumb_dst.exists():
        img = Image.open(random.choice(thumb_pool)).convert("RGB")
        img = _add_thumbnail_overlay(img, scene, config)
        img.save(thumb_dst, "PNG", quality=95)

    reel_dir = assets_dir / "images" / scene["id"]
    reel_dir.mkdir(parents=True, exist_ok=True)
    picks = (random.sample(reel_pool, k=5) if len(reel_pool) >= 5
             else random.choices(reel_pool, k=5))  # 圖庫張數不夠 5 張就允許重複
    reel_bgs = []
    for i, src in enumerate(picks):
        dst = reel_dir / f"{scene['id']}_reel_bg_{i:02d}.png"
        shutil.copyfile(src, dst)
        reel_bgs.append(dst)

    logger.info(f"使用圖庫：{scene['id']}（{len(reel_pool)} 背景 / {len(thumb_pool)} 縮圖候選）")
    return {"thumbnail": thumb_dst, "reel_backgrounds": reel_bgs}


def generate_all_images(scene: dict, config: dict, assets_dir: Path, dry_run: bool = False) -> dict:
    """
    生成一個場景所需的所有圖像資源
    返回 {thumbnail: Path, reel_backgrounds: [Path, ...]}
    dry_run=True 時使用佔位圖，不呼叫 Replicate

    優先順序：圖庫（免費，若已建立）→ Replicate 即時生成（要錢）→ 佔位圖（免費但不好看）
    """
    assets_dir = Path(assets_dir)
    api_token = config["api_keys"].get("replicate", "")

    if not dry_run:
        bank_result = _bank_pick(assets_dir, scene, config)
        if bank_result:
            return bank_result

    # dry_run 或無 token 時使用佔位圖
    if dry_run or not api_token or api_token.startswith("r8_YOUR"):
        if not dry_run:
            raise ValueError("請在 config.yaml 設定 Replicate API token")
        logger.info("DRY RUN：使用佔位圖像，不呼叫 Replicate")
        thumbnail_path = assets_dir / "images" / f"{scene['id']}_thumbnail.png"
        if not thumbnail_path.exists():
            _create_placeholder_image(thumbnail_path, 1280, 720, scene, config)
        reel_dir = assets_dir / "images" / scene["id"]
        reel_dir.mkdir(parents=True, exist_ok=True)
        reel_bgs = []
        for i in range(5):
            p = reel_dir / f"{scene['id']}_reel_bg_{i:02d}.png"
            if not p.exists():
                _create_placeholder_image(p, 1080, 1920, scene, config)
            reel_bgs.append(p)
        return {"thumbnail": thumbnail_path, "reel_backgrounds": reel_bgs}

    generator = ImageGenerator(api_token)

    try:
        thumbnail_path = generate_youtube_thumbnail(
            scene=scene,
            config=config,
            output_path=assets_dir / "images" / f"{scene['id']}_thumbnail.png",
            generator=generator,
        )
    except Exception as e:
        logger.warning(f"Replicate 縮圖生成失敗（{type(e).__name__}: {e}），改用佔位縮圖")
        thumbnail_path = assets_dir / "images" / f"{scene['id']}_thumbnail.png"
        if not thumbnail_path.exists():
            _create_placeholder_image(thumbnail_path, 1280, 720, scene, config)

    try:
        reel_bgs = generate_reels_backgrounds(
            scene=scene,
            config=config,
            output_dir=assets_dir / "images" / scene["id"],
            generator=generator,
            count=5,
        )
    except Exception as e:
        logger.warning(f"Replicate 背景圖生成失敗（{type(e).__name__}: {e}），改用佔位背景圖")
        reel_dir = assets_dir / "images" / scene["id"]
        reel_bgs = []
        for i in range(5):
            p = reel_dir / f"{scene['id']}_reel_bg_{i:02d}.png"
            if not p.exists():
                _create_placeholder_image(p, 1080, 1920, scene, config)
            reel_bgs.append(p)

    return {
        "thumbnail": thumbnail_path,
        "reel_backgrounds": reel_bgs,
    }


if __name__ == "__main__":
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    scene = cfg["scenes"][0]
    assets_dir = Path(os.path.expanduser(cfg["paths"]["assets_dir"]))

    result = generate_all_images(scene, cfg, assets_dir)
    print(f"✅ 縮圖：{result['thumbnail']}")
    print(f"✅ Reels 背景：{len(result['reel_backgrounds'])} 張")

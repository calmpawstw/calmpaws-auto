#!/usr/bin/env python3
"""
安寵 Calm Paws — 影片組裝模組
使用 FFmpeg 組裝 YouTube 長片（8h）與 Instagram Reels（60s）
"""

import os
import subprocess
import logging
import random
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _run_ffmpeg(cmd: list, label: str = "ffmpeg"):
    """執行 FFmpeg 指令，含錯誤處理"""
    logger.info(f"[{label}] 執行：{' '.join(cmd[:6])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg 錯誤：{result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg 失敗（{label}）")
    logger.info(f"[{label}] 完成")
    return result


# ── YouTube 長片組裝 ───────────────────────────────────────────────────────────

def assemble_youtube_video(
    music_path: Path,
    images: list[Path],
    thumbnail_path: Path,
    output_path: Path,
    config: dict,
    duration_seconds: int = 28800,
) -> Path:
    """
    組裝 YouTube 長片（1920x1080）
    使用靜態縮圖 + 音樂，快速編碼模式（避免逐幀 zoompan 耗時數小時）
    """
    output_path = Path(output_path)
    if output_path.exists():
        logger.info(f"YouTube 影片已存在：{output_path}")
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    video_cfg = config.get("video", {})
    resolution = video_cfg.get("youtube_resolution", "1920x1080")
    w, h = map(int, resolution.split("x"))

    # 使用縮圖（或第一張圖片）作為靜態背景
    bg_image = thumbnail_path if thumbnail_path.exists() else (images[0] if images else None)
    if bg_image is None:
        raise ValueError("沒有圖像素材，無法組裝影片")

    logger.info(f"YouTube 快速靜態模式：{bg_image.name} + {music_path.name} → {duration_seconds//3600}h")

    # 靜態圖片 + 音樂，-tune stillimage 大幅加速編碼
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", str(bg_image),
        "-i", str(music_path),
        "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-shortest",
        "-t", str(duration_seconds),
        "-movflags", "+faststart",
        str(output_path),
    ]

    _run_ffmpeg(cmd, "youtube_long")
    logger.info(f"YouTube 長片完成：{output_path} ({duration_seconds//3600}小時)")
    return output_path


# ── Instagram Reels 組裝 ──────────────────────────────────────────────────────

def assemble_reel(
    music_path: Path,
    backgrounds: list[Path],
    voice_files: dict,      # {intro: Path, middle: Path, outro: Path} 或 {}
    output_path: Path,
    config: dict,
    scene: dict,
    duration_seconds: int = 60,
) -> Path:
    """
    組裝 60 秒 Instagram Reels（1080x1920）
    結構：開場(5s) → 主體圖像輪播(45s) → 結尾品牌(10s)
    若有語音旁白則疊入
    """
    output_path = Path(output_path)
    if output_path.exists():
        logger.info(f"Reel 已存在：{output_path}")
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)

    video_cfg = config.get("video", {})
    resolution = video_cfg.get("reels_resolution", "1080x1920")
    w, h = map(int, resolution.split("x"))
    bitrate = video_cfg.get("reels_bitrate", "8000k")

    # 選取背景圖（若有多張則輪播）
    bgs = backgrounds if backgrounds else []
    if not bgs:
        raise ValueError("Reels 需要至少一張背景圖")

    num_bgs = len(bgs)
    bg_duration = duration_seconds / num_bgs

    # 建立帶字幕的 Reel
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # 1. 組合背景影片（圖片輪播）
        bg_video = tmp / "bg.mp4"
        inputs = []
        filter_parts = []

        for i, bg in enumerate(bgs):
            inputs += ["-loop", "1", "-t", str(bg_duration), "-i", str(bg)]
            filter_parts.append(
                f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},setsar=1,fps=30[v{i}]"
            )

        concat = "".join([f"[v{i}]" for i in range(num_bgs)])
        filter_parts.append(f"{concat}concat=n={num_bgs}:v=1:a=0[vbg]")

        _run_ffmpeg(
            ["ffmpeg", "-y"] + inputs + [
                "-filter_complex", ";".join(filter_parts),
                "-map", "[vbg]",
                "-c:v", "libx264", "-preset", "fast",
                "-t", str(duration_seconds),
                str(bg_video),
            ],
            "reel_bg"
        )

        # 2. 準備音訊（60s 截段）
        audio_out = tmp / "audio.mp3"
        _run_ffmpeg(
            ["ffmpeg", "-y",
             "-i", str(music_path),
             "-vn",
             "-t", str(duration_seconds),
             "-ar", "44100", "-ac", "2",
             "-c:a", "libmp3lame", "-b:a", "128k",
             str(audio_out)],
            "reel_audio"
        )

        # 3. 如果有語音旁白，mix 進去
        if voice_files.get("intro") and voice_files.get("outro"):
            mixed_audio = tmp / "mixed.m4a"
            voice_inputs = [
                "-i", str(voice_files["intro"]),
                "-i", str(voice_files.get("middle", voice_files["intro"])),
                "-i", str(voice_files["outro"]),
            ]
            # intro 在 t=0, middle 在 t=20, outro 在 t=50
            _run_ffmpeg(
                ["ffmpeg", "-y",
                 "-i", str(audio_out)]
                + voice_inputs
                + ["-filter_complex",
                   "[0:a]volume=0.4[bg];"
                   "[1:a]adelay=0|0[v0];"
                   "[2:a]adelay=20000|20000[v1];"
                   "[3:a]adelay=50000|50000[v2];"
                   "[bg][v0][v1][v2]amix=inputs=4:duration=first[aout]",
                   "-map", "[aout]",
                   "-c:a", "aac", "-b:a", "128k",
                   str(mixed_audio)],
                "reel_mix"
            )
            final_audio = mixed_audio
        else:
            final_audio = audio_out

        # 4. 合併影像 + 音訊，加字幕 overlay
        caption = scene.get("reel_caption_template", "").replace(
            "{brand_tagline}", config["brand"]["tagline"]
        )

        # drawtext 需要 FFmpeg 有 libfreetype 支援，跳過改用純影片
        # 文字說明放在 Instagram 貼文 caption，不內嵌在影片
        _run_ffmpeg(
            ["ffmpeg", "-y",
             "-i", str(bg_video),
             "-i", str(final_audio),
             "-vf", "fps=30",
             "-map", "0:v", "-map", "1:a",
             "-c:v", "libx264", "-preset", "fast",
             "-b:v", bitrate,
             "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "128k",
             "-t", str(duration_seconds),
             "-movflags", "+faststart",
             str(output_path)],
            "reel_final"
        )

    logger.info(f"Reel 組裝完成：{output_path}")
    return output_path


def assemble_all(
    scene: dict,
    config: dict,
    assets: dict,   # {music, thumbnail, reel_backgrounds, voice}
    output_dir: Path,
) -> dict:
    """
    組裝該場景的所有影片
    返回 {youtube: Path, reel: Path}
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scene_id = scene["id"]
    duration_h = config["schedule"].get("youtube_video_duration_hours", 8)
    duration_s = duration_h * 3600

    # YouTube 長片（用縮圖作為靜態背景，多張時輪播）
    yt_images = [assets["thumbnail"]]  # 至少有縮圖
    if assets.get("reel_backgrounds"):
        # 也把 Reels 背景轉為 16:9 版本（直接拉伸，YouTube 觀眾習慣靜態背景）
        yt_images += assets["reel_backgrounds"][:3]

    yt_output = output_dir / f"{scene_id}_youtube_{duration_h}h.mp4"
    youtube_path = assemble_youtube_video(
        music_path=assets["music"],
        images=yt_images,
        thumbnail_path=assets["thumbnail"],
        output_path=yt_output,
        config=config,
        duration_seconds=duration_s,
    )

    # Reels（60秒）
    reel_output = output_dir / f"{scene_id}_reel_60s.mp4"
    reel_path = assemble_reel(
        music_path=assets["music"],
        backgrounds=assets.get("reel_backgrounds", [assets["thumbnail"]]),
        voice_files=assets.get("voice", {}),
        output_path=reel_output,
        config=config,
        scene=scene,
        duration_seconds=60,
    )

    return {
        "youtube": youtube_path,
        "reel": reel_path,
    }


if __name__ == "__main__":
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    # 測試用（假設 assets 已存在）
    scene = cfg["scenes"][0]
    assets_dir = Path(os.path.expanduser(cfg["paths"]["assets_dir"]))
    output_dir = Path(os.path.expanduser(cfg["paths"]["output_dir"]))

    print("⚠️  請先執行 orchestrator.py 完整流程")

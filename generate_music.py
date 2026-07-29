#!/usr/bin/env python3
"""
安寵 Calm Paws — 音樂生成模組
使用 Mureka.ai 官方 API 生成純樂器療癒音樂
API 文件：https://platform.mureka.ai/docs/en/quickstart.html
"""

import os
import time
import logging
import requests
from pathlib import Path

logger = logging.getLogger(__name__)

MUREKA_API_BASE = "https://api.mureka.ai"


# ── Mureka AI ─────────────────────────────────────────────────────────────────

class MurekaGenerator:
    """
    使用 Mureka 官方 API 生成純樂器音樂（instrumental）
    每次生成約 45 秒，需多段生成後用 FFmpeg 拼接至目標長度
    """

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def generate_instrumental(self, prompt: str) -> dict:
        """
        生成純樂器音樂，返回 task_id
        POST /v1/instrumental/generate
        """
        logger.info(f"Mureka 生成 instrumental：{prompt[:60]}...")
        resp = self.session.post(
            f"{MUREKA_API_BASE}/v1/instrumental/generate",
            json={"prompt": prompt},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        task_id = data["id"]
        logger.info(f"Mureka task_id：{task_id}，狀態：{data.get('status')}")
        return data

    def query_instrumental(self, task_id: str) -> dict:
        """
        查詢生成狀態
        GET /v1/instrumental/query/{task_id}
        """
        resp = self.session.get(
            f"{MUREKA_API_BASE}/v1/instrumental/query/{task_id}",
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def wait_for_completion(self, task_id: str, timeout: int = 300) -> list[dict]:
        """
        輪詢直到生成完成，返回音樂資訊列表
        每首 Mureka 生成結果包含 audio_url
        """
        start = time.time()
        while time.time() - start < timeout:
            data = self.query_instrumental(task_id)
            status = data.get("status", "")
            logger.info(f"Mureka 狀態：{status}（{int(time.time()-start)}s）")

            if status == "succeeded":
                # 返回所有生成的曲目
                choices = data.get("choices", [])
                logger.info(f"生成完成，{len(choices)} 首曲目")
                return choices

            elif status in ("failed", "cancelled"):
                raise RuntimeError(f"Mureka 生成失敗：{data}")

            time.sleep(10)

        raise TimeoutError(f"Mureka 生成超時（>{timeout}s）")

    def download(self, audio_url: str, output_path: Path) -> Path:
        """下載音樂到本機"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"下載：{audio_url[:60]}...")
        resp = requests.get(audio_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"已下載：{output_path}")
        return output_path


# ── 主要介面 ──────────────────────────────────────────────────────────────────

def generate_music(
    scene: dict,
    config: dict,
    output_dir: Path,
    duration_seconds: int = 28800,
    use_existing: bool = False,
) -> Path:
    """
    為指定場景生成音樂。

    策略（優先順序）：
    1. use_existing=True → 直接循環現有 mix_31m10s.mp3
    2. Mureka API → 生成短段後拼接
    3. 兩者都失敗 → 拋出錯誤
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scene_id = scene["id"]
    output_path = output_dir / f"{scene_id}_music.mp3"

    if output_path.exists():
        logger.info(f"音樂已存在，跳過：{output_path}")
        return output_path

    # ① 優先使用現有音樂循環
    existing_music = Path(os.path.expanduser(
        config["paths"].get("existing_music", "")
    ))
    if use_existing and existing_music.exists():
        logger.info(f"使用現有音樂循環至 {duration_seconds}s")
        return _loop_music(existing_music, output_path, duration_seconds)

    # ① 備案：若現有音樂不存在，自動用 FFmpeg 產生療癒環境音
    if use_existing:
        logger.warning(f"找不到現有音樂 {existing_music}，改用 FFmpeg 自動產生療癒音樂")
        return _generate_ambient_music(output_path, duration_seconds)

    # ② Mureka API
    api_key = config["api_keys"].get("mureka", "")
    if not api_key or api_key == "YOUR_MUREKA_API_KEY":
        raise ValueError("請在 config.yaml 設定 mureka API key，或將 use_existing 設為 True")

    generator = MurekaGenerator(api_key)

    # Mureka 每首約 1-2 分鐘，生成多段後拼接到目標長度
    # 計算需要幾段（每段以 90 秒估算）
    segment_duration_estimate = 90
    num_segments_needed = max(1, duration_seconds // segment_duration_estimate + 1)
    # 上限 10 段（避免費用過高），不夠再循環
    num_segments = min(num_segments_needed, 10)

    logger.info(f"需要 {num_segments} 段 Mureka 音樂，拼接至 {duration_seconds}s")

    segments = []
    for i in range(num_segments):
        logger.info(f"生成第 {i+1}/{num_segments} 段...")
        task = generator.generate_instrumental(scene["music_prompt"])
        choices = generator.wait_for_completion(task["id"])

        if not choices:
            logger.warning(f"第 {i+1} 段無結果，跳過")
            continue

        # 取第一首（品質通常最好）
        audio_url = choices[0].get("url") or choices[0].get("audio_url", "")
        if not audio_url:
            logger.warning(f"第 {i+1} 段無 URL：{choices[0]}")
            continue

        seg_path = output_dir / f"{scene_id}_seg_{i:02d}.mp3"
        generator.download(audio_url, seg_path)
        segments.append(seg_path)
        time.sleep(3)  # 避免 rate limit

    if not segments:
        raise RuntimeError("Mureka 未能生成任何音樂段落")

    # 拼接所有段落
    if len(segments) == 1:
        combined = segments[0]
    else:
        combined = output_dir / f"{scene_id}_combined.mp3"
        _concat_segments(segments, combined)
        for s in segments:
            s.unlink(missing_ok=True)

    # 循環至目標長度
    result = _loop_music(combined, output_path, duration_seconds)
    if combined != output_path:
        combined.unlink(missing_ok=True)

    return result


def _concat_segments(segments: list[Path], output: Path):
    """FFmpeg 串接多段音樂"""
    import subprocess
    list_file = output.parent / "_concat_list.txt"
    with open(list_file, "w") as f:
        for s in segments:
            f.write(f"file '{s.resolve()}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", str(list_file), "-c", "copy", str(output)],
        check=True, capture_output=True,
    )
    list_file.unlink(missing_ok=True)
    logger.info(f"拼接完成：{output}")


def _generate_ambient_music(output: Path, target_seconds: int) -> Path:
    """
    用 FFmpeg 合成療癒環境音（粉紅噪音低通濾波）
    無需外部音樂檔案，適合測試或初期使用
    """
    import subprocess
    output.parent.mkdir(parents=True, exist_ok=True)
    logger.info(f"FFmpeg 合成療癒音樂：{target_seconds}s → {output}")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", f"anoisesrc=color=pink:amplitude=0.05:sample_rate=44100:duration={target_seconds}",
            "-af", "lowpass=f=1200,highpass=f=60,volume=0.5",
            "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
            str(output),
        ],
        check=True,
        capture_output=True,
    )
    logger.info(f"療癒音樂合成完成：{output}")
    return output


def _loop_music(source: Path, output: Path, target_seconds: int) -> Path:
    """FFmpeg 無限循環截取至目標長度"""
    import subprocess
    logger.info(f"循環 {source.name} → {target_seconds}s（{target_seconds//3600}h）")
    subprocess.run(
        ["ffmpeg", "-y",
         "-stream_loop", "-1",
         "-i", str(source),
         "-t", str(target_seconds),
         "-c:a", "libmp3lame", "-b:a", "192k", "-ar", "44100",
         str(output)],
        check=True, capture_output=True,
    )
    logger.info(f"音樂完成：{output}")
    return output


if __name__ == "__main__":
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    scene = cfg["scenes"][0]
    out_dir = Path(os.path.expanduser(cfg["paths"]["music_dir"]))

    result = generate_music(
        scene=scene,
        config=cfg,
        output_dir=out_dir,
        duration_seconds=28800,
        use_existing=True,  # 先用現有 mp3，等 Mureka key 設好再改 False
    )
    print(f"✅ 音樂：{result}")

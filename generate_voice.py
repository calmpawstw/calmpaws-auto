#!/usr/bin/env python3
"""
安寵 Calm Paws — 語音生成模組
使用 ElevenLabs API 生成中文 ASMR 旁白（Reels 開場 / 結尾）
"""

import os
import logging
from pathlib import Path
from elevenlabs import ElevenLabs, VoiceSettings

logger = logging.getLogger(__name__)

# Reels 各類型旁白腳本模板
VOICE_SCRIPTS = {
    "separation_anxiety": {
        "intro": "你今天又出門了吧。沒關係，我陪著牠。",
        "middle": "讓這首音樂，替你守著牠。",
        "outro": "安寵，讓每隻毛孩安心入眠。",
    },
    "sleep": {
        "intro": "夜深了，陪牠一起入睡吧。",
        "middle": "輕輕的音樂，帶走今天所有的疲憊。",
        "outro": "安寵，讓每隻毛孩安心入眠。",
    },
    "relax": {
        "intro": "今天也辛苦了，你和你的毛孩。",
        "middle": "就這樣，靜靜地待在一起。",
        "outro": "安寵，讓每隻毛孩安心入眠。",
    },
    "vet_visit": {
        "intro": "看醫生了，緊張是正常的。",
        "middle": "這首音樂，讓牠知道你在。",
        "outro": "安寵，陪你們度過每一個時刻。",
    },
}


class VoiceGenerator:
    def __init__(self, api_key: str, voice_id: str, config: dict):
        self.client = ElevenLabs(api_key=api_key)
        self.voice_id = voice_id
        self.settings = config.get("elevenlabs", {})

    def generate(self, text: str, output_path: Path) -> Path:
        """生成單段語音"""
        output_path = Path(output_path)
        if output_path.exists():
            logger.info(f"語音已存在：{output_path}")
            return output_path

        output_path.parent.mkdir(parents=True, exist_ok=True)

        vs = self.settings.get("voice_settings", {})
        voice_settings = VoiceSettings(
            stability=vs.get("stability", 0.75),
            similarity_boost=vs.get("similarity_boost", 0.8),
            style=vs.get("style", 0.3),
            use_speaker_boost=vs.get("use_speaker_boost", True),
        )

        logger.info(f"ElevenLabs 生成語音：「{text[:30]}...」")

        audio_generator = self.client.text_to_speech.convert(
            voice_id=self.voice_id,
            text=text,
            model_id=self.settings.get("model_id", "eleven_multilingual_v2"),
            voice_settings=voice_settings,
        )

        with open(output_path, "wb") as f:
            for chunk in audio_generator:
                f.write(chunk)

        logger.info(f"語音已儲存：{output_path}")
        return output_path

    def generate_reel_narration(
        self,
        scene_id: str,
        output_dir: Path,
    ) -> dict[str, Path]:
        """
        生成 Reels 用的三段旁白（intro / middle / outro）
        返回 {intro: Path, middle: Path, outro: Path}
        """
        scripts = VOICE_SCRIPTS.get(scene_id, VOICE_SCRIPTS["relax"])
        output_dir = Path(output_dir)
        results = {}

        for segment, text in scripts.items():
            path = output_dir / f"{scene_id}_voice_{segment}.mp3"
            results[segment] = self.generate(text=text, output_path=path)

        return results


def generate_all_voice(scene: dict, config: dict, assets_dir: Path) -> dict:
    """
    生成一個場景的所有語音資源
    """
    api_key = config["api_keys"].get("elevenlabs", "")
    voice_id = config["brand"].get("voice_id", "")

    if not api_key or api_key == "YOUR_ELEVENLABS_API_KEY":
        logger.warning("ElevenLabs API key 未設定，跳過語音生成")
        return {}

    if not voice_id or voice_id == "YOUR_ELEVENLABS_VOICE_ID":
        logger.warning("ElevenLabs Voice ID 未設定，跳過語音生成")
        return {}

    generator = VoiceGenerator(
        api_key=api_key,
        voice_id=voice_id,
        config=config,
    )

    voice_dir = Path(assets_dir) / "voice" / scene["id"]
    voice_dir.mkdir(parents=True, exist_ok=True)

    return generator.generate_reel_narration(
        scene_id=scene["id"],
        output_dir=voice_dir,
    )


if __name__ == "__main__":
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    scene = cfg["scenes"][0]
    assets_dir = Path(os.path.expanduser(cfg["paths"]["assets_dir"]))

    result = generate_all_voice(scene, cfg, assets_dir)
    for seg, path in result.items():
        print(f"✅ {seg}：{path}")

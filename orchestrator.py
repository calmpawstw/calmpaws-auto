#!/usr/bin/env python3
"""
安寵 Calm Paws — 主排程器
執行完整自動化管線：生成 → 組裝 → 上傳 → 推廣
用法：
  python orchestrator.py --mode youtube   # 只跑 YouTube 長片
  python orchestrator.py --mode reel      # 只跑 Reels
  python orchestrator.py --mode both      # 兩者都跑（預設）
  python orchestrator.py --mode reply     # 只跑留言回覆
  python orchestrator.py --scene separation_anxiety  # 指定場景
  python orchestrator.py --dry-run        # 試跑（不實際上傳）
"""

import os
import sys
import yaml
import logging
import argparse
import json
import traceback
from pathlib import Path
from datetime import datetime, timezone

# 設定根目錄為腳本所在目錄
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from generate_music import generate_music
from generate_images import generate_all_images
from generate_voice import generate_all_voice
from assemble_video import assemble_all
from upload_youtube import upload_to_youtube
from upload_instagram import upload_reel, auto_reply_comments, InstagramPublisher


# ── Logging 設定 ──────────────────────────────────────────────────────────────

def setup_logging(logs_dir: Path):
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    return logging.getLogger("orchestrator")


# ── 場景選擇邏輯 ──────────────────────────────────────────────────────────────

def select_scene(config: dict, force_scene: str = None) -> dict:
    """
    根據權重輪換選擇場景，或強制指定
    避免連續兩次選同一場景
    """
    scenes = config["scenes"]
    history_file = Path.home() / ".calm_paws_scene_history.json"

    history = {}
    if history_file.exists():
        history = json.loads(history_file.read_text())

    if force_scene:
        scene = next((s for s in scenes if s["id"] == force_scene), None)
        if not scene:
            raise ValueError(f"找不到場景：{force_scene}")
        return scene

    # 計算加權選擇（排除上次）
    last_scene = history.get("last_scene_id")
    candidates = [s for s in scenes if s["id"] != last_scene] or scenes

    total_weight = sum(s.get("weight", 1) for s in candidates)
    import random
    r = random.uniform(0, total_weight)
    cumulative = 0
    selected = candidates[0]
    for s in candidates:
        cumulative += s.get("weight", 1)
        if r <= cumulative:
            selected = s
            break

    # 更新歷史
    history["last_scene_id"] = selected["id"]
    history["last_run"] = datetime.now(timezone.utc).isoformat()
    history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2))

    return selected


# ── 狀態追蹤 ─────────────────────────────────────────────────────────────────

def load_run_state(state_file: Path) -> dict:
    if state_file.exists():
        return json.loads(state_file.read_text())
    return {}


def save_run_state(state_file: Path, state: dict):
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str))


# ── 主要管線 ──────────────────────────────────────────────────────────────────

def run_youtube_pipeline(scene: dict, config: dict, state: dict, dry_run: bool = False) -> dict:
    """YouTube 長片完整流程"""
    logger = logging.getLogger("orchestrator.youtube")
    assets_dir = Path(os.path.expanduser(config["paths"]["assets_dir"]))
    output_dir = Path(os.path.expanduser(config["paths"]["output_dir"]))

    logger.info(f"▶ YouTube 管線開始：場景「{scene['name']}」")

    # ① 生成音樂（優先用現有 mp3 循環）
    if not state.get("music_path"):
        logger.info("① 音樂生成中...")
        # dry_run 時只產 30 秒測試音樂，加快測試速度
        music_duration = 30 if dry_run else config["schedule"]["youtube_video_duration_hours"] * 3600
        music_path = generate_music(
            scene=scene,
            config=config,
            output_dir=assets_dir / "music",
            duration_seconds=music_duration,
            use_existing=True,  # 優先使用你現有的 mix_31m10s.mp3
        )
        state["music_path"] = str(music_path)
    else:
        music_path = Path(state["music_path"])
        logger.info(f"① 音樂已存在：{music_path.name}")

    # ② 生成圖像
    if not state.get("images"):
        logger.info("② 圖像生成中...")
        images = generate_all_images(scene, config, assets_dir, dry_run=dry_run)
        state["thumbnail"] = str(images["thumbnail"])
        state["reel_backgrounds"] = [str(p) for p in images["reel_backgrounds"]]
    else:
        images = {
            "thumbnail": Path(state["thumbnail"]),
            "reel_backgrounds": [Path(p) for p in state["reel_backgrounds"]],
        }
        logger.info(f"② 圖像已存在：縮圖 + {len(images['reel_backgrounds'])} 張背景")

    # ③ 組裝影片
    if not state.get("youtube_video_path"):
        if dry_run:
            logger.info("③ [DRY RUN] 跳過影片組裝（節省時間）")
            state["youtube_video_path"] = "DRY_RUN_youtube.mp4"
            state["reel_video_path"] = "DRY_RUN_reel.mp4"
        else:
            logger.info("③ 影片組裝中...")
            assets = {
                "music": music_path,
                "thumbnail": images["thumbnail"],
                "reel_backgrounds": images["reel_backgrounds"],
                "voice": {},
            }
            videos = assemble_all(scene, config, assets, output_dir)
            state["youtube_video_path"] = str(videos["youtube"])
            state["reel_video_path"] = str(videos["reel"])
    else:
        logger.info(f"③ 影片已存在：{Path(state['youtube_video_path']).name}")

    # ④ 上傳 YouTube
    if not state.get("youtube_video_id"):
        if dry_run:
            logger.info("④ [DRY RUN] 跳過 YouTube 上傳")
            state["youtube_video_id"] = "DRY_RUN_VIDEO_ID"
        else:
            logger.info("④ 上傳 YouTube 中...")
            video_id = upload_to_youtube(
                video_path=Path(state["youtube_video_path"]),
                thumbnail_path=images["thumbnail"],
                scene=scene,
                config=config,
            )
            state["youtube_video_id"] = video_id
            logger.info(f"✅ YouTube 上傳完成：https://youtu.be/{video_id}")
    else:
        logger.info(f"④ YouTube 已上傳：{state['youtube_video_id']}")

    return state


def run_reel_pipeline(scene: dict, config: dict, state: dict, dry_run: bool = False) -> dict:
    """Reels 流程"""
    logger = logging.getLogger("orchestrator.reel")
    assets_dir = Path(os.path.expanduser(config["paths"]["assets_dir"]))
    output_dir = Path(os.path.expanduser(config["paths"]["output_dir"]))

    logger.info(f"▶ Reels 管線開始：場景「{scene['name']}」")

    # ① 音樂（短片用 60 秒段落）
    if not state.get("music_path"):
        music_path = generate_music(
            scene=scene, config=config,
            output_dir=assets_dir / "music",
            duration_seconds=60, use_existing=True,
        )
        state["music_path"] = str(music_path)
    else:
        music_path = Path(state["music_path"])

    # ② 圖像
    if not state.get("reel_backgrounds"):
        images = generate_all_images(scene, config, assets_dir, dry_run=dry_run)
        state["thumbnail"] = str(images["thumbnail"])
        state["reel_backgrounds"] = [str(p) for p in images["reel_backgrounds"]]
    else:
        images = {
            "thumbnail": Path(state["thumbnail"]),
            "reel_backgrounds": [Path(p) for p in state["reel_backgrounds"]],
        }

    # ③ 語音旁白
    if not state.get("voice"):
        logger.info("③ 語音生成中...")
        # _cp_voice_optional：elevenlabs 金鑰為空時跳過旁白。
        # 寵物舒緩音樂本來就不需要人聲，加了反而破壞放鬆感，
        # 沒必要為此讓整支流程失敗。
        try:
            if not (config.get('api_keys', {}) or {}).get('elevenlabs'):
                raise RuntimeError('elevenlabs 金鑰未設定')
            voice = generate_all_voice(scene, config, assets_dir)
        except Exception as _cp_ve:
            logger.warning(f'跳過語音旁白：{_cp_ve}')
            voice = {}

        state["voice"] = {k: str(v) for k, v in voice.items()}
    else:
        voice = {k: Path(v) for k, v in state["voice"].items()}
        logger.info("③ 語音已存在")

    # ④ 組裝 Reel
    if not state.get("reel_video_path"):
        if dry_run:
            logger.info("④ [DRY RUN] 跳過 Reel 影片組裝")
            state["reel_video_path"] = "DRY_RUN_reel.mp4"
        else:
            assets = {
                "music": music_path,
                "thumbnail": images["thumbnail"],
                "reel_backgrounds": images["reel_backgrounds"],
                "voice": voice,
            }
            videos = assemble_all(scene, config, assets, output_dir)
            state["reel_video_path"] = str(videos["reel"])
    else:
        logger.info(f"④ Reel 影片已存在")

    # ⑤ 上傳 Instagram
    if not state.get("ig_media_id"):
        if dry_run:
            logger.info("⑤ [DRY RUN] 跳過 Instagram 上傳")
            state["ig_media_id"] = "DRY_RUN_MEDIA_ID"
        else:
            logger.info("⑤ 上傳 Instagram Reels 中...")
            # _cp_skip_upload：雲端由 workflow 用 Release 附件 + Instagram API 上傳，
            # orchestrator 只負責產出影片。runner 上沒有 cloudflared，
            # 而且重複上傳會發出兩則貼文。
            if os.environ.get('CP_SKIP_UPLOAD'):
                logger.info('略過內建上傳（由 workflow 處理）')
                media_id = None
            else:
                media_id = upload_reel(
                    reel_path=Path(state["reel_video_path"]),
                    scene=scene,
                    config=config,
                    youtube_video_id=state.get("youtube_video_id"),
                )
            state["ig_media_id"] = media_id
            logger.info(f"✅ Instagram Reel 上傳完成：{media_id}")
    else:
        logger.info(f"⑤ Instagram 已上傳：{state['ig_media_id']}")

    return state


def run_reply_pipeline(config: dict, dry_run: bool = False):
    """留言自動回覆"""
    logger = logging.getLogger("orchestrator.reply")
    if not config.get("comment_reply", {}).get("enabled", True):
        logger.info("留言回覆已停用")
        return

    ig_token = config["api_keys"].get("instagram_access_token", "")
    ig_user_id = config["api_keys"].get("instagram_user_id", "")

    if not ig_token or ig_token == "YOUR_IG_ACCESS_TOKEN":
        logger.warning("Instagram token 未設定，跳過留言回覆")
        return

    if dry_run:
        logger.info("[DRY RUN] 跳過留言回覆")
        return

    publisher = InstagramPublisher(ig_token, ig_user_id)
    auto_reply_comments(publisher, config)


# ── 入口點 ────────────────────────────────────────────────────────────────────

# ── 優化引擎參數注入（由 cp_patch.py 加入）──────────────────
def _load_cp_params() -> dict:
    """
    讀取優化引擎選定的參數。
    來源優先序：scene_override.json > CP_* 環境變數。
    兩者都沒有就回空 dict，行為與加裝前完全相同。
    """
    import json as _json
    params = {}

    override = BASE_DIR / "scene_override.json"
    if override.exists():
        try:
            params.update(_json.loads(override.read_text(encoding="utf-8")))
        except Exception:
            pass

    for key in ("scene", "title_formula", "thumb_style",
                "duration_h", "upload_slot", "reel_hook", "hashtag_set"):
        env = os.environ.get(f"CP_{key.upper()}")
        if env and not params.get(key):
            params[key] = env

    return {k: v for k, v in params.items() if v and not k.startswith("_")}


def _apply_cp_params(scene: dict, config: dict, params: dict, logger) -> tuple:
    """把參數注入 scene 與 config。回傳 (scene, config)。"""
    if not params:
        return scene, config

    scene = dict(scene)   # 不改動原本的 config["scenes"] 內容

    if params.get("title_formula"):
        scene["_title_formula"] = params["title_formula"]
    if params.get("thumb_style"):
        scene["_thumb_style"] = params["thumb_style"]

    if params.get("duration_h"):
        try:
            h = float(params["duration_h"])
            config.setdefault("youtube", {})["youtube_video_duration_hours"] = h
            scene["_duration_h"] = h
        except (TypeError, ValueError):
            pass

    logger.info(
        "優化參數：" + "  ".join(
            f"{k}={v}" for k, v in params.items() if k != "scene"))
    return scene, config
# ── 注入結束 ────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="安寵 Calm Paws 自動化管線")
    parser.add_argument("--mode", choices=["youtube", "reel", "both", "reply"], default="both")
    parser.add_argument("--scene", type=str, default=None, help="指定場景 ID")
    parser.add_argument("--dry-run", action="store_true", help="試跑，不實際上傳")
    parser.add_argument("--config", type=str, default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--resume", action="store_true", help="從上次中斷點繼續")
    args = parser.parse_args()

    # 載入設定
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 展開路徑
    for key in config["paths"]:
        config["paths"][key] = os.path.expanduser(config["paths"][key])

    logs_dir = Path(config["paths"]["logs_dir"])
    logger = setup_logging(logs_dir)

    if args.dry_run:
        logger.info("🔍 DRY RUN 模式：不會實際上傳任何內容")

    logger.info("=" * 60)
    logger.info("安寵 Calm Paws 自動化管線啟動")
    logger.info(f"模式：{args.mode}  |  場景：{args.scene or '自動選擇'}")
    logger.info("=" * 60)

    # 留言回覆模式
    if args.mode == "reply":
        # _cp_reply_optional：留言回覆失敗不該擋住發文。
        # 影片已經產出，卻因為抓留言拿到 400 就整個中止，
        # 會讓後續上傳步驟全部被跳過。
        try:
            run_reply_pipeline(config, args.dry_run)
        except Exception as _cp_re:
            logger.warning(f'留言回覆略過：{_cp_re}')
        return

    # 選擇場景
    _cp_params = _load_cp_params()
    scene = select_scene(
        config,
        force_scene=args.scene or _cp_params.get('scene'))
    scene, config = _apply_cp_params(scene, config, _cp_params, logger)
    logger.info(f"本次場景：{scene['name']} (ID: {scene['id']})")

    # 載入/建立狀態檔（支援斷點續跑）
    state_file = Path(config["paths"]["logs_dir"]) / f"state_{scene['id']}.json"
    state = load_run_state(state_file) if args.resume else {}

    try:
        if args.mode in ("youtube", "both"):
            try:
                state = run_youtube_pipeline(scene, config, state, args.dry_run)
            except Exception as _e:
                logger.error(f"YouTube 階段失敗（繼續執行 Reel）：{_e}")
                if args.mode == "youtube":
                    raise
            save_run_state(state_file, state)

        if args.mode in ("reel", "both"):
            state = run_reel_pipeline(scene, config, state, args.dry_run)
            save_run_state(state_file, state)

        # 自動回覆留言
        # _cp_reply_optional：留言回覆失敗不該擋住發文。
        # 影片已經產出，卻因為抓留言拿到 400 就整個中止，
        # 會讓後續上傳步驟全部被跳過。
        try:
            run_reply_pipeline(config, args.dry_run)
        except Exception as _cp_re:
            logger.warning(f'留言回覆略過：{_cp_re}')

        logger.info("=" * 60)
        logger.info("✅ 本次自動化管線全部完成！")
        if state.get("youtube_video_id") and not args.dry_run:
            logger.info(f"   YouTube：https://youtu.be/{state['youtube_video_id']}")
        if state.get("ig_media_id") and not args.dry_run:
            logger.info(f"   Instagram Media ID：{state['ig_media_id']}")
        logger.info("=" * 60)

        # 清除狀態（完成後清空，下次重新開始）
        if state_file.exists():
            state_file.unlink()

    except Exception as e:
        logger.error(f"管線錯誤：{e}")
        logger.error(traceback.format_exc())
        save_run_state(state_file, state)
        logger.info(f"狀態已儲存至 {state_file}，下次加 --resume 可繼續")
        sys.exit(1)


if __name__ == "__main__":
    main()

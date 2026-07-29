#!/usr/bin/env python3
"""
安寵 Calm Paws — YouTube 上傳模組
使用 YouTube Data API v3 上傳長片，自動設定縮圖、標題、描述、標籤
"""

import os
import time
import random
import logging
import httplib2
import json
from pathlib import Path
from datetime import datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]
TOKEN_FILE = Path.home() / ".calm_paws_yt_token.json"
MAX_RETRIES = 10
RETRIABLE_STATUS_CODES = [500, 502, 503, 504]


def get_youtube_client(client_secrets_path: str):
    """取得已授權的 YouTube API 客戶端"""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                client_secrets_path, SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        logger.info(f"YouTube 授權 token 已儲存：{TOKEN_FILE}")

    return build("youtube", "v3", credentials=creds)


# ── 標題句型變體（由 cp_patch.py 加入）──────────────────────
def _pick_title_template(scene: dict) -> str:
    """
    依優化引擎選定的句型挑標題模板。
    scene["yt_title_variants"][formula] 存在就用它，
    否則退回原本的 yt_title_template —— 保證永遠有標題可用。
    """
    formula = scene.get("_title_formula")
    variants = scene.get("yt_title_variants") or {}
    if formula and variants.get(formula):
        return variants[formula]
    return scene["yt_title_template"]
# ── 加入結束 ────────────────────────────────────────────────


def _build_metadata(scene: dict, config: dict, video_path: Path) -> dict:
    """根據場景設定建立 YouTube 影片 metadata"""
    yt_cfg = config.get("youtube", {})
    brand = config.get("brand", {})

    duration_hours = config["schedule"].get("youtube_video_duration_hours", 8)
    duration_str = f"{duration_hours} 小時"
    channel_url = brand.get("channel_url", "")

    hashtags = " ".join([
        "#寵物音樂", "#狗狗放鬆", "#分離焦慮", "#寵物療癒", "#安寵",
        "#CalmPaws", "#432Hz", "#寵物睡眠", "#毛孩日常"
    ])

    title = _pick_title_template(scene).format(duration=duration_str)
    description = scene["yt_description_template"].format(
        duration=duration_str,
        channel_url=channel_url,
        hashtags=hashtags,
    )

    tags = list(set(yt_cfg.get("default_tags", []) + [
        scene["name"],
        f"{duration_hours}小時音樂",
        "Taiwan",
        "台灣",
    ]))

    return {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags[:500],  # YouTube 標籤上限
            "categoryId": yt_cfg.get("default_category_id", "10"),
            "defaultLanguage": yt_cfg.get("default_language", "zh-TW"),
            "defaultAudioLanguage": "zh-TW",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }


def _resumable_upload(youtube, insert_request, video_path: Path) -> str:
    """帶指數退避重試的可續傳上傳，返回 video_id"""
    response = None
    retry = 0
    file_size_mb = video_path.stat().st_size / 1024 / 1024

    logger.info(f"開始上傳：{video_path.name} ({file_size_mb:.1f} MB)")

    while response is None:
        try:
            status, response = insert_request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.info(f"上傳進度：{pct}%")
            if response is not None:
                if "id" in response:
                    video_id = response["id"]
                    logger.info(f"✅ 上傳成功！Video ID：{video_id}")
                    return video_id
                else:
                    raise RuntimeError(f"意外回應：{response}")
        except HttpError as e:
            if e.resp.status in RETRIABLE_STATUS_CODES:
                logger.warning(f"HTTP {e.resp.status}，重試中...")
            else:
                raise
        except (IOError, httplib2.HttpLib2Error) as e:
            logger.warning(f"網路錯誤：{e}，重試中...")

        retry += 1
        if retry > MAX_RETRIES:
            raise RuntimeError("上傳重試次數超限")

        sleep = random.uniform(0, min(2 ** retry, 60))
        logger.info(f"等待 {sleep:.1f}s 後重試...")
        time.sleep(sleep)

    raise RuntimeError("上傳失敗")


def set_thumbnail(youtube, video_id: str, thumbnail_path: Path):
    """設定影片縮圖"""
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/png"),
        ).execute()
        logger.info(f"縮圖已設定：{thumbnail_path.name}")
    except HttpError as e:
        logger.warning(f"縮圖設定失敗（需要 YPP 驗證帳號）：{e}")


def upload_to_youtube(
    video_path: Path,
    thumbnail_path: Path,
    scene: dict,
    config: dict,
) -> str:
    """
    上傳影片到 YouTube，返回 video_id
    """
    video_path = Path(video_path)
    thumbnail_path = Path(thumbnail_path)

    client_secrets = config["api_keys"].get("youtube_client_secrets", "client_secrets.json")
    youtube = get_youtube_client(client_secrets)

    body = _build_metadata(scene, config, video_path)

    insert_request = youtube.videos().insert(
        part=",".join(body.keys()),
        body=body,
        media_body=MediaFileUpload(
            str(video_path),
            chunksize=50 * 1024 * 1024,  # 50MB chunks
            resumable=True,
            mimetype="video/mp4",
        ),
    )

    video_id = _resumable_upload(youtube, insert_request, video_path)

    # 設定縮圖
    if thumbnail_path.exists():
        time.sleep(2)  # 等 YouTube 處理
        set_thumbnail(youtube, video_id, thumbnail_path)

    # 加入播放清單（若有設定）
    playlist_id = config.get("youtube", {}).get("playlist_id")
    if playlist_id:
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
        logger.info(f"已加入播放清單：{playlist_id}")

    youtube_url = f"https://youtu.be/{video_id}"
    logger.info(f"YouTube 影片：{youtube_url}")
    return video_id


if __name__ == "__main__":
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    print("YouTube 上傳模組已就緒。請由 orchestrator.py 呼叫。")
    print("首次執行會開啟瀏覽器進行 Google OAuth 授權。")

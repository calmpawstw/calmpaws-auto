#!/usr/bin/env python3
"""
安寵 Calm Paws — Instagram 上傳模組
使用 Meta Graph API 發佈 Reels 和留言自動回覆
需要影片先上傳到公開可訪問的 URL（使用 Cloudinary 免費方案）
"""

import os
import time
import random
import logging
import requests
import cloudinary
import cloudinary.uploader
from pathlib import Path
from anthropic import Anthropic

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.instagram.com/v21.0"


# ── Cloudinary（暫存公開 URL）────────────────────────────────────────────────
# Instagram API 需要影片從公開 URL 下載，Cloudinary 免費方案夠用

def upload_to_cloudinary(file_path: Path, config: dict) -> str:
    """
    上傳到 Cloudinary，返回公開 URL
    免費方案：25GB 儲存 / 25GB 每月流量
    """
    cld_cfg = config.get("cloudinary", {})
    cloud_name = cld_cfg.get("cloud_name") or os.environ.get("CLOUDINARY_CLOUD_NAME")
    api_key = cld_cfg.get("api_key") or os.environ.get("CLOUDINARY_API_KEY")
    api_secret = cld_cfg.get("api_secret") or os.environ.get("CLOUDINARY_API_SECRET")

    if not all([cloud_name, api_key, api_secret]):
        # Fallback：嘗試用 file.io 一次性上傳（60分鐘有效）
        return _upload_to_fileio(file_path)

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
    )

    logger.info(f"上傳到 Cloudinary：{file_path.name}")
    result = cloudinary.uploader.upload(
        str(file_path),
        resource_type="video",
        folder="calm_paws",
        overwrite=True,
        public_id=file_path.stem,
    )
    url = result["secure_url"]
    logger.info(f"Cloudinary URL：{url}")
    return url


def _upload_to_fileio(file_path: Path) -> str:
    """透過 Cloudflare Tunnel 提供公開 URL（免費、免註冊）

    啟動本機 HTTP server + cloudflared quick tunnel，
    回傳可供 Instagram 下載的公開網址。
    呼叫端需在上傳完成後呼叫 stop_public_server()。
    """
    import subprocess, threading, http.server, socketserver, time, re as _re, os, socket

    global _PUBLIC_SERVER, _TUNNEL_PROC

    serve_dir = str(file_path.parent)
    fname = file_path.name

    # 找空閒 port
    s = socket.socket(); s.bind(('', 0)); port = s.getsockname()[1]; s.close()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=serve_dir, **kw)
        def log_message(self, *a):
            pass

    httpd = socketserver.TCPServer(("", port), Handler)
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _PUBLIC_SERVER = httpd
    logger.info(f"本機 server 啟動於 port {port}")

    # 啟動 cloudflared quick tunnel
    proc = subprocess.Popen(
        ['cloudflared', 'tunnel', '--url', f'http://localhost:{port}',
         '--no-autoupdate'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    _TUNNEL_PROC = proc

    public_url = None
    deadline = time.time() + 60
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                break
            continue
        m = _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
        if m:
            public_url = m.group(0)
            break

    if not public_url:
        try:
            proc.terminate(); httpd.shutdown()
        except Exception:
            pass
        raise RuntimeError("cloudflared 未能建立公開通道")

    time.sleep(5)  # 等待通道穩定
    url = f"{public_url}/{fname}"
    logger.info(f"公開 URL: {url}")
    return url


def stop_public_server():
    """關閉 Cloudflare Tunnel 與本機 server"""
    global _PUBLIC_SERVER, _TUNNEL_PROC
    try:
        if _TUNNEL_PROC:
            _TUNNEL_PROC.terminate(); _TUNNEL_PROC = None
    except Exception:
        pass
    try:
        if _PUBLIC_SERVER:
            _PUBLIC_SERVER.shutdown(); _PUBLIC_SERVER = None
    except Exception:
        pass


_PUBLIC_SERVER = None
_TUNNEL_PROC = None


class InstagramPublisher:
    def __init__(self, access_token: str, user_id: str):
        self.token = access_token
        self.user_id = user_id
        self.session = requests.Session()
        self.session.params = {"access_token": access_token}

    def _post(self, endpoint: str, **kwargs) -> dict:
        url = f"{GRAPH_API_BASE}/{endpoint}"
        resp = self.session.post(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _get(self, endpoint: str, **kwargs) -> dict:
        url = f"{GRAPH_API_BASE}/{endpoint}"
        resp = self.session.get(url, timeout=30, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def create_reel_container(
        self,
        video_url: str,
        caption: str,
        share_to_feed: bool = True,
    ) -> str:
        """Step 1：建立 Reels 媒體容器，返回 container_id"""
        logger.info("建立 IG Reels 容器...")
        data = self._post(
            f"{self.user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": caption,
                "share_to_feed": str(share_to_feed).lower(),
            },
        )
        container_id = data["id"]
        logger.info(f"容器 ID：{container_id}")
        return container_id

    def wait_for_container(self, container_id: str, timeout: int = 300) -> bool:
        """等待 Instagram 處理影片"""
        start = time.time()
        while time.time() - start < timeout:
            data = self._get(
                container_id,
                params={"fields": "status_code,status"},
            )
            status = data.get("status_code", "")
            logger.info(f"容器狀態：{status}")

            if status == "FINISHED":
                return True
            elif status == "ERROR":
                logger.error(f"容器處理失敗：{data}")
                return False

            time.sleep(15)

        logger.error("等待容器處理超時")
        return False

    def publish_container(self, container_id: str) -> str:
        """Step 2：發佈容器，返回 media_id"""
        logger.info("發佈 Reel...")
        data = self._post(
            f"{self.user_id}/media_publish",
            data={"creation_id": container_id},
        )
        media_id = data["id"]
        logger.info(f"✅ Reel 發佈成功！Media ID：{media_id}")
        return media_id

    def publish_reel(self, video_url: str, caption: str) -> str:
        """完整兩步驟發佈流程"""
        container_id = self.create_reel_container(video_url, caption)
        if not self.wait_for_container(container_id):
            raise RuntimeError("Reels 容器處理失敗")
        return self.publish_container(container_id)

    def get_recent_comments(self, media_id: str, limit: int = 20) -> list[dict]:
        """取得最新留言"""
        data = self._get(
            f"{media_id}/comments",
            params={"fields": "id,text,username,timestamp", "limit": limit},
        )
        return data.get("data", [])

    def reply_to_comment(self, media_id: str, comment_id: str, reply_text: str):
        """回覆留言"""
        self._post(
            f"{media_id}/replies",
            data={"message": reply_text},
        )
        logger.info(f"已回覆留言 {comment_id}")

    def get_all_media(self, limit: int = 10) -> list[dict]:
        """取得最新貼文清單"""
        data = self._get(
            f"{self.user_id}/media",
            params={"fields": "id,caption,timestamp,media_type", "limit": limit},
        )
        return data.get("data", [])


# ── 自動留言回覆 ──────────────────────────────────────────────────────────────

def auto_reply_comments(publisher: InstagramPublisher, config: dict):
    """
    掃描所有最近貼文的留言，用 Claude API 生成回覆
    """
    anthropic_key = config["api_keys"].get("anthropic", "")
    if not anthropic_key or anthropic_key == "YOUR_ANTHROPIC_API_KEY":
        logger.warning("Anthropic API key 未設定，跳過留言回覆")
        return

    client = Anthropic(api_key=anthropic_key)
    system_prompt = config.get("comment_reply", {}).get("system_prompt", "")
    max_replies = config.get("comment_reply", {}).get("max_replies_per_run", 20)
    replied_file = Path.home() / ".calm_paws_replied_comments.txt"

    # 載入已回覆的留言 ID
    replied_ids = set()
    if replied_file.exists():
        replied_ids = set(replied_file.read_text().splitlines())

    replied_count = 0
    media_list = publisher.get_all_media(limit=10)

    for media in media_list:
        if replied_count >= max_replies:
            break

        comments = publisher.get_recent_comments(media["id"])
        for comment in comments:
            if replied_count >= max_replies:
                break
            if comment["id"] in replied_ids:
                continue

            # 用 Claude 生成回覆
            try:
                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=100,
                    system=system_prompt,
                    messages=[{
                        "role": "user",
                        "content": f"留言：{comment['text']}\n用戶名：{comment['username']}"
                    }]
                )
                reply = response.content[0].text.strip()

                publisher.reply_to_comment(media["id"], comment["id"], reply)
                replied_ids.add(comment["id"])
                replied_count += 1
                logger.info(f"回覆 @{comment['username']}：{reply[:50]}...")
                time.sleep(2)  # 避免 rate limit

            except Exception as e:
                logger.warning(f"回覆失敗：{e}")

    # 儲存已回覆清單
    replied_file.write_text("\n".join(replied_ids))
    logger.info(f"本次共回覆 {replied_count} 則留言")


# ── 主要介面 ──────────────────────────────────────────────────────────────────

def _build_caption(scene: dict, config: dict, youtube_video_id: str = None) -> str:
    """建立 Reels 文案（含 hashtag）"""
    brand = config["brand"]
    ig_cfg = config.get("instagram", {})
    hashtags_pool = ig_cfg.get("hashtags_pool", [])
    n = ig_cfg.get("hashtags_per_post", 12)
    hashtags = " ".join(random.sample(hashtags_pool, min(n, len(hashtags_pool))))

    caption_template = scene.get("reel_caption_template", "")
    caption = caption_template.replace("{brand_tagline}", brand["tagline"])

    if youtube_video_id:
        yt_url = f"https://youtu.be/{youtube_video_id}"
        caption += f"\n\n▶ 完整 8 小時版：{yt_url}"

    caption += f"\n\n{hashtags}"
    return caption


def upload_reel(
    reel_path: Path,
    scene: dict,
    config: dict,
    youtube_video_id: str = None,
) -> str:
    """
    上傳 Reel 到 Instagram，返回 media_id
    """
    reel_path = Path(reel_path)
    ig_token = config["api_keys"].get("instagram_access_token", "")
    ig_user_id = config["api_keys"].get("instagram_user_id", "")

    if not ig_token or ig_token == "YOUR_IG_ACCESS_TOKEN":
        raise ValueError("請在 config.yaml 設定 Instagram Access Token")

    # 1. 上傳影片到公開 URL
    video_url = upload_to_cloudinary(reel_path, config)

    # 2. 建立文案
    caption = _build_caption(scene, config, youtube_video_id)

    # 3. 發佈到 Instagram
    publisher = InstagramPublisher(ig_token, ig_user_id)
    media_id = publisher.publish_reel(video_url, caption)

    logger.info(f"Instagram Reel 上傳完成：{media_id}")
    return media_id


if __name__ == "__main__":
    import yaml
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)

    print("Instagram 上傳模組已就緒。請由 orchestrator.py 呼叫。")
    print("確認已設定 config.yaml 中的 instagram_access_token 和 instagram_user_id。")

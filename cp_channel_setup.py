#!/usr/bin/env python3
"""
安寵 Calm Paws — 自動補齊 YouTube 頻道設定

可以自動做的（YouTube Data API）：
  1. 頻道說明與關鍵字
  2. 依場景建立播放清單，並把既有影片加進去
  3. 修正既有影片說明欄裡指向錯誤頻道的網址

無法自動做的（沒有對應 API，必須手動）：
  ・Instagram 的名稱、簡介、連結 —— Graph API 不提供編輯個人檔案的端點
  ・YouTube 橫幅圖片 —— 需要你提供圖片
  ・追蹤別的帳號 —— 沒有 API，而且自動追蹤本來就不該做

用法：
    python cp_channel_setup.py --check    # 只看會改什麼，不動任何東西
    python cp_channel_setup.py --apply    # 實際套用
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

BASE = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
sys.path.insert(0, str(BASE))

CORRECT_HANDLE = "@calmpawstw"
WRONG_URL = "https://www.youtube.com/@calmpaws"
RIGHT_URL = "https://www.youtube.com/@calmpawstw"

CHANNEL_DESC = """安寵 Calm Paws｜專為毛孩設計的療癒音樂

我們製作專門給狗狗與貓咪聆聽的放鬆音樂。
所有曲目皆為原創編曲，針對寵物的聽覺特性調整頻率與節奏，
不使用突然的音量變化或高頻聲響，適合長時間播放。

▎適用情境
・分離焦慮 — 你出門上班、牠獨自在家的時候
・睡眠 — 夜晚難以安定、頻繁走動
・就醫前 — 出門前的緊張與不安
・日常放鬆 — 需要安靜背景音的任何時候

▎關於音樂
每首都是我們自己編寫錄製，長時間循環不會出現突兀的接點。
432Hz 調頻與較慢的節拍，是參考寵物聽覺研究後的選擇。

Instagram：@calmpaws.tw
"""

CHANNEL_KEYWORDS = ('寵物音樂 狗狗放鬆音樂 貓咪音樂 分離焦慮 寵物助眠 '
                    '毛孩療癒 432Hz 寵物睡眠音樂 "pet relaxing music" '
                    '"dog calming music"')

# 場景 → 播放清單標題與說明
PLAYLISTS = {
    "separation_anxiety": (
        "分離焦慮｜你不在家的時候",
        "你出門上班、牠獨自在家的時候播放。連續播放可陪伴一整個工作日。"),
    "sleep": (
        "睡眠｜深夜安眠",
        "夜晚難以安定、頻繁走動時播放。適合整夜循環。"),
    "relax": (
        "日常放鬆｜隨時可播",
        "需要安靜背景音的任何時候。"),
    "vet_visit": (
        "就醫前｜緊張紓解",
        "出門看醫生前的緊張與不安，出發前先播放一段。"),
    "thunderstorm": (
        "打雷放鞭炮｜安撫",
        "台灣夏季雷雨與節慶鞭炮時段。"),
    "kitten_calm": ("幼貓安撫", "給剛到家的小貓。"),
    "senior_pet": ("老年寵物", "給年紀大的毛孩。"),
    "car_travel": ("車程安撫", "搭車移動時播放。"),
}


def get_client():
    import yaml
    from upload_youtube import get_youtube_client, TOKEN_FILE
    if not TOKEN_FILE.exists():
        print(f"❌ 找不到 YouTube 授權檔：{TOKEN_FILE}")
        return None
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    sec = cfg.get("api_keys", {}).get("youtube_client_secrets", "client_secrets.json")
    if not os.path.isabs(sec):
        sec = str(BASE / sec)
    return get_youtube_client(sec)


def my_channel(yt):
    r = yt.channels().list(part="id,snippet,brandingSettings,contentDetails",
                           mine=True).execute()
    return r["items"][0] if r.get("items") else None


def my_videos(yt, ch):
    up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    vids, token = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=up,
                                    maxResults=50, pageToken=token).execute()
        vids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    if not vids:
        return []
    out = []
    for i in range(0, len(vids), 50):
        r = yt.videos().list(part="snippet,status",
                             id=",".join(vids[i:i + 50])).execute()
        out += r.get("items", [])
    return out


def scene_of(video_id: str, title: str) -> str | None:
    """先查資料庫，查不到再用標題關鍵字猜"""
    db = BASE / "data" / "metrics.db"
    if db.exists():
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT scene FROM videos WHERE video_id=?",
                               (video_id,)).fetchone()
            conn.close()
            if row and row["scene"]:
                return row["scene"]
        except Exception:
            pass
    hints = {
        "separation_anxiety": ["分離焦慮", "不在家", "上班"],
        "sleep": ["睡", "安眠", "深夜", "晚上"],
        "relax": ["放鬆", "日常", "平靜"],
        "vet_visit": ["看醫生", "就醫", "獸醫", "緊張"],
        "thunderstorm": ["打雷", "雷雨", "鞭炮"],
    }
    for sc, words in hints.items():
        if any(w in title for w in words):
            return sc
    return None


def run(apply: bool):
    yt = get_client()
    if not yt:
        return 1
    ch = my_channel(yt)
    if not ch:
        print("❌ 抓不到頻道資訊")
        return 1

    cid = ch["id"]
    bs = ch.get("brandingSettings", {}).get("channel", {})
    print(f"頻道：{ch['snippet']['title']}（{cid}）")
    print(f"目前說明：{'（空白）' if not bs.get('description') else bs['description'][:40] + '…'}")
    print(f"目前關鍵字：{bs.get('keywords') or '（未設定）'}")
    print()

    todo = []

    # ① 頻道說明與關鍵字
    need_desc = (bs.get("description") or "").strip() != CHANNEL_DESC.strip()
    need_kw = (bs.get("keywords") or "").strip() != CHANNEL_KEYWORDS.strip()
    if need_desc or need_kw:
        todo.append("更新頻道說明與關鍵字")

    # ② 影片說明欄的錯誤網址
    videos = my_videos(yt, ch)
    bad = [v for v in videos if WRONG_URL in (v["snippet"].get("description") or "")
           and RIGHT_URL not in (v["snippet"].get("description") or "")]
    print(f"影片共 {len(videos)} 支，其中 {len(bad)} 支說明欄指向錯誤頻道")
    for v in bad:
        print(f"   ・{v['snippet']['title'][:40]}")
    if bad:
        todo.append(f"修正 {len(bad)} 支影片的說明欄")

    # ③ 播放清單
    existing = {}
    r = yt.playlists().list(part="snippet", mine=True, maxResults=50).execute()
    for p in r.get("items", []):
        existing[p["snippet"]["title"]] = p["id"]

    by_scene: dict[str, list] = {}
    for v in videos:
        sc = scene_of(v["id"], v["snippet"]["title"])
        if sc and sc in PLAYLISTS:
            by_scene.setdefault(sc, []).append(v)

    print(f"\n播放清單：目前有 {len(existing)} 個")
    plan_pl = []
    for sc, vs in sorted(by_scene.items()):
        title = PLAYLISTS[sc][0]
        mark = "已存在" if title in existing else "要建立"
        print(f"   ・{title}（{len(vs)} 支）— {mark}")
        plan_pl.append((sc, title, vs))
    if plan_pl:
        todo.append(f"整理 {len(plan_pl)} 個播放清單")
    unmatched = [v for v in videos
                 if not scene_of(v["id"], v["snippet"]["title"])]
    if unmatched:
        print(f"   ⚠️ {len(unmatched)} 支對不到場景，不會加入清單：")
        for v in unmatched[:5]:
            print(f"      {v['snippet']['title'][:40]}")

    print("\n" + "═" * 50)
    if not todo:
        print("✅ 全部都已經補齊，沒有要做的事")
        return 0
    print("要做的事：")
    for t in todo:
        print(f"   ・{t}")
    print("═" * 50)

    if not apply:
        print("\n（--check 模式，未實際修改。加 --apply 才會執行）")
        return 0

    print()
    # ── 執行 ①
    if need_desc or need_kw:
        try:
            newbs = dict(ch.get("brandingSettings", {}))
            chset = dict(newbs.get("channel", {}))
            chset["description"] = CHANNEL_DESC
            chset["keywords"] = CHANNEL_KEYWORDS
            newbs["channel"] = chset
            yt.channels().update(part="brandingSettings",
                                 body={"id": cid, "brandingSettings": newbs}).execute()
            print("✅ 頻道說明與關鍵字已更新")
        except Exception as e:
            print(f"❌ 更新頻道資訊失敗：{type(e).__name__}: {e}")

    # ── 執行 ②
    for v in bad:
        try:
            sn = dict(v["snippet"])
            sn["description"] = sn["description"].replace(WRONG_URL, RIGHT_URL)
            # videos.update 要求 snippet 帶齊必要欄位
            body = {"id": v["id"], "snippet": {
                "title": sn["title"],
                "description": sn["description"],
                "categoryId": sn.get("categoryId", "10"),
                "tags": sn.get("tags", []),
                "defaultLanguage": sn.get("defaultLanguage", "zh-Hant"),
            }}
            yt.videos().update(part="snippet", body=body).execute()
            print(f"✅ 已修正說明欄：{sn['title'][:34]}")
        except Exception as e:
            print(f"❌ 修正失敗 {v['snippet']['title'][:24]}：{type(e).__name__}: {e}")

    # ── 執行 ③
    for sc, title, vs in plan_pl:
        try:
            pid = existing.get(title)
            if not pid:
                r = yt.playlists().insert(part="snippet,status", body={
                    "snippet": {"title": title,
                                "description": PLAYLISTS[sc][1],
                                "defaultLanguage": "zh-Hant"},
                    "status": {"privacyStatus": "public"},
                }).execute()
                pid = r["id"]
                print(f"✅ 已建立播放清單：{title}")
            # 已在清單裡的不重複加
            inpl = set()
            try:
                rr = yt.playlistItems().list(part="contentDetails",
                                             playlistId=pid, maxResults=50).execute()
                inpl = {i["contentDetails"]["videoId"] for i in rr.get("items", [])}
            except Exception:
                pass
            added = 0
            for v in vs:
                if v["id"] in inpl:
                    continue
                yt.playlistItems().insert(part="snippet", body={
                    "snippet": {"playlistId": pid,
                                "resourceId": {"kind": "youtube#video",
                                               "videoId": v["id"]}}}).execute()
                added += 1
            if added:
                print(f"   → 加入 {added} 支影片")
        except Exception as e:
            print(f"❌ 播放清單 {title} 失敗：{type(e).__name__}: {e}")

    print("\n✅ 完成")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not a.check and not a.apply:
        ap.print_help()
        return 0
    return run(apply=a.apply)


if __name__ == "__main__":
    sys.exit(main())

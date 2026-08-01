#!/usr/bin/env python3
"""
安寵 Calm Paws — 音樂庫

你把自己做的音檔丟進一個資料夾，這支負責：
  1. 掃描、讀出長度、算出內容指紋（避免同一首重複收錄）
  2. 上傳到 GitHub Release（音檔不進 repo —— 30MB 的檔案放 repo
     很快就會爆，Release 單檔上限 2GB 且不計入 repo 大小）
  3. 產生一份很小的清單 JSON 進 repo，雲端靠它知道有哪些曲子
  4. 記錄每首用過幾次，挑選時優先選最久沒用的，做到不重複

資料夾規則（放在 ~/calm_paws/music_bank/）：

    music_bank/
      ├── 任何一首.mp3          ← 放根目錄＝任何場景都可以用
      ├── sleep/
      │     └── 深夜安眠.mp3    ← 放場景資料夾＝只給該場景用
      ├── separation_anxiety/
      ├── relax/
      ├── vet_visit/
      ├── thunderstorm/
      ├── kitten_calm/
      ├── senior_pet/
      └── car_travel/

  想新增音樂：直接丟檔案進去，再跑一次「更新音樂庫.command」。
  已上傳過的會自動跳過，只處理新的。

用法：
    python cp_music_bank.py --scan            # 只看有什麼，不上傳
    python cp_music_bank.py --sync            # 掃描＋上傳新檔＋更新清單
    python cp_music_bank.py --status          # 看每首用過幾次
    python cp_music_bank.py --pick sleep      # 挑一首（雲端用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
BANK_DIR = BASE / "music_bank"
MANIFEST = BASE / "data" / "music_manifest.json"
DB_PATH = BASE / "data" / "metrics.db"
RELEASE_TAG = "music-bank"

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS music_usage (
    track_id   TEXT,
    scene      TEXT,
    used_at    TEXT,
    PRIMARY KEY (track_id, used_at)
);
CREATE INDEX IF NOT EXISTS idx_music_usage_track ON music_usage(track_id);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def probe_duration(p: Path) -> float:
    """讀音檔長度（秒）。讀不到回 0，呼叫端會當成不可用。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=60)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def file_fingerprint(p: Path) -> str:
    """
    用檔案前後各 1MB + 檔案大小算指紋。
    不讀整份是因為 30MB 的檔案很多首時會很慢，
    而前後段＋大小已足以分辨是不是同一份檔案。
    """
    h = hashlib.sha256()
    size = p.stat().st_size
    h.update(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(1024 * 1024))
        if size > 2 * 1024 * 1024:
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()[:16]


def safe_asset_name(track_id: str, original: str) -> str:
    """
    GitHub Release 附件名稱不能有空白與部分符號，中文也容易出問題。
    用 track_id 當主檔名，保證唯一且純 ASCII；原始檔名留在清單裡顯示。
    """
    ext = Path(original).suffix.lower() or ".mp3"
    return f"{track_id}{ext}"


def scan_bank() -> list[dict]:
    """掃描 music_bank，回傳曲目清單"""
    if not BANK_DIR.exists():
        return []
    tracks = []
    for p in sorted(BANK_DIR.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXT:
            continue
        if p.name.startswith("."):
            continue
        rel = p.relative_to(BANK_DIR)
        # 放在子資料夾＝該場景專用；放根目錄＝任何場景可用
        scene = rel.parts[0] if len(rel.parts) > 1 else "any"
        tid = file_fingerprint(p)
        dur = probe_duration(p)
        tracks.append({
            "id": tid,
            "name": p.name,
            "scene": scene,
            "duration": round(dur, 1),
            "size_mb": round(p.stat().st_size / 1024 / 1024, 1),
            "local_path": str(p),
            "asset": safe_asset_name(tid, p.name),
        })
    return tracks


def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"tracks": [], "updated_at": None}


def save_manifest(tracks: list[dict], repo: str):
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    # 清單不放本機路徑 —— 雲端看不到，留著只會誤導
    clean = [{k: v for k, v in t.items() if k != "local_path"} for t in tracks]
    MANIFEST.write_text(json.dumps({
        "repo": repo,
        "release_tag": RELEASE_TAG,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tracks": clean,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def gh(args: list[str], **kw):
    return subprocess.run(["gh"] + args, capture_output=True, text=True, **kw)


def ensure_release(repo: str) -> bool:
    r = gh(["release", "view", RELEASE_TAG, "--repo", repo])
    if r.returncode == 0:
        return True
    r = gh(["release", "create", RELEASE_TAG, "--repo", repo,
            "--title", "音樂庫", "--notes",
            "原創音檔存放處。由 cp_music_bank.py 管理，請勿手動刪除。",
            "--latest=false"])
    if r.returncode != 0:
        print(f"❌ 無法建立 Release：{r.stderr.strip()[:200]}")
        return False
    return True


def uploaded_assets(repo: str) -> set:
    r = gh(["release", "view", RELEASE_TAG, "--repo", repo,
            "--json", "assets", "--jq", ".assets[].name"])
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


def cmd_scan():
    tracks = scan_bank()
    if not tracks:
        print(f"音樂庫是空的：{BANK_DIR}")
        print("把音檔丟進去，放根目錄＝任何場景可用，")
        print("放場景資料夾（例如 sleep/）＝只給該場景用。")
        return 0
    by_scene: dict[str, list] = {}
    for t in tracks:
        by_scene.setdefault(t["scene"], []).append(t)
    total_mb = sum(t["size_mb"] for t in tracks)
    print(f"共 {len(tracks)} 首，{total_mb:.0f} MB\n")
    for scene, ts in sorted(by_scene.items()):
        label = "（任何場景可用）" if scene == "any" else ""
        print(f"  {scene} {label}")
        for t in ts:
            m, s = divmod(int(t["duration"]), 60)
            warn = "  ⚠️ 讀不到長度" if t["duration"] <= 0 else ""
            print(f"    ・{t['name']}  {m}分{s:02d}秒  {t['size_mb']}MB{warn}")
    bad = [t for t in tracks if t["duration"] <= 0]
    if bad:
        print(f"\n⚠️ 有 {len(bad)} 個檔案讀不到長度，可能已損毀或格式不支援")
    return 0


def cmd_sync(repo: str):
    tracks = scan_bank()
    if not tracks:
        print(f"音樂庫是空的：{BANK_DIR}")
        return 1

    bad = [t for t in tracks if t["duration"] <= 0]
    if bad:
        print(f"⚠️ 跳過 {len(bad)} 個讀不到長度的檔案：")
        for t in bad:
            print(f"    {t['name']}")
        tracks = [t for t in tracks if t["duration"] > 0]

    if not ensure_release(repo):
        return 1

    have = uploaded_assets(repo)
    todo = [t for t in tracks if t["asset"] not in have]
    print(f"音樂庫共 {len(tracks)} 首，其中 {len(todo)} 首需要上傳")

    for i, t in enumerate(todo, 1):
        m, s = divmod(int(t["duration"]), 60)
        print(f"  [{i}/{len(todo)}] {t['name']}（{m}分{s:02d}秒，{t['size_mb']}MB）...",
              flush=True)
        # 用 track_id 當附件名，避免中文與空白造成的下載問題
        tmp = Path("/tmp") / t["asset"]
        try:
            tmp.write_bytes(Path(t["local_path"]).read_bytes())
            r = gh(["release", "upload", RELEASE_TAG, str(tmp),
                    "--repo", repo, "--clobber"])
            if r.returncode != 0:
                print(f"      ❌ 上傳失敗：{r.stderr.strip()[:150]}")
            else:
                print(f"      ✅")
        finally:
            tmp.unlink(missing_ok=True)

    for t in tracks:
        t["url"] = (f"https://github.com/{repo}/releases/download/"
                    f"{RELEASE_TAG}/{t['asset']}")

    save_manifest(tracks, repo)
    print(f"\n✅ 清單已更新：{MANIFEST}")
    print("   記得推送到雲端才會生效。")
    return 0


def candidates_for(scene: str, tracks: list[dict]) -> list[dict]:
    """該場景可用的曲目：場景專屬 + 通用"""
    return [t for t in tracks if t["scene"] in (scene, "any")]


def cmd_pick(scene: str, quiet: bool = False):
    """
    挑一首最久沒用的。全部用過就從最早用的開始輪，
    所以在用完一輪之前不會重複。
    """
    man = load_manifest()
    tracks = man.get("tracks", [])
    if not tracks:
        if not quiet:
            print("MANIFEST_EMPTY", file=sys.stderr)
        return 1

    cands = candidates_for(scene, tracks)
    if not cands:
        if not quiet:
            print(f"NO_TRACK_FOR_SCENE:{scene}", file=sys.stderr)
        return 1

    conn = db()
    rows = conn.execute(
        "SELECT track_id, MAX(used_at) last_used, COUNT(*) n "
        "FROM music_usage GROUP BY track_id").fetchall()
    conn.close()
    used = {r["track_id"]: (r["last_used"], r["n"]) for r in rows}

    # 沒用過的排最前面，其次是最久沒用的
    def sort_key(t):
        last, n = used.get(t["id"], ("", 0))
        return (n, last)

    pick = sorted(cands, key=sort_key)[0]
    print(json.dumps(pick, ensure_ascii=False))
    return 0


def record_use(track_id: str, scene: str):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO music_usage VALUES (?,?,?)",
                 (track_id, scene, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def cmd_status():
    man = load_manifest()
    tracks = man.get("tracks", [])
    if not tracks:
        print("清單是空的，先跑 --sync")
        return 1
    conn = db()
    rows = conn.execute(
        "SELECT track_id, MAX(used_at) last_used, COUNT(*) n "
        "FROM music_usage GROUP BY track_id").fetchall()
    conn.close()
    used = {r["track_id"]: (r["last_used"], r["n"]) for r in rows}

    print(f"音樂庫共 {len(tracks)} 首（更新於 {man.get('updated_at','?')[:16]}）\n")
    by_scene: dict[str, list] = {}
    for t in tracks:
        by_scene.setdefault(t["scene"], []).append(t)
    for scene, ts in sorted(by_scene.items()):
        print(f"  {scene}")
        for t in sorted(ts, key=lambda x: used.get(x["id"], ("", 0))[1]):
            last, n = used.get(t["id"], ("從未使用", 0))
            m, s = divmod(int(t["duration"]), 60)
            print(f"    ・{t['name']:<28} {m}分{s:02d}秒  用過 {n} 次"
                  f"  最近 {last[:10] if n else '—'}")
    # 各場景輪完一輪要多久
    print("\n輪替週期（以每支影片用一首估算）：")
    for scene in sorted({t["scene"] for t in tracks} - {"any"}):
        c = len(candidates_for(scene, tracks))
        print(f"  {scene}: {c} 首可用")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--pick", metavar="SCENE")
    ap.add_argument("--record", nargs=2, metavar=("TRACK_ID", "SCENE"))
    ap.add_argument("--repo", default=os.environ.get("CP_REPO", "calmpawstw/calmpaws-auto"))
    args = ap.parse_args()

    if args.scan:
        return cmd_scan()
    if args.sync:
        return cmd_sync(args.repo)
    if args.status:
        return cmd_status()
    if args.pick:
        return cmd_pick(args.pick)
    if args.record:
        record_use(args.record[0], args.record[1])
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
統一 Instagram 貼文的行動呼籲（CTA）。

要解決的問題：
  ・separation_anxiety 寫著「▶ 完整版連結在限動」，但你沒有在發限時動態，
    profile 也沒有連結 —— 對每個看到的人開空頭支票。
  ・其他場景完全沒有提到 YouTube，等於白白放掉每一次曝光。

改法：四個場景結尾統一成同一句，指向個人檔案的連結。
統一的另一個好處是，日後才能從數據判斷這句 CTA 到底有沒有用 ——
每個場景講不同的話，就沒有對照基準。

用文字層級的精準修改而不是 yaml.safe_dump 重寫整份，
因為 safe_dump 會把 config.yaml 裡所有註解都吃掉。

用法：
    python cp_patch_cta.py --check     # 只看會改成什麼
    python cp_patch_cta.py             # 實際修改（會先備份）
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
TARGET = BASE / "config.yaml"

CTA = "▶ 8 小時完整版在 YouTube｜點簡介連結"

# 舊的、要被取代的收尾句特徵
STALE_PAT = re.compile(r"(連結在限動|完整版連結|點簡介連結|完整版在 ?YouTube)")


def patch(src: str):
    lines = src.splitlines(keepends=True)
    out = []
    i = 0
    changed = []

    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\s+)reel_caption_template:\s*>\s*$", line)
        if not m:
            out.append(line)
            i += 1
            continue

        head_indent = m.group(1)
        out.append(line)
        i += 1

        # 收集這個區塊的內容行（縮排比 key 更深）
        block = []
        while i < len(lines):
            nxt = lines[i]
            if nxt.strip() == "":
                # 空行可能是區塊結束，先看下一行縮排
                if (i + 1 < len(lines)
                        and lines[i + 1].strip()
                        and len(lines[i + 1]) - len(lines[i + 1].lstrip())
                            > len(head_indent)):
                    block.append(nxt)
                    i += 1
                    continue
                break
            ind = len(nxt) - len(nxt.lstrip())
            if ind <= len(head_indent):
                break
            block.append(nxt)
            i += 1

        if not block:
            continue

        body_indent = block[0][:len(block[0]) - len(block[0].lstrip())]
        # 丟掉舊的收尾句
        kept = [b for b in block if not STALE_PAT.search(b)]
        # 去掉尾端空行，再接上統一的 CTA
        while kept and not kept[-1].strip():
            kept.pop()
        kept.append(f"{body_indent}{CTA}\n")

        before = "".join(block).strip()
        after = "".join(kept).strip()
        if before != after:
            changed.append((before, after))
        out.extend(kept)

    return "".join(out), changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"❌ 找不到 {TARGET}")
        return 1

    src = TARGET.read_text(encoding="utf-8")
    new_src, changed = patch(src)

    if not changed:
        print("✅ CTA 已經是統一的，不需修改")
        return 0

    print(f"會修改 {len(changed)} 個場景：\n")
    for before, after in changed:
        print("  ── 修改前")
        for ln in before.split("\n"):
            print(f"     {ln.strip()}")
        print("  ── 修改後")
        for ln in after.split("\n"):
            print(f"     {ln.strip()}")
        print()

    if args.check:
        print("（--check，未實際修改）")
        return 0

    # 改完必須仍是合法 YAML，而且場景數與欄位不能少
    import yaml
    old = yaml.safe_load(src)
    try:
        new = yaml.safe_load(new_src)
    except Exception as e:
        print(f"❌ 修改後 YAML 無效，放棄：{e}")
        return 1

    if len(new.get("scenes", [])) != len(old.get("scenes", [])):
        print("❌ 場景數量改變了，放棄")
        return 1
    for a, b in zip(old["scenes"], new["scenes"]):
        if set(a.keys()) != set(b.keys()) or a["id"] != b["id"]:
            print(f"❌ 場景 {a.get('id')} 的欄位有變動，放棄")
            return 1
        if not b.get("reel_caption_template", "").strip().endswith(CTA):
            print(f"❌ 場景 {a['id']} 的 CTA 沒接上，放棄")
            return 1

    # 註解不能被吃掉
    old_comments = src.count("#")
    new_comments = new_src.count("#")
    if new_comments < old_comments:
        print(f"❌ 註解減少了（{old_comments} → {new_comments}），放棄")
        return 1

    backup = TARGET.with_suffix(".yaml.bak_cta")
    shutil.copyfile(TARGET, backup)
    TARGET.write_text(new_src, encoding="utf-8")
    print(f"✅ 已修改 {len(changed)} 個場景，備份：{backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
安寵 Calm Paws — 讓 orchestrator 在雲端跳過自行上傳

問題：
  orchestrator 的 run_reel_pipeline 會自己呼叫 upload_reel()，
  而 upload_reel 走的是 Cloudflare Tunnel（當初為 Mac 寫的）。
  GitHub runner 上沒有 cloudflared，直接 FileNotFoundError。

  就算裝了 cloudflared 也不該這樣走 —— 雲端 workflow 已經有
  「Release 附件取得公開網址 → Instagram API」的路徑，
  讓 orchestrator 再上傳一次會變成重複發文。

作法：
  設了環境變數 CP_SKIP_UPLOAD 就跳過 orchestrator 內建的上傳，
  只把影片產出來，交給 workflow 後續步驟處理。
  本機不設這個變數，行為完全不變。

用 AST 定位而非字串比對，因為 upload_reel( 的呼叫跨多行。
"""
import ast
import os
import shutil
import sys
import py_compile
from pathlib import Path

BASE = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
TARGET = BASE / "orchestrator.py"
MARK = "_cp_skip_upload"


def find_upload_calls(src: str, func_names: set) -> list:
    """回傳 [(起始行, 結束行, 縮排)]，1-based，含頭尾"""
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        name = None
        if isinstance(call.func, ast.Name):
            name = call.func.id
        elif isinstance(call.func, ast.Attribute):
            name = call.func.attr
        if name in func_names:
            hits.append((node.lineno, node.end_lineno,
                         [t.id for t in node.targets
                          if isinstance(t, ast.Name)]))
    return hits


def patch() -> bool:
    if not TARGET.exists():
        print(f"❌ 找不到 {TARGET}")
        return False

    src = TARGET.read_text(encoding="utf-8")
    if MARK in src:
        print("✅ 已修補過，略過")
        return True

    hits = find_upload_calls(src, {"upload_reel"})
    if not hits:
        print("⚠️  找不到 upload_reel 呼叫，可能已改過結構")
        return False

    lines = src.split("\n")
    # 由後往前改，避免行號位移
    for start, end, targets in sorted(hits, reverse=True):
        block = lines[start - 1:end]
        indent = block[0][:len(block[0]) - len(block[0].lstrip())]
        var = targets[0] if targets else "media_id"

        wrapped = [
            f"{indent}# {MARK}：雲端由 workflow 用 Release 附件 + Instagram API 上傳，",
            f"{indent}# orchestrator 只負責產出影片。runner 上沒有 cloudflared，",
            f"{indent}# 而且重複上傳會發出兩則貼文。",
            f"{indent}if os.environ.get('CP_SKIP_UPLOAD'):",
            f"{indent}    logger.info('略過內建上傳（由 workflow 處理）')",
            f"{indent}    {var} = None",
            f"{indent}else:",
        ]
        wrapped += ["    " + l if l.strip() else l for l in block]

        lines[start - 1:end] = wrapped
        print(f"✅ 已包覆第 {start}-{end} 行的 {var} = upload_reel(...)")

    new_src = "\n".join(lines)

    if "\nimport os" not in new_src and not new_src.startswith("import os"):
        new_src = "import os\n" + new_src

    backup = str(TARGET) + ".preupload.bak"
    shutil.copy(TARGET, backup)
    TARGET.write_text(new_src, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
        print("✅ 語法檢查通過")
        return True
    except py_compile.PyCompileError as e:
        shutil.copy(backup, TARGET)
        print(f"❌ 語法錯誤，已還原：{e}")
        return False


def verify() -> bool:
    """靜態確認：設了 CP_SKIP_UPLOAD 就不會走到 cloudflared"""
    src = TARGET.read_text(encoding="utf-8")
    checks = [
        (MARK in src, "已加入跳過上傳的標記"),
        ("CP_SKIP_UPLOAD" in src, "會讀取 CP_SKIP_UPLOAD"),
    ]
    up = BASE / "upload_instagram.py"
    if up.exists():
        has_cf = "cloudflared" in up.read_text(encoding="utf-8")
        checks.append((True,
                       f"upload_instagram 仍含 cloudflared："
                       f"{'是（本機用）' if has_cf else '否'}"))
    allok = True
    for good, msg in checks:
        print(f"  {'✅' if good else '❌'} {msg}")
        allok = allok and good
    return allok


if __name__ == "__main__":
    print("═" * 54)
    print("  讓 orchestrator 在雲端跳過自行上傳")
    print("═" * 54)
    print(f"  目標：{TARGET}")
    print()
    okp = patch()
    print()
    print("驗證：")
    okv = verify()
    sys.exit(0 if (okp and okv) else 1)

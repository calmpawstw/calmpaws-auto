#!/usr/bin/env python3
"""
在 upload_youtube.py 的 get_youtube_client 裡加一道雲端防呆。

問題：
  取不到有效 token 時，程式會走 InstalledAppFlow.run_local_server()，
  那是「開瀏覽器讓人按同意」的互動式流程。在 GitHub Actions 上
  沒有人會去按，於是整個 job 會靜靜卡住，直到 180 分鐘逾時才死，
  白白燒掉 Actions 額度，而且日誌上看不出在等什麼。

  這次就是踩到這個：token 路徑不匹配 → 找不到 token → 進互動流程
  → client_secrets 是空字串 → FileNotFoundError。

修法：
  在進入互動流程前，若偵測到 GITHUB_ACTIONS 環境變數，
  直接丟出說明清楚的錯誤，不要卡住。

用法：
  python cp_patch_ytauth.py           # 套用
  python cp_patch_ytauth.py --check   # 只檢查
"""
import argparse
import ast
import py_compile
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
TARGET = BASE / "upload_youtube.py"
MARKER = "_cp_no_interactive_in_ci"

GUARD = '''\
            # {marker}：雲端沒有人可以按「同意授權」，
            # 讓它卡在 run_local_server 直到 workflow 逾時毫無意義。
            if os.environ.get("GITHUB_ACTIONS"):
                raise RuntimeError(
                    "在雲端取不到有效的 YouTube token，且無法執行互動式授權。\\n"
                    "請確認 GitHub Secret 的 YT_TOKEN_JSON 內容正確且未過期，\\n"
                    "並確認 cloud_publish.py --build-config 有把它寫到 "
                    f"{TOKEN_FILE}。"
                )
'''.replace("{marker}", MARKER)


def find_flow_line(src: str):
    """找 get_youtube_client 裡 InstalledAppFlow.from_client_secrets_file 那一行"""
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "get_youtube_client"):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "from_client_secrets_file"):
                return node.lineno
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"❌ 找不到 {TARGET}")
        return 1

    src = TARGET.read_text(encoding="utf-8")

    if MARKER in src:
        print("✅ 已經有雲端防呆，不需重複修改")
        return 0

    try:
        lineno = find_flow_line(src)
    except SyntaxError as e:
        print(f"❌ upload_youtube.py 語法有問題，不敢動：{e}")
        return 1

    if lineno is None:
        print("❌ 找不到 InstalledAppFlow 呼叫，結構跟預期不同")
        return 1

    if args.check:
        print(f"⚠️  需要修改：第 {lineno} 行前要加雲端防呆")
        return 0

    lines = src.splitlines(keepends=True)
    # 用該行的實際縮排，避免硬編縮排在被 patch 過的檔案上對不齊
    target_line = lines[lineno - 1]
    indent = target_line[:len(target_line) - len(target_line.lstrip())]
    guard = "".join(indent + ln[12:] if ln.strip() else ln
                    for ln in GUARD.splitlines(keepends=True))

    lines.insert(lineno - 1, guard)
    new_src = "".join(lines)

    if "import os" not in new_src.split("def ")[0]:
        new_src = "import os\n" + new_src

    backup = TARGET.with_suffix(".py.bak_ytauth")
    shutil.copyfile(TARGET, backup)
    TARGET.write_text(new_src, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copyfile(backup, TARGET)
        print(f"❌ 修改後語法錯誤，已還原：{e}")
        return 1

    if MARKER not in TARGET.read_text(encoding="utf-8"):
        shutil.copyfile(backup, TARGET)
        print("❌ 驗證失敗，已還原")
        return 1

    print(f"✅ 已在第 {lineno} 行前加入雲端防呆，備份：{backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

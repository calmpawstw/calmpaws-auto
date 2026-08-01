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
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
TARGET = BASE / "upload_youtube.py"
MARKER = "_cp_no_interactive_in_ci"
SCOPE_MARKER = "_cp_use_token_own_scopes"

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


def find_scopes_call(src: str):
    """
    找 Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES) 這個呼叫。
    回傳 node，沒有就 None。

    多傳 SCOPES 會蓋掉 token 實際被授權的 scope。續期時送出的 scope
    若不是原始授權的子集，Google 會回 invalid_scope: Bad Request。
    """
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_authorized_user_file"):
            return node
    return None


def find_scopes_arg(src: str):
    """回傳 (行號, 是否有多傳 SCOPES)"""
    node = find_scopes_call(src)
    if node is None:
        return None, False
    return node.lineno, len(node.args) >= 2


def patch_scopes(src: str):
    """
    把 from_authorized_user_file 的第二個參數（SCOPES）拿掉。

    用 AST 的 col_offset 精確定位而不是字串比對，因為第一個參數
    通常是 str(TOKEN_FILE)，本身含括號，正則很容易咬錯範圍。
    col_offset 是 UTF-8 位元組偏移量，所以先轉 bytes 再切。
    """
    node = find_scopes_call(src)
    if node is None or len(node.args) < 2:
        return src, False
    # 只處理單行呼叫，跨行的情況保守跳過
    if node.lineno != getattr(node, "end_lineno", node.lineno):
        return src, False

    lines = src.splitlines(keepends=True)
    line = lines[node.lineno - 1]
    raw = line.encode("utf-8")

    arg0_end = node.args[0].end_col_offset   # 第一個參數結束處
    call_end = node.end_col_offset           # 整個呼叫的右括號之後

    # 保留到第一個參數為止，補上右括號，再接原本呼叫之後的內容
    new_line = (raw[:arg0_end].decode("utf-8") + ")"
                + raw[call_end:].decode("utf-8"))

    indent = line[:len(line) - len(line.lstrip())]
    comment = (f"{indent}# {SCOPE_MARKER}：不要傳 SCOPES。傳了會蓋掉 token 實際被\n"
               f"{indent}# 授權的 scope，續期時送出非原始授權子集的 scope，\n"
               f"{indent}# Google 會回 invalid_scope: Bad Request。\n")
    lines[node.lineno - 1] = comment + new_line
    return "".join(lines), True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"❌ 找不到 {TARGET}")
        return 1

    src = TARGET.read_text(encoding="utf-8")
    original = src

    try:
        ast.parse(src)
    except SyntaxError as e:
        print(f"❌ upload_youtube.py 語法有問題，不敢動：{e}")
        return 1

    # ── 檢查模式 ──────────────────────────────────────────
    if args.check:
        need = []
        if MARKER not in src:
            ln = find_flow_line(src)
            if ln:
                need.append(f"第 {ln} 行前要加雲端防呆")
        _, has_scopes = find_scopes_arg(src)
        if SCOPE_MARKER not in src and has_scopes:
            need.append("from_authorized_user_file 多傳了 SCOPES，要拿掉")
        if need:
            for n in need:
                print(f"⚠️  {n}")
        else:
            print("✅ 兩項修正都已套用")
        return 0

    changed = []

    # ── 修正一：雲端不進互動式授權 ────────────────────────
    if MARKER in src:
        print("✅ 雲端防呆：已存在")
    else:
        lineno = find_flow_line(src)
        if lineno is None:
            print("⚠️  找不到 InstalledAppFlow 呼叫，跳過雲端防呆")
        else:
            lines = src.splitlines(keepends=True)
            target_line = lines[lineno - 1]
            indent = target_line[:len(target_line) - len(target_line.lstrip())]
            guard = "".join(indent + ln[12:] if ln.strip() else ln
                            for ln in GUARD.splitlines(keepends=True))
            lines.insert(lineno - 1, guard)
            src = "".join(lines)
            if "import os" not in src.split("def ")[0]:
                src = "import os\n" + src
            changed.append(f"雲端防呆（第 {lineno} 行）")

    # ── 修正二：續期不要多傳 SCOPES ───────────────────────
    if SCOPE_MARKER in src:
        print("✅ SCOPES 修正：已存在")
    else:
        src, ok = patch_scopes(src)
        if ok:
            changed.append("from_authorized_user_file 移除 SCOPES")
        else:
            _, has_scopes = find_scopes_arg(src)
            if has_scopes:
                print("⚠️  SCOPES 參數存在但無法自動移除（可能是跨行寫法）")

    if not changed:
        print("沒有需要修改的地方")
        return 0

    backup = TARGET.with_suffix(".py.bak_ytauth")
    shutil.copyfile(TARGET, backup)
    TARGET.write_text(src, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copyfile(backup, TARGET)
        print(f"❌ 修改後語法錯誤，已還原：{e}")
        return 1

    final = TARGET.read_text(encoding="utf-8")
    if "雲端防呆" in " ".join(changed) and MARKER not in final:
        shutil.copyfile(backup, TARGET)
        print("❌ 驗證失敗，已還原")
        return 1

    for c in changed:
        print(f"✅ 已套用：{c}")
    print(f"   備份：{backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

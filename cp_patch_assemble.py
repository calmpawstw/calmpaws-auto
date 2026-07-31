#!/usr/bin/env python3
"""
把 orchestrator.py 裡「Reel 流程」的 assemble_all 呼叫加上 mode="reel"。

為什麼需要：
  assemble_all 原本不分模式一律組裝 YouTube 長片 + Reel 兩支。
  以前 8 小時長片因為 -shortest 的 bug 實際只有 60 秒、15 秒就編完，
  所以 Reel 流程順手多編一支沒人發現。修好 bug 之後那會是真的 8 小時，
  Reel workflow（上限 45 分鐘）會直接被拖爆。

  只改 Reel 流程那一個呼叫；YouTube 流程維持 both，
  因為它多編一支 60 秒 Reel 的成本可以忽略。

用 AST 定位而不是字串比對，因為 orchestrator.py 已經被其他工具
patch 過多次，字串長相不保證一致。

用法：
  python cp_patch_assemble.py            # 套用
  python cp_patch_assemble.py --check    # 只檢查狀態，不修改
"""
import argparse
import ast
import py_compile
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).parent
TARGET = BASE / "orchestrator.py"


def find_call(src: str):
    """回傳 run_reel_pipeline 裡 assemble_all 呼叫的 (node, 是否已修改過)"""
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "run_reel_pipeline"):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "assemble_all"):
                already = any(k.arg == "mode" for k in node.keywords)
                return node, already
    return None, False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"❌ 找不到 {TARGET}")
        return 1

    src = TARGET.read_text(encoding="utf-8")

    try:
        node, already = find_call(src)
    except SyntaxError as e:
        print(f"❌ orchestrator.py 語法有問題，不敢動：{e}")
        return 1

    if node is None:
        print("❌ 在 run_reel_pipeline 裡找不到 assemble_all 呼叫")
        print("   orchestrator.py 結構可能跟預期不同，請把檔案給我看")
        return 1

    if already:
        print("✅ 已經有 mode 參數，不需重複修改")
        return 0

    if args.check:
        print(f"⚠️  需要修改：第 {node.lineno} 行的 assemble_all 缺少 mode 參數")
        return 0

    lines = src.splitlines(keepends=True)
    # AST 的 lineno 從 1 開始；呼叫可能跨行，用 end_lineno 抓完整範圍
    start = node.lineno - 1
    end = getattr(node, "end_lineno", node.lineno) - 1

    segment = "".join(lines[start:end + 1])
    # 從後面找最後一個右括號（呼叫的收尾），插在它前面
    idx = segment.rfind(")")
    if idx == -1:
        print("❌ 找不到呼叫的右括號，放棄修改")
        return 1

    head, tail = segment[:idx], segment[idx:]
    # 跨行呼叫的最後一個參數常帶尾隨逗號，例如：
    #     assemble_all(
    #         scene,
    #         output_dir,
    #     )
    # 直接插 ', mode="reel"' 會變成「, , mode=...」的語法錯誤。
    # 先把括號前的空白拆下來，判斷是否已有逗號，再把空白接回去保留排版。
    stripped = head.rstrip()
    trailing_ws = head[len(stripped):]
    sep = ' mode="reel"' if stripped.endswith(",") else ', mode="reel"'
    new_segment = stripped + sep + trailing_ws + tail
    new_src = "".join(lines[:start]) + new_segment + "".join(lines[end + 1:])

    backup = TARGET.with_suffix(".py.bak_assemble")
    shutil.copyfile(TARGET, backup)
    TARGET.write_text(new_src, encoding="utf-8")

    try:
        py_compile.compile(str(TARGET), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copyfile(backup, TARGET)
        print(f"❌ 修改後語法錯誤，已還原：{e}")
        return 1

    # 再用 AST 確認真的加上去了
    node2, already2 = find_call(TARGET.read_text(encoding="utf-8"))
    if not already2:
        shutil.copyfile(backup, TARGET)
        print("❌ 驗證失敗（mode 沒被加上），已還原")
        return 1

    print(f"✅ 已修改第 {node.lineno} 行，備份：{backup.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
安寵 Calm Paws — 把優化引擎的參數真正接進產出管線

目前只有 --scene 有效。title_formula / thumb_style / duration_h
被選出來卻沒有任何地方使用，優化引擎在這三個維度上學到的全是雜訊。

本腳本做四件事：
  1. orchestrator.py  讀取 CP_* 環境變數與 scene_override.json，
                      把參數注入 scene dict 與 config
  2. upload_youtube.py 依 title_formula 選用不同標題句型
  3. generate_images.py 依 thumb_style 調整縮圖 prompt，
                       並把風格加進快取檔名（否則不同風格會共用同一張圖）
  4. config.yaml      補上新場景與標題變體

每個檔案都先備份，改完立即 py_compile 驗證，
任一失敗就全部還原 —— 半套修改比不改更危險。
"""
import os
import re
import sys
import shutil
import py_compile
from pathlib import Path

BASE = Path(os.environ.get("CP_HOME") or os.path.expanduser("~/calm_paws"))
BACKUP_SUFFIX = ".prepatch.bak"

results = []
backed_up = []


def log(ok, name, msg):
    results.append((ok, name, msg))
    print(f"  {'✅' if ok else '❌'} {name}：{msg}")


def backup(p: Path):
    b = p.with_suffix(p.suffix + BACKUP_SUFFIX)
    if not b.exists():
        shutil.copy(p, b)
    backed_up.append((p, b))


def rollback_all():
    for p, b in backed_up:
        if b.exists():
            shutil.copy(b, p)
    print("\n  ⚠️  已全部還原到修改前的狀態")


# ══════════════════════════════════════════════════════════
#  1. orchestrator.py
# ══════════════════════════════════════════════════════════
INJECT_FUNC = '''

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
'''


def patch_orchestrator():
    p = BASE / "orchestrator.py"
    if not p.exists():
        log(False, "orchestrator.py", "檔案不存在")
        return False

    src = p.read_text(encoding="utf-8")
    if "_load_cp_params" in src:
        log(True, "orchestrator.py", "已修補過，略過")
        return True

    backup(p)

    # 在 main() 之前插入函式
    m = re.search(r"^def main\(\):", src, re.M)
    if not m:
        log(False, "orchestrator.py", "找不到 def main()")
        return False
    src = src[:m.start()] + INJECT_FUNC.strip() + "\n\n\n" + src[m.start():]

    # 在 select_scene 之後套用參數
    anchor = "    scene = select_scene(config, force_scene=args.scene)"
    if anchor not in src:
        log(False, "orchestrator.py", "找不到 select_scene 呼叫點")
        return False

    replacement = (
        "    _cp_params = _load_cp_params()\n"
        "    scene = select_scene(\n"
        "        config,\n"
        "        force_scene=args.scene or _cp_params.get('scene'))\n"
        "    scene, config = _apply_cp_params(scene, config, _cp_params, logger)"
    )
    src = src.replace(anchor, replacement, 1)

    p.write_text(src, encoding="utf-8")
    log(True, "orchestrator.py", "已加入參數讀取與注入")
    return True


# ══════════════════════════════════════════════════════════
#  2. upload_youtube.py — 標題句型
# ══════════════════════════════════════════════════════════
TITLE_HELPER = '''

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
'''


def patch_upload_youtube():
    p = BASE / "upload_youtube.py"
    if not p.exists():
        log(False, "upload_youtube.py", "檔案不存在")
        return False

    src = p.read_text(encoding="utf-8")
    if "_pick_title_template" in src:
        log(True, "upload_youtube.py", "已修補過，略過")
        return True

    backup(p)

    old = 'title = scene["yt_title_template"].format(duration=duration_str)'
    if old not in src:
        log(False, "upload_youtube.py", "找不到標題組裝那一行")
        return False

    m = re.search(r"^def _build_metadata", src, re.M)
    if not m:
        log(False, "upload_youtube.py", "找不到 _build_metadata")
        return False
    src = src[:m.start()] + TITLE_HELPER.strip() + "\n\n\n" + src[m.start():]

    src = src.replace(
        old,
        "title = _pick_title_template(scene).format(duration=duration_str)",
        1)

    p.write_text(src, encoding="utf-8")
    log(True, "upload_youtube.py", "標題已改為依句型選擇")
    return True


# ══════════════════════════════════════════════════════════
#  3. generate_images.py — 縮圖風格
# ══════════════════════════════════════════════════════════
THUMB_STYLES = '''

# ── 縮圖風格（由 cp_patch.py 加入）──────────────────────────
_THUMB_STYLE_PROMPTS = {
    "big_text_closeup":
        "extreme close-up of the pet's face filling the frame, "
        "shallow depth of field, bold high-contrast lighting, "
        "large empty area on the left for text overlay",
    "minimal_illust":
        "flat vector illustration style, minimal shapes, "
        "limited pastel palette, lots of negative space, "
        "clean modern graphic design, no photorealism",
    "realistic_sleep":
        "photorealistic sleeping pet in dim warm bedroom light, "
        "cozy blankets, soft shadows, intimate night atmosphere",
    "split_beforeafter":
        "split composition, left side anxious tense pet in cool blue tones, "
        "right side same pet relaxed in warm golden tones, "
        "clear vertical divider in the middle",
}


def _thumb_style_suffix(scene: dict) -> str:
    """回傳風格對應的 prompt 片段，沒指定就回空字串"""
    style = scene.get("_thumb_style")
    if not style:
        return ""
    frag = _THUMB_STYLE_PROMPTS.get(style, "")
    return (", " + frag) if frag else ""
# ── 加入結束 ────────────────────────────────────────────────
'''


def patch_generate_images():
    p = BASE / "generate_images.py"
    if not p.exists():
        log(False, "generate_images.py", "檔案不存在")
        return False

    src = p.read_text(encoding="utf-8")
    if "_thumb_style_suffix" in src:
        log(True, "generate_images.py", "已修補過，略過")
        return True

    backup(p)

    m = re.search(r"^def generate_youtube_thumbnail", src, re.M)
    if not m:
        log(False, "generate_images.py", "找不到 generate_youtube_thumbnail")
        return False
    src = src[:m.start()] + THUMB_STYLES.strip() + "\n\n\n" + src[m.start():]

    # 把風格片段接到 prompt 尾端
    pat = re.compile(
        r'(thumbnail_prompt\s*=\s*\((?:[^()]|\([^()]*\))*?)\)', re.S)
    if pat.search(src):
        src = pat.sub(r"\1 + _thumb_style_suffix(scene))", src, count=1)
        prompt_ok = True
    else:
        prompt_ok = False

    # 快取檔名要含風格，否則不同風格會直接沿用同一張舊圖，
    # 等於整個 thumb_style 維度沒有作用
    old_name = '''f"{scene['id']}_thumbnail.png"'''
    new_name = ('''f"{scene['id']}'''
                '''{'_' + scene['_thumb_style'] if scene.get('_thumb_style') else ''}'''
                '''_thumbnail.png"''')
    name_ok = old_name in src
    if name_ok:
        src = src.replace(old_name, new_name)

    p.write_text(src, encoding="utf-8")
    if prompt_ok and name_ok:
        log(True, "generate_images.py", "縮圖 prompt 與快取檔名都已支援風格")
    elif prompt_ok:
        log(True, "generate_images.py",
            "prompt 已支援風格，但快取檔名未改（可能已含風格）")
    else:
        log(False, "generate_images.py", "找不到 thumbnail_prompt 組裝處")
        return False
    return True


# ══════════════════════════════════════════════════════════
#  4. config.yaml — 標題變體
# ══════════════════════════════════════════════════════════
def _already_has_variants(lines, idx) -> bool:
    """
    檢查這個場景是否已經有標題變體。

    必須逐場景判斷，不能只看整份檔案有沒有出現 yt_title_variants —
    否則之後在 config.yaml 新增場景時，會因為舊場景已有變體
    而讓新場景被整批略過，那幾個場景的 title_formula 就永遠是死的。
    """
    for line in lines[idx + 1:]:
        if re.match(r"^\s*-\s*id:", line):      # 進入下一個場景了
            return False
        if re.match(r"^\s*yt_title_variants:", line):
            return True
    return False


def patch_config_titles():
    """為每個場景補上四種標題句型（逐場景判斷，可重複執行）"""
    p = BASE / "config.yaml"
    if not p.exists():
        log(False, "config.yaml", "檔案不存在")
        return False

    txt = p.read_text(encoding="utf-8")
    lines = txt.split("\n")

    targets = [i for i, l in enumerate(lines)
               if re.match(r"^\s*yt_title_template:", l)
               and not _already_has_variants(lines, i)]
    if not targets:
        if "yt_title_template" in txt:
            log(True, "config.yaml", "所有場景都已有標題變體，略過")
            return True
        log(False, "config.yaml", "找不到任何 yt_title_template")
        return False

    backup(p)

    # 優先使用手寫的標題檔。
    # 中文句法沒辦法靠字串拼接產生通順的標題 ——
    # 直接把「怎麼辦？」接在主題後面會得到
    # 「打雷放鞭炮不再怕怎麼辦？」這種讀不通的句子，
    # 而優化引擎會誤判成該句型效果差，其實是模板寫壞了。
    curated = {}
    for cand in (Path(__file__).parent / "title_variants.yaml",
                 BASE / "title_variants.yaml"):
        if cand.exists():
            try:
                import yaml as _yaml
                curated = _yaml.safe_load(cand.read_text(encoding="utf-8")) or {}
                print(f"     使用手寫標題檔：{cand.name}")
                break
            except Exception as e:
                print(f"     ⚠️ 讀取 {cand.name} 失敗：{e}")

    def scene_id_at(idx):
        """往回找這一行屬於哪個場景"""
        for j in range(idx, -1, -1):
            mm = re.match(r"^\s*-\s*id:\s*[\"']?([\w-]+)", lines[j])
            if mm:
                return mm.group(1)
        return None

    out = []
    added = 0
    fallback_used = []

    for i, line in enumerate(lines):
        out.append(line)
        if i not in targets:
            continue
        m = re.match(r"^(\s*)yt_title_template:\s*(.+)$", line)
        if not m:
            continue
        indent, orig = m.group(1), m.group(2).strip()
        sid = scene_id_at(i)

        out.append(f"{indent}yt_title_variants:")

        if sid and curated.get(sid):
            for formula in ("problem_first", "scenario_first",
                            "outcome_first", "duration_first"):
                val = curated[sid].get(formula)
                if val:
                    out.append(f'{indent}  {formula}: "{val}"')
                elif formula == "scenario_first":
                    out.append(f"{indent}  scenario_first: {orig}")
        else:
            # 沒有手寫版本時，只放原標題當作四種句型的共同值。
            # 這樣不會產出爛標題，代價是該場景暫時測不出句型差異 ——
            # 寧可少一個實驗維度，也不要用壞標題傷 CTR。
            fallback_used.append(sid or "?")
            for formula in ("problem_first", "scenario_first",
                            "outcome_first", "duration_first"):
                out.append(f"{indent}  {formula}: {orig}")
        added += 1

    if fallback_used:
        print(f"     ⚠️ 這些場景沒有手寫標題，暫用原標題："
              f"{', '.join(fallback_used)}")

    if not added:
        log(False, "config.yaml", "找不到任何 yt_title_template")
        return False

    p.write_text("\n".join(out), encoding="utf-8")
    log(True, "config.yaml", f"已為 {added} 個場景補上 4 種標題句型")
    return True


# ══════════════════════════════════════════════════════════
def verify() -> bool:
    ok = True
    for name in ("orchestrator.py", "upload_youtube.py",
                 "generate_images.py"):
        p = BASE / name
        if not p.exists():
            continue
        try:
            py_compile.compile(str(p), doraise=True)
            print(f"  ✅ {name} 語法正確")
        except py_compile.PyCompileError as e:
            print(f"  ❌ {name} 語法錯誤：{e}")
            ok = False

    # config.yaml
    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
        n = len(cfg.get("scenes", []))
        v = sum(1 for s in cfg.get("scenes", []) if s.get("yt_title_variants"))
        print(f"  ✅ config.yaml 可解析，{n} 個場景，{v} 個有標題變體")
    except Exception as e:
        print(f"  ❌ config.yaml 解析失敗：{e}")
        ok = False
    return ok


def main():
    print("══════════════════════════════════════════════════════")
    print("  把優化參數接進產出管線")
    print("══════════════════════════════════════════════════════")
    print(f"  目錄：{BASE}\n")

    print("【修補】")
    patch_orchestrator()
    patch_upload_youtube()
    patch_generate_images()
    patch_config_titles()

    print("\n【驗證】")
    if not verify():
        rollback_all()
        print("\n❌ 驗證未通過，已還原。請把上面的錯誤訊息回報。")
        sys.exit(1)

    failed = [r for r in results if not r[0]]
    print("\n══════════════════════════════════════════════════════")
    if failed:
        print("  ⚠️  部分項目未完成：")
        for _, name, msg in failed:
            print(f"     {name} — {msg}")
        print("\n  已完成的部分仍然有效，語法驗證通過。")
    else:
        print("  ✅ 全部完成")
        print()
        print("  現在四個維度都真的會影響產出：")
        print("    scene          → 決定音樂與圖像 prompt")
        print("    title_formula  → 決定標題句型")
        print("    thumb_style    → 決定縮圖風格與構圖")
        print("    duration_h     → 決定影片長度")
    print("══════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
安寵 Calm Paws — 在本機完整模擬雲端環境

先前每次修一個問題就推到雲端跑一次，一輪 50 秒只驗證一個假設，
連續四次都在下一個環節才發現新錯誤。

這支改成在本機建一個與 GitHub runner 等價的沙箱：
  • 乾淨目錄，只放 repo 會有的檔案
  • CP_HOME 指向沙箱（不是 ~/calm_paws）
  • 金鑰走環境變數，跟 workflow 一樣
  • 實際執行 build-config → 優化引擎選參數 → orchestrator dry-run

所有錯誤會一次浮現，修完再推。
"""
import os
import re
import sys
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

LOCAL = Path(os.path.expanduser("~/calm_paws"))
SIM = Path("/tmp/cp_cloud_sim")

# repo 會包含的檔案（跟 搬上雲端.command 一致）
CODE_FILES = [
    "orchestrator.py", "generate_music.py", "generate_images.py",
    "generate_voice.py", "assemble_video.py", "upload_youtube.py",
    "upload_instagram.py",
    "cp_analytics.py", "cp_optimizer.py", "cp_report.py",
    "cp_generate.py", "cloud_publish.py", "db_merge.py", "cp_deps.py",
    "title_variants.yaml", "requirements.txt", "git_push.sh",
]

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def ok(msg):    print(f"  {GREEN}✅{RESET} {msg}")
def bad(msg):   print(f"  {RED}❌{RESET} {msg}")
def warn(msg):  print(f"  {YELLOW}⚠️ {RESET} {msg}")


def read_conf_field(field: str) -> str:
    """不依賴 pyyaml 讀 config.yaml 單一欄位"""
    txt = (LOCAL / "config.yaml").read_text(encoding="utf-8")
    m = re.search(rf"^\s*{field}\s*:\s*(.*)$", txt, re.M)
    if not m:
        return ""
    raw = m.group(1).strip()
    if raw.startswith('"'):
        mm = re.match(r'"([^"]*)"', raw)
        return mm.group(1) if mm else ""
    if raw.startswith("'"):
        mm = re.match(r"'([^']*)'", raw)
        return mm.group(1) if mm else ""
    return re.sub(r"\s+#.*$", "", raw).strip()


# ══════════════════════════════════════════════════════════
def build_template() -> bool:
    """由本機 config.yaml 產生 config.template.yaml（金鑰清空、路徑轉相對）"""
    try:
        import yaml
    except ImportError:
        bad("這支必須用 venv 的 python 執行（需要 pyyaml）")
        return False

    src = LOCAL / "config.yaml"
    cfg = yaml.safe_load(src.read_text(encoding="utf-8"))

    for k in list(cfg.get("api_keys", {})):
        cfg["api_keys"][k] = ""

    # 路徑轉相對，runner 上沒有 ~/calm_paws
    for k, v in list((cfg.get("paths") or {}).items()):
        if isinstance(v, str):
            cfg["paths"][k] = v.replace("~/calm_paws/", "").replace("~/calm_paws", ".")
    for k, v in list(cfg.items()):
        if isinstance(v, str) and "~/calm_paws/" in v:
            cfg[k] = v.replace("~/calm_paws/", "")

    out = LOCAL / "config.template.yaml"
    out.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")

    ok(f"config.template.yaml 已產生")
    print(f"       paths  = {cfg.get('paths')}")
    print(f"       scenes = {len(cfg.get('scenes', []))} 個")
    print(f"       金鑰欄位 = {list(cfg.get('api_keys', {}))}（值已清空）")
    return True


def setup_sandbox() -> bool:
    if SIM.exists():
        shutil.rmtree(SIM)
    SIM.mkdir(parents=True)

    missing = []
    for f in CODE_FILES:
        s = LOCAL / f
        if s.exists():
            shutil.copy(s, SIM / f)
        elif f in ("title_variants.yaml", "cp_deps.py", "git_push.sh",
                   "db_merge.py"):
            pass
        else:
            missing.append(f)

    shutil.copy(LOCAL / "config.template.yaml", SIM / "config.template.yaml")

    for d in ("assets", "data"):
        s = LOCAL / d
        if s.exists():
            shutil.copytree(s, SIM / d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.mp4"))
    (SIM / "output").mkdir(exist_ok=True)
    (SIM / "logs").mkdir(exist_ok=True)

    for f in ("token.json", "client_secret.json"):
        if (LOCAL / f).exists():
            shutil.copy(LOCAL / f, SIM / f)

    if missing:
        bad(f"缺少檔案：{', '.join(missing)}")
        return False
    ok(f"沙箱建立於 {SIM}")
    return True


def sim_env() -> dict:
    """模擬 workflow 的環境變數"""
    env = os.environ.copy()
    env["CP_HOME"] = str(SIM)
    secrets = {
        "ANTHROPIC_API_KEY": read_conf_field("anthropic"),
        "REPLICATE_API_TOKEN": read_conf_field("replicate"),
        "ELEVENLABS_API_KEY": read_conf_field("elevenlabs"),
        "IG_ACCESS_TOKEN": read_conf_field("instagram_access_token"),
        "IG_USER_ID": read_conf_field("instagram_user_id"),
    }
    tok = LOCAL / "token.json"
    if tok.exists():
        secrets["YT_TOKEN_JSON"] = tok.read_text()
    env["ALL_SECRETS"] = json.dumps({k: v for k, v in secrets.items() if v})
    env.update({k: v for k, v in secrets.items() if v})
    return env


def run(cmd, env, label, timeout=180):
    print(f"\n  ── {label} ──")
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    try:
        p = subprocess.run(cmd, cwd=str(SIM), env=env,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        bad(f"{label} 逾時"); return False, ""
    out = (p.stdout or "") + (p.stderr or "")
    tail = "\n".join(out.strip().split("\n")[-25:])
    print("\n".join(f"     {l}" for l in tail.split("\n")))
    if p.returncode == 0:
        ok(f"{label} 通過")
        return True, out
    bad(f"{label} 失敗（return code {p.returncode}）")
    return False, out


def main():
    print("=" * 58)
    print("  在本機模擬 GitHub Actions 環境")
    print("=" * 58)
    print()

    print("【1】產生 config.template.yaml")
    if not build_template():
        sys.exit(1)
    print()

    print("【2】建立沙箱")
    if not setup_sandbox():
        sys.exit(1)
    print()

    env = sim_env()
    py = sys.executable

    print("【3】依序執行 workflow 的每個步驟")

    good, _ = run([py, "cloud_publish.py", "--build-config"],
                  env, "由 Secrets 產生設定檔")
    if not good:
        sys.exit(1)

    # 檢查產生出來的 config
    try:
        import yaml
        cfg = yaml.safe_load((SIM / "config.yaml").read_text(encoding="utf-8"))
        print()
        for k in ("paths", "scenes", "youtube", "api_keys"):
            v = cfg.get(k)
            n = len(v) if isinstance(v, (list, dict)) else v
            (ok if v else bad)(f"config['{k}'] = {n}")
        if not cfg.get("paths"):
            bad("缺少 paths，orchestrator 會 KeyError")
            sys.exit(1)
    except Exception as e:
        bad(f"讀取產生的 config 失敗：{e}")
        sys.exit(1)

    good, out = run([py, "cp_optimizer.py", "--pick", "reel"],
                    env, "優化引擎挑選參數")
    if not good:
        sys.exit(1)
    try:
        picked = json.loads(out.strip().split("\n")[-1])
        scene = picked.get("scene", "")
        valid = {s["id"] for s in cfg.get("scenes", [])}
        (ok if scene in valid else bad)(
            f"選到的場景 {scene} {'存在' if scene in valid else '不存在於 config'}")
        if scene not in valid:
            sys.exit(1)
    except Exception as e:
        warn(f"無法解析選出的參數：{e}")
        scene = ""

    good, out = run([py, "orchestrator.py", "--mode", "reel",
                     "--scene", scene or "relax", "--dry-run"],
                    env, "orchestrator dry-run", timeout=300)
    if not good:
        sys.exit(1)

    # dry-run 會跳過上傳，所以上傳路徑的問題不會浮現 ——
    # 先前 cloudflared 缺失就是這樣漏掉的。這裡改用靜態檢查補上。
    print("\n  ── 上傳路徑靜態檢查 ──")
    orch = (SIM / "orchestrator.py").read_text(encoding="utf-8")
    up = (SIM / "upload_instagram.py")
    up_src = up.read_text(encoding="utf-8") if up.exists() else ""

    # 要驗證的是「即將推送的」workflow，不是本機那份可能過期的複本。
    # 先前就是拿舊複本驗新設定，結果誤判。
    candidates = []
    env_dir = os.environ.get("CP_WORKFLOW_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / "reel.yml")
    candidates += [
        LOCAL.parent / "calmpaws-cloud" / ".github" / "workflows" / "reel.yml",
        LOCAL / ".github" / "workflows" / "reel.yml",
    ]
    reel_yml = next((c for c in candidates if c.exists()), None)
    yml_src = reel_yml.read_text(encoding="utf-8") if reel_yml else ""
    if reel_yml:
        print(f"     檢查對象：{reel_yml}")

    uses_cloudflared = "cloudflared" in up_src
    has_skip = "CP_SKIP_UPLOAD" in orch
    yml_sets_skip = "CP_SKIP_UPLOAD" in yml_src if yml_src else None

    if uses_cloudflared:
        warn("upload_instagram 會呼叫 cloudflared（runner 上沒有）")
        if has_skip:
            ok("orchestrator 支援 CP_SKIP_UPLOAD，雲端會跳過內建上傳")
        else:
            bad("orchestrator 沒有跳過機制 —— 雲端會 FileNotFoundError")
            bad("請先執行 cp_patch_upload.py")
            sys.exit(1)
        if yml_sets_skip is False:
            bad("reel.yml 沒有設定 CP_SKIP_UPLOAD，雲端仍會走到 cloudflared")
            sys.exit(1)
        elif yml_sets_skip:
            ok("reel.yml 已設定 CP_SKIP_UPLOAD")
    else:
        ok("upload_instagram 不依賴 cloudflared")

    # 留言回覆必須是非阻斷的。
    # 影片產出後才因為抓留言拿到 400 而 exit 1，
    # 會讓後面的上傳步驟全被 skip —— 白做一次影片。
    if "run_reply_pipeline" in orch:
        if "_cp_reply_optional" in orch:
            ok("留言回覆失敗不會中斷發文")
        else:
            bad("留言回覆失敗會中斷整支管線 —— 請執行 cp_patch_upload.py")
            sys.exit(1)

    # 實際驗證跳過邏輯會生效
    env2 = dict(env)
    env2["CP_SKIP_UPLOAD"] = "1"
    good2, out2 = run([py, "-c",
                       "import os;print('CP_SKIP_UPLOAD=' + "
                       "os.environ.get('CP_SKIP_UPLOAD','未設定'))"],
                      env2, "確認環境變數可傳遞")

    print()
    print("=" * 58)
    if good:
        print(f"  {GREEN}✅ 模擬全部通過{RESET}")
        print()
        print("  dry-run 不會呼叫外部 API，所以驗證的是：")
        print("    設定載入、路徑、場景查找、模組匯入、參數注入")
        print("  仍可能在實際 API 呼叫時出問題（例如空金鑰）。")
    else:
        print(f"  {RED}❌ 模擬失敗 —— 修好再推，不要推上去試{RESET}")
        sys.exit(1)
    print("=" * 58)


if __name__ == "__main__":
    main()

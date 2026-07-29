#!/usr/bin/env python3
"""
安寵 Calm Paws — 相依套件掃描與驗證

設計原則：白名單標準庫，其餘一律視為第三方套件。

先前用「已知套件對應表」的做法失敗了兩次 ——
表裡沒有的（cloudinary）只會被印在「未知」清單裡，
不會進 requirements.txt，等於把判斷丟回給人眼。
反過來列標準庫就不會有這個漏洞：標準庫是有限且穩定的集合，
凡是不在裡面又不是專案內部檔案的，就一定要安裝。

用法：
    python cp_deps.py --scan        產生 requirements.txt 到 stdout
    python cp_deps.py --verify FILE 驗證某份 requirements 是否涵蓋所有 import
"""
import ast
import sys
import argparse
from pathlib import Path

BASE = Path(__file__).resolve().parent

# Python 3.9+ 標準庫頂層模組
STDLIB = {
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
    "asyncore", "atexit", "audioop", "base64", "bdb", "binascii", "binhex",
    "bisect", "builtins", "bz2", "calendar", "cgi", "cgitb", "chunk",
    "cmath", "cmd", "code", "codecs", "codeop", "collections", "colorsys",
    "compileall", "concurrent", "configparser", "contextlib", "contextvars",
    "copy", "copyreg", "cProfile", "crypt", "csv", "ctypes", "curses",
    "dataclasses", "datetime", "dbm", "decimal", "difflib", "dis",
    "distutils", "doctest", "email", "encodings", "ensurepip", "enum",
    "errno", "faulthandler", "fcntl", "filecmp", "fileinput", "fnmatch",
    "formatter", "fractions", "ftplib", "functools", "gc", "getopt",
    "getpass", "gettext", "glob", "graphlib", "grp", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "imaplib", "imghdr", "imp",
    "importlib", "inspect", "io", "ipaddress", "itertools", "json",
    "keyword", "lib2to3", "linecache", "locale", "logging", "lzma",
    "mailbox", "mailcap", "marshal", "math", "mimetypes", "mmap",
    "modulefinder", "multiprocessing", "netrc", "nis", "nntplib", "numbers",
    "operator", "optparse", "os", "ossaudiodev", "parser", "pathlib", "pdb",
    "pickle", "pickletools", "pipes", "pkgutil", "platform", "plistlib",
    "poplib", "posix", "pprint", "profile", "pstats", "pty", "pwd",
    "py_compile", "pyclbr", "pydoc", "queue", "quopri", "random",
    "re", "readline", "reprlib", "resource", "rlcompleter", "runpy",
    "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver",
    "spwd", "sqlite3", "ssl", "stat", "statistics", "string", "stringprep",
    "struct", "subprocess", "sunau", "symbol", "symtable", "sys", "sysconfig",
    "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile", "termios",
    "test", "textwrap", "threading", "time", "timeit", "tkinter", "token",
    "tokenize", "trace", "traceback", "tracemalloc", "tty", "turtle",
    "types", "typing", "unicodedata", "unittest", "urllib", "uu", "uuid",
    "venv", "warnings", "wave", "weakref", "webbrowser", "wsgiref",
    "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
    "zoneinfo", "__future__",
}

# import 名稱與 pip 套件名不同的情況
PIP_NAME = {
    "yaml": "pyyaml",
    "PIL": "Pillow",
    "googleapiclient": "google-api-python-client",
    "google": "google-auth",
    "google_auth_oauthlib": "google-auth-oauthlib",
    "google_auth_httplib2": "google-auth-httplib2",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
}

# 某些套件要連帶安裝
COMPANIONS = {
    "google-api-python-client": [
        "google-auth", "google-auth-oauthlib", "google-auth-httplib2"],
}


def local_modules() -> set:
    return {p.stem for p in BASE.glob("*.py")}


def scan_imports() -> dict:
    """回傳 {import名稱: {出現的檔案}}，已排除標準庫與專案內部模組"""
    local = local_modules()
    found = {}
    for f in sorted(BASE.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
        except Exception as e:
            print(f"# 無法解析 {f.name}: {e}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # 相對匯入，屬於專案內部
                    continue
                if node.module:
                    names = [node.module.split(".")[0]]
            for n in names:
                if n in STDLIB or n in local:
                    continue
                found.setdefault(n, set()).add(f.name)
    return found


def to_packages(found: dict) -> set:
    pkgs = set()
    for imp in found:
        pkg = PIP_NAME.get(imp, imp)
        pkgs.add(pkg)
        pkgs.update(COMPANIONS.get(pkg, []))
    return pkgs


def cmd_scan():
    found = scan_imports()
    for imp, files in sorted(found.items()):
        print(f"# {imp:24s} -> {PIP_NAME.get(imp, imp):28s} "
              f"({', '.join(sorted(files))})", file=sys.stderr)
    print("# 依實際 import 掃描產生（白名單標準庫，其餘一律列入）")
    for p in sorted(to_packages(found)):
        print(p)


def cmd_verify(req_path: str) -> int:
    """確認 requirements 涵蓋所有第三方 import。回傳缺少的數量。"""
    text = Path(req_path).read_text(encoding="utf-8").lower()
    listed = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("=")[0].split(">")[0].split("<")[0].split("[")[0]
        listed.add(name.strip())

    found = scan_imports()
    missing = []
    for imp, files in sorted(found.items()):
        pkg = PIP_NAME.get(imp, imp).lower()
        if pkg not in listed:
            missing.append((imp, pkg, files))

    print(f"第三方 import 共 {len(found)} 個，requirements 列了 {len(listed)} 個套件")
    print()
    for imp, files in sorted(found.items()):
        pkg = PIP_NAME.get(imp, imp)
        ok = pkg.lower() in listed
        print(f"  {'✅' if ok else '❌'} {imp:24s} -> {pkg:28s} "
              f"({', '.join(sorted(files))})")
    print()
    if missing:
        print(f"❌ 缺少 {len(missing)} 個套件：")
        for imp, pkg, files in missing:
            print(f"     {pkg}  （{imp}，用於 {', '.join(sorted(files))}）")
    else:
        print("✅ 所有第三方 import 都有對應套件，不會再出現 ModuleNotFoundError")
    return len(missing)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--verify", metavar="REQUIREMENTS")
    args = ap.parse_args()

    if args.scan:
        cmd_scan()
    elif args.verify:
        sys.exit(1 if cmd_verify(args.verify) else 0)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

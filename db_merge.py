#!/usr/bin/env python3
"""
安寵 Calm Paws — SQLite 併發合併

多個 workflow 可能同時寫 metrics.db。二進位檔案沒辦法用 git 合併，
用 git 的 --ours/--theirs 只會整份覆蓋，必定丟資料。
所以改在資料庫層級逐列合併。

各表的合併策略不同：
  • 有 PRIMARY KEY 的表（videos / metrics / channel_daily）
      → INSERT OR REPLACE，後寫的覆蓋，因為是同一筆的更新
  • arms（優化引擎信念）
      → 取 updated_at 較新的那筆。alpha/beta 是累積值，
        只有週報 workflow 會寫，不會真的併發，取新的即可
  • decisions / policy_flags（僅追加的日誌）
      → 完整比對去重後追加

用法：
    python db_merge.py --base remote.db --incoming local.db --out merged.db
"""
import sys
import shutil
import sqlite3
import argparse
from pathlib import Path

# 純追加的日誌表（無 PRIMARY KEY，需去重）
APPEND_ONLY = {"decisions", "policy_flags"}
# 需要比對時間戳的表
TIMESTAMPED = {"arms": "updated_at"}


def tables(conn) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchall()]


def columns(conn, table) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def pk_columns(conn, table) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            if r[5]]


def merge(base_path: Path, incoming_path: Path, out_path: Path) -> dict:
    """把 incoming 的資料併進 base，輸出到 out"""
    if not incoming_path.exists():
        shutil.copy(base_path, out_path)
        return {}
    if not base_path.exists():
        shutil.copy(incoming_path, out_path)
        return {}

    shutil.copy(base_path, out_path)
    conn = sqlite3.connect(out_path)
    conn.execute("ATTACH DATABASE ? AS inc", (str(incoming_path),))

    stats = {}
    for t in tables(conn):
        # 來源沒有這張表就跳過
        exists = conn.execute(
            "SELECT COUNT(*) FROM inc.sqlite_master "
            "WHERE type='table' AND name=?", (t,)).fetchone()[0]
        if not exists:
            continue

        cols = columns(conn, t)
        inc_cols = [r[1] for r in conn.execute(
            f"PRAGMA inc.table_info({t})").fetchall()]
        shared = [c for c in cols if c in inc_cols]
        if not shared:
            continue
        collist = ", ".join(f'"{c}"' for c in shared)

        before = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]

        try:
            if t in APPEND_ONLY:
                # 完整列比對去重
                where = " AND ".join(
                    f'IFNULL(m."{c}",\'\') = IFNULL(i."{c}",\'\')' for c in shared)
                conn.execute(f"""
                    INSERT INTO main.{t} ({collist})
                    SELECT {collist} FROM inc.{t} i
                    WHERE NOT EXISTS (
                        SELECT 1 FROM main.{t} m WHERE {where})""")

            elif t in TIMESTAMPED:
                ts = TIMESTAMPED[t]
                pks = pk_columns(conn, t)
                if pks and ts in shared:
                    join = " AND ".join(f'm."{p}" = i."{p}"' for p in pks)
                    # 只有來源比較新才覆蓋
                    conn.execute(f"""
                        INSERT OR REPLACE INTO main.{t} ({collist})
                        SELECT {", ".join(f'i."{c}"' for c in shared)}
                        FROM inc.{t} i
                        LEFT JOIN main.{t} m ON {join}
                        WHERE m."{ts}" IS NULL
                           OR IFNULL(i."{ts}",'') > IFNULL(m."{ts}",'')""")
                else:
                    conn.execute(
                        f"INSERT OR REPLACE INTO main.{t} ({collist}) "
                        f"SELECT {collist} FROM inc.{t}")

            else:
                conn.execute(
                    f"INSERT OR REPLACE INTO main.{t} ({collist}) "
                    f"SELECT {collist} FROM inc.{t}")

        except sqlite3.Error as e:
            print(f"  ⚠️  {t} 合併失敗：{e}", file=sys.stderr)
            continue

        after = conn.execute(f"SELECT COUNT(*) FROM main.{t}").fetchone()[0]
        if after != before:
            stats[t] = after - before

    conn.commit()
    conn.execute("DETACH DATABASE inc")
    conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="遠端版本")
    ap.add_argument("--incoming", required=True, help="本次執行產生的版本")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    stats = merge(Path(args.base), Path(args.incoming), Path(args.out))
    if stats:
        for t, n in stats.items():
            print(f"  {t}: +{n} 列")
    else:
        print("  無新增資料")


if __name__ == "__main__":
    main()

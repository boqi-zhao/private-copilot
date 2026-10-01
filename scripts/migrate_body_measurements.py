#!/usr/bin/env python3
"""迁移：新建 body_measurements（身体尺寸）并接管 health_metrics 里的存量围度。

背景
----
用户要求"新建一张表记录身体尺寸"，明确选择**宽表**（一圈一列），
而不是把它塞进已有的 health_metrics 长表。

但 health_metrics 里已经存了 3 条围度数据（腰围/臀围/胸围，2026-10-01），
是建表之后、用户改主意之前写进去的。**不能直接丢掉** —— 那是用户的真实测量。

所以本迁移做两件事：
  1. 建 body_measurements 表；
  2. 把 health_metrics 里 metric_key IN (腰围/臀围/胸围/...) 的行
     **按 measured_at 归并**成宽表行搬过去（同一天多项围度合并成一行）。

搬完后 health_metrics 里的围度行**会被删除** —— 否则同一个数字存在两处，
以后改一处忘一处，查询结果必然自相矛盾。这一步是幂等的：
搬过的日期再跑一次不会重复插入。

幂等性：靠 body_measurements(measured_at) 唯一索引 + INSERT OR IGNORE，
       以及"搬完就删源行"让第二次运行没有可搬的东西。

    python3 scripts/migrate_body_measurements.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "copilot.db"

# 注意：schema_migrations.version 是**全局**的，不是每个脚本各自从 1 开始。
# 已有的 1=meal_photos 可空、2=health_metrics。所以这里必须是 3，
# 否则会撞上别人的 version=1，让"是否已迁移"的判断永远为真。
VERSION = 3
DESCRIPTION = "body_measurements（身体尺寸宽表）+ 接管 health_metrics 存量围度"

DDL = """
CREATE TABLE IF NOT EXISTS body_measurements (
    id          INTEGER PRIMARY KEY,
    measured_at TEXT NOT NULL UNIQUE,
    waist       REAL,
    hip         REAL,
    chest       REAL,
    bicep       REAL,
    thigh       REAL,
    calf        REAL,
    source      TEXT NOT NULL DEFAULT 'text'
                CHECK (source IN ('text','image')),
    raw_desc    TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_body_measured ON body_measurements(measured_at);
"""

# metric_key -> body_measurements 列名
# 只搬围度；weight / bp_sys 这些**留在 health_metrics**（它们是体检指标，不是围度）
CIRCUMFERENCE_KEYS = {
    "waist": "waist",
    "hip": "hip",
    "chest": "chest",
    "bicep": "bicep",
    "thigh": "thigh",
    "calf": "calf",
}
KEY_TO_LABEL = {
    "waist": "腰围", "hip": "臀围", "chest": "胸围",
    "bicep": "大臂围", "thigh": "大腿围", "calf": "小腿围",
}


def table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def migration_applied(conn) -> bool:
    if not table_exists(conn, "schema_migrations"):
        return False
    return conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (VERSION,)
    ).fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    args = ap.parse_args()

    if not DB.exists():
        print(f"数据库不存在: {DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        already = migration_applied(conn)
        has_src = table_exists(conn, "health_metrics")

        # ---- 收集待搬数据 ----
        pending: dict[str, dict] = {}
        to_delete: list[int] = []
        if has_src:
            placeholders = ",".join("?" * len(CIRCUMFERENCE_KEYS))
            rows = conn.execute(
                f"""SELECT id, metric_key, value, measured_at
                    FROM health_metrics
                    WHERE metric_key IN ({placeholders})
                    ORDER BY measured_at""",
                tuple(CIRCUMFERENCE_KEYS),
            ).fetchall()
            for r in rows:
                col = CIRCUMFERENCE_KEYS[r["metric_key"]]
                day = str(r["measured_at"])[:10]
                pending.setdefault(day, {})[col] = r["value"]
                to_delete.append(int(r["id"]))

        if args.dry_run:
            print(f"[dry-run] 将建表 body_measurements（已存在则跳过）")
            print(f"[dry-run] 迁移记录 version={VERSION} 已存在: {already}")
            print(f"[dry-run] health_metrics 中待搬围度行: {len(to_delete)}")
            for day, cols in sorted(pending.items()):
                desc = "  ".join(f"{KEY_TO_LABEL[c]}={v}" for c, v in cols.items())
                print(f"  {day}: {desc}  -> 1 行宽表")
            print(f"[dry-run] 搬完将删除上述 {len(to_delete)} 行源数据")
            return 0

        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(DDL)

        # ---- 归并写入宽表 ----
        moved = 0
        for day, cols in sorted(pending.items()):
            names = ["measured_at"] + list(cols.keys())
            vals: list = [day] + list(cols.values())
            q = ",".join("?" * len(names))
            cur = conn.execute(
                f"INSERT OR IGNORE INTO body_measurements ({','.join(names)}) VALUES ({q})",
                vals,
            )
            if cur.rowcount:
                moved += 1
                # 只有真的插进去了才记审计，避免重复运行刷日志
                conn.execute(
                    """INSERT INTO ingest_log (skill, target_table, target_id, action, detail)
                       VALUES (?,?,?,?,?)""",
                    ("body-measure", "body_measurements", int(cur.lastrowid), "insert",
                     f'{{"migrated_from":"health_metrics","measured_at":"{day}"}}'),
                )
            else:
                # 该日期已存在（可能是重复运行），补齐空缺的列
                existing = conn.execute(
                    "SELECT * FROM body_measurements WHERE measured_at=?", (day,)
                ).fetchone()
                updates = {
                    c: v for c, v in cols.items()
                    if existing is not None and existing[c] is None
                }
                if updates:
                    sets = ",".join(f"{c}=?" for c in updates)
                    conn.execute(
                        f"UPDATE body_measurements SET {sets} WHERE measured_at=?",
                        [*updates.values(), day],
                    )

        # ---- 删除已搬走的源行 ----
        # 用 id 精确删除，不用 metric_key 条件删（避免误删将来手工补的回填数据）
        deleted = 0
        if to_delete:
            ph = ",".join("?" * len(to_delete))
            conn.execute(f"DELETE FROM health_metrics WHERE id IN ({ph})", to_delete)
            deleted = len(to_delete)

        # ---- 记录迁移 ----
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, description, sql) VALUES (?,?,?)",
            (VERSION, DESCRIPTION, "create table body_measurements; move circumferences"),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"✅ 迁移完成 version={VERSION}")
    print(f"   health_metrics 搬出围度行: {deleted}")
    print(f"   body_measurements 新增行 : {moved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""把 meal_photos.photo_path 从 NOT NULL 改成可空（纯文字记录没有照片）。

SQLite 不支持 ALTER COLUMN，必须重建表。这个脚本：
  1. 检查当前列定义，已是可空就跳过（幂等）
  2. 建新表 -> 拷数据 -> 删旧表 -> 改名 -> 重建索引
  3. 记一条 schema_migrations

用法：
    python3 scripts/migrate_meal_photo_nullable.py            # 真跑
    python3 scripts/migrate_meal_photo_nullable.py --dry-run  # 只看会做什么
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PC_DB", ROOT / "data" / "copilot.db"))

VERSION = 1
DESC = "meal_photos.photo_path 改为可空（支持纯文字餐食记录）"


def col_is_notnull(conn) -> bool | None:
    rows = conn.execute("PRAGMA table_info(meal_photos)").fetchall()
    if not rows:
        return None                      # 表不存在
    for r in rows:
        if r[1] == "photo_path":
            return bool(r[3])            # notnull 标志
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = col_is_notnull(conn)
        if cur is None:
            print("meal_photos 表不存在，无需迁移（先跑 validate.py init）")
            return 0
        if cur is False:
            print("photo_path 已经是可空，无需迁移")
            return 0

        n = conn.execute("SELECT COUNT(*) FROM meal_photos").fetchone()[0]
        print(f"photo_path 当前是 NOT NULL，需要重建表（现有 {n} 行）")

        if args.dry_run:
            print("dry-run：将重建 meal_photos 并保留全部数据")
            return 0

        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE meal_photos_new (
                id          INTEGER PRIMARY KEY,
                eaten_at    TEXT NOT NULL,
                photo_path  TEXT,
                note        TEXT,
                source      TEXT NOT NULL DEFAULT 'image'
                            CHECK (source IN ('image','text')),
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute("""
            INSERT INTO meal_photos_new (id, eaten_at, photo_path, note, source, created_at)
            SELECT id, eaten_at, photo_path, note, source, created_at FROM meal_photos
        """)
        conn.execute("DROP TABLE meal_photos")
        conn.execute("ALTER TABLE meal_photos_new RENAME TO meal_photos")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meal_eaten ON meal_photos(eaten_at)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uniq_meal_photo ON meal_photos(photo_path)")
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, description, sql)"
            " VALUES (?,?,?)", (VERSION, DESC, "rebuild meal_photos.photo_path nullable"))
        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM meal_photos").fetchone()[0]
        print(f"迁移完成：{n} -> {after} 行（应相等）")
        if n != after:
            print("警告：行数不一致！", file=sys.stderr)
            return 1
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""meal_photos（食物照片）回归测试。

在临时库 + 临时照片目录上跑，绝不碰真实数据/照片（有断言保证）。

    python3 scripts/test_meal.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATE = ROOT / "scripts" / "validate.py"
QUERY = ROOT / "scripts" / "query.py"
REAL_DB = (ROOT / "data" / "copilot.db").resolve()

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILED.append(name)


def make_png(path: Path, color=(255, 0, 0), size=4):
    """生成一个合法的小 PNG，不需要 PIL。"""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b""
    for _ in range(size):
        raw += b"\x00" + bytes(color) * size
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def seed_schema(db: Path):
    conn = sqlite3.connect(db)
    script = (ROOT / "scripts" / "schema.sql").read_text(encoding="utf-8")
    # 临时库不需要 WAL
    script = script.replace("PRAGMA journal_mode = WAL;", "")
    conn.executescript(script)
    conn.commit()
    conn.close()


def run(cmd, db, cwd, extra_env=None):
    env = dict(os.environ, PC_DB=str(db))
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable, str(cmd), *([] if cmd == VALIDATE else [])],
                          env=env, capture_output=True, text=True, cwd=cwd)


def vrun(args, db, cwd):
    env = dict(os.environ, PC_DB=str(db))
    return subprocess.run([sys.executable, str(VALIDATE), *args], env=env,
                          capture_output=True, text=True, cwd=cwd)


def qrun(args, db, cwd):
    env = dict(os.environ, PC_DB=str(db))
    return subprocess.run([sys.executable, str(QUERY), *args], env=env,
                          capture_output=True, text=True, cwd=cwd)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "t.db"
        assert db.resolve() != REAL_DB, "拒绝在真实库上跑测试"
        seed_schema(db)

        # 用临时 ROOT 做不到（脚本里 ROOT 固定），所以照片会写进真实 data/photos。
        # 因此这里给照片起一个绝不会和真实数据冲突的名字，跑完删掉。
        photo_dir = ROOT / "data" / "photos"

        src1 = tmp / "lunch.png"
        src2 = tmp / "dinner.png"
        make_png(src1, (200, 30, 30))
        make_png(src2, (30, 200, 30))

        created = []

        print("== 基本插入 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05 12:30", "photo": str(src1), "note": "午饭"})],
            db, tmp)
        check("insert exit 0", r.returncode == 0, r.stderr[:300] + r.stdout[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("insert ok", j.get("ok") is True)
            check("id=1", j.get("id") == 1, str(j.get("id")))
            rel = j["detail"]["photo_path"]
            created.append(ROOT / rel)
            check("路径在 data/photos 下", rel.startswith("data/photos/2026/03/"), rel)
            check("路径含日期前缀", "20260305" in rel, rel)
            check("照片已落盘", (ROOT / rel).exists())
            check("落盘内容一致", (ROOT / rel).read_bytes() == src1.read_bytes())

        print("== 时间缺省 -> 用当前时间 ==")
        r = vrun(["meal", "--json", json.dumps({"photo": str(src2)})], db, tmp)
        check("no-time exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            created.append(ROOT / j["detail"]["photo_path"])
            check("填了时间", bool(j["detail"]["eaten_at"]), str(j["detail"]["eaten_at"]))
            check("source=image", j["detail"]["source"] == "image")

        print("== 同一张照片重复 -> 拦截 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05 12:30", "photo": str(src1)})], db, tmp)
        check("duplicate exit 2", r.returncode == 2, f"rc={r.returncode} {r.stdout[:200]}")
        if r.returncode == 2:
            j = json.loads(r.stdout)
            check("reason=duplicate", j.get("reason") == "duplicate")
            check("existing_id=1", j.get("existing_id") == 1, str(j.get("existing_id")))

        print("== 文件不存在 -> 拒绝 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05", "photo": str(tmp / "nope.jpg")})], db, tmp)
        check("missing file exit 1", r.returncode == 1, f"rc={r.returncode}")
        if r.returncode == 1:
            check("reason=invalid", json.loads(r.stdout).get("reason") == "invalid")

        print("== 非图片扩展名 -> 拒绝 ==")
        bad = tmp / "evil.txt"
        bad.write_text("not an image")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05", "photo": str(bad)})], db, tmp)
        check("bad ext exit 1", r.returncode == 1, f"rc={r.returncode}")
        if r.returncode == 1:
            check("提示格式不支持", "格式" in json.loads(r.stdout).get("error", ""))

        print("== 空文件 -> 拒绝 ==")
        empty = tmp / "empty.png"
        empty.write_bytes(b"")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05", "photo": str(empty)})], db, tmp)
        check("empty file exit 1", r.returncode == 1, f"rc={r.returncode}")

        print("== source=image 但没给照片 -> 拒绝 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05", "source": "image"})], db, tmp)
        check("image w/o photo exit 1", r.returncode == 1, f"rc={r.returncode}")

        print("== source=text 但没 note -> 拒绝 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05", "source": "text"})], db, tmp)
        check("text w/o note exit 1", r.returncode == 1, f"rc={r.returncode}")

        print("== source=text + note -> 通过 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-06 08:00", "source": "text", "note": "豆浆油条"})],
            db, tmp)
        check("text ok exit 0", r.returncode == 0, r.stderr[:200])
        if r.returncode == 0:
            check("photo_path 为空", json.loads(r.stdout)["detail"]["photo_path"] is None)

        print("== 非法 source -> 拒绝 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "2026-03-05", "source": "video", "note": "x"})], db, tmp)
        check("bad source exit 1", r.returncode == 1, f"rc={r.returncode}")

        print("== 非法时间 -> 拒绝 ==")
        r = vrun(["meal", "--json", json.dumps(
            {"eaten_at": "昨天下午", "photo": str(src2)})], db, tmp)
        check("bad time exit 1", r.returncode == 1, f"rc={r.returncode}")

        print("== dry-run 不落盘 ==")
        src3 = tmp / "snack.png"
        make_png(src3, (30, 30, 200))
        before = set((photo_dir.rglob("*.png")) if photo_dir.exists() else [])
        r = vrun(["meal", "--dry-run", "--json", json.dumps(
            {"eaten_at": "2026-03-07", "photo": str(src3)})], db, tmp)
        check("dry-run exit 0", r.returncode == 0, r.stderr[:200])
        after = set((photo_dir.rglob("*.png")) if photo_dir.exists() else [])
        check("dry-run 未新增文件", before == after, f"{len(before)} -> {len(after)}")

        print("== meal-last ==")
        r = vrun(["meal-last"], db, tmp)
        check("meal-last exit 0", r.returncode == 0, r.stderr[:200])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("meal-last ok", j.get("ok") is True)
            check("有记录", len(j.get("recent", [])) >= 3, str(len(j.get("recent", []))))
            check("倒序（最新在前）",
                  j["recent"][0]["id"] > j["recent"][-1]["id"]
                  if len(j["recent"]) > 1 else True)

        print("== 查询 meal ==")
        r = qrun(["meal", "--from", "2026-03-01", "--to", "2026-03-31"], db, tmp)
        check("query exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            # 3 月应有 2 条：午饭（照片，指定 03-05）+ 豆浆油条（文字，03-06）。
            # 第二条照片没给时间，默认落到"今天"，不在 3 月区间内。
            check("3月查到 2 条", j["count"] == 2, str(j["count"]))
            check("有 by_day", len(j["by_day"]) == 2, str(j["by_day"]))
            withphoto = [x for x in j["items"] if x.get("photo_path")]
            check("照片项带 photo_abs", all(x.get("photo_abs") for x in withphoto))
            check("photo_abs 是绝对路径",
                  all(str(x["photo_abs"]).startswith("/") for x in withphoto))

        print("== 查询空区间 ==")
        r = qrun(["meal", "--from", "2020-01-01", "--to", "2020-01-31"], db, tmp)
        check("empty query exit 0", r.returncode == 0, r.stderr[:200])
        if r.returncode == 0:
            check("count=0", json.loads(r.stdout)["count"] == 0)

        print("== 审计留痕 ==")
        conn = sqlite3.connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE skill='food-capture'").fetchone()[0]
        dup = conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE action='reject_duplicate'").fetchone()[0]
        conn.close()
        check("有 insert 审计", n >= 3, str(n))
        check("有 reject_duplicate 审计", dup >= 1, str(dup))

        # 清理测试产生的照片（只删本次创建的，按精确路径）
        for p in created:
            try:
                if p.exists() and p.is_file():
                    p.unlink()
            except OSError:
                pass
        # 清理空目录
        for d in sorted((photo_dir / "2026").rglob("*"), reverse=True) if (photo_dir / "2026").exists() else []:
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass
        for d in [photo_dir / "2026", photo_dir / "2026" / "03"]:
            if d.exists() and d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                except OSError:
                    pass

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all meal tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

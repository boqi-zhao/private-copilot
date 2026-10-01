#!/usr/bin/env python3
"""chart.py 回归测试。

在临时库上跑，绝不碰真实数据（有断言保证）。

    python3 scripts/test_chart.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "scripts" / "chart.py"
CHARTGEN = ROOT / "scripts" / "chartgen"
REAL_DB = (ROOT / "data" / "copilot.db").resolve()

FAILED = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILED.append(name)


def seed(db: Path):
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            category_l1 TEXT NOT NULL,
            category_l2 TEXT,
            amount REAL NOT NULL,
            platform TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'text',
            raw_desc TEXT,
            note TEXT,
            created_at TEXT
        );
        CREATE TABLE finance_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            alipay REAL DEFAULT 0, wechat REAL DEFAULT 0,
            bank_balance REAL DEFAULT 0, cash REAL DEFAULT 0,
            wealth REAL DEFAULT 0, housing_fund REAL DEFAULT 0,
            huabei REAL DEFAULT 0, jd_baitiao REAL DEFAULT 0,
            credit_card REAL DEFAULT 0, loan_outstanding REAL DEFAULT 0,
            total_assets REAL, total_debt REAL, net_worth REAL
        );
        """
    )
    rows = [
        ("2026-10-01T12:00:00", "餐饮", "午饭", 35.0, "微信"),
        ("2026-10-01T19:00:00", "餐饮", "晚饭", 62.0, "支付宝"),
        ("2026-10-02T09:00:00", "交通", "地铁", 6.0, "支付宝"),
        ("2026-10-03T15:00:00", "购物", "衣服", 899.0, "信用卡"),
        ("2026-10-03T20:00:00", "娱乐", "电影", 45.0, "微信"),
        ("2026-10-07T10:00:00", "餐饮", "咖啡", 28.0, "现金"),
    ]
    conn.executemany(
        "INSERT INTO expenses(occurred_at,category_l1,category_l2,amount,platform)"
        " VALUES(?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO finance_snapshots(snapshot_at,total_assets,total_debt,net_worth)"
        " VALUES(?,?,?,?)",
        [("2026-08-31", 157500, 130500, 27000),
         ("2026-09-30", 177300, 128700, 48600),
         ("2026-10-31", 191150, 120600, 70550)])
    conn.commit()
    conn.close()


def run(args, db, cwd):
    env = dict(os.environ, PC_DB=str(db))
    return subprocess.run([sys.executable, str(CHART), *args], env=env,
                          capture_output=True, text=True, cwd=cwd)


def png_size(p: Path):
    b = p.read_bytes()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    return w, h


def pixel_at(p: Path, x: int, y: int):
    """读回 PNG 上某个像素的 RGB。

    图是压缩过的，没法直接 grep 颜色，所以借渲染器同一套 canvas 来解码 ——
    比在测试里手写一遍 PNG 反滤波靠谱，也不用给服务器装 Pillow。
    """
    code = (
        "const {createCanvas,loadImage}=require('@napi-rs/canvas');"
        "loadImage(process.argv[1]).then(im=>{"
        "const c=createCanvas(im.width,im.height);const g=c.getContext('2d');"
        "g.drawImage(im,0,0);"
        "const d=g.getImageData(+process.argv[2],+process.argv[3],1,1).data;"
        "console.log(d[0]+' '+d[1]+' '+d[2]);});"
    )
    r = subprocess.run(["node", "-e", code, str(p), str(x), str(y)],
                       capture_output=True, text=True, cwd=CHARTGEN)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return tuple(int(v) for v in r.stdout.split())


def is_colorful(px) -> bool:
    """扇形是饱和色，白底和灰色小字都不是。用来判断「图上到底画没画东西」。"""
    return px is not None and (max(px) - min(px)) > 40


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "t.db"
        assert db.resolve() != REAL_DB, "拒绝在真实库上跑测试"
        seed(db)

        print("== pie ==")
        out = tmp / "pie.png"
        r = run(["pie", "--from", "2026-10-01", "--to", "2026-10-07",
                 "--out", str(out)], db, tmp)
        check("pie exit 0", r.returncode == 0, r.stderr[:200])
        check("pie png exists", out.exists())
        check("pie 1920x1240 (2x)", png_size(out) == (1920, 1240), str(png_size(out)))
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("pie total correct", abs(j["total"] - 1075.0) < 0.01, str(j["total"]))
            check("pie items=4", j["items"] == 4, str(j["items"]))
        # 圆内左侧一点（逻辑坐标 225,375 -> 2x 设备 450,750），必须落在扇形上
        check("pie 真的画出了扇形（非白底）", is_colorful(pixel_at(out, 450, 750)),
              str(pixel_at(out, 450, 750)))

        print("== bar ==")
        out = tmp / "bar.png"
        r = run(["bar", "--by", "platform", "--from", "2026-10-01",
                 "--to", "2026-10-07", "--out", str(out)], db, tmp)
        check("bar exit 0", r.returncode == 0, r.stderr[:200])
        check("bar png", png_size(out) == (1920, 1240))
        if r.returncode == 0:
            # 种子数据里有 4 个平台：微信 / 支付宝 / 信用卡 / 现金
            check("bar items=4", json.loads(r.stdout)["items"] == 4,
                  str(json.loads(r.stdout)["items"]))

        print("== line (day) ==")
        out = tmp / "line.png"
        r = run(["line", "--metric", "day", "--from", "2026-10-01",
                 "--to", "2026-10-07", "--out", str(out)], db, tmp)
        check("line exit 0", r.returncode == 0, r.stderr[:200])
        check("line png", png_size(out) == (1920, 1240))
        if r.returncode == 0:
            check("line points=4", json.loads(r.stdout)["points"] == 4,
                  str(json.loads(r.stdout)["points"]))

        print("== line (month) ==")
        out = tmp / "line2.png"
        r = run(["line", "--metric", "month", "--from", "2026-10-01",
                 "--to", "2026-10-31", "--out", str(out)], db, tmp)
        check("line month exit 0", r.returncode == 0, r.stderr[:200])
        if r.returncode == 0:
            check("line month points=1", json.loads(r.stdout)["points"] == 1)

        print("== finance ==")
        out = tmp / "fin.png"
        r = run(["finance", "--out", str(out)], db, tmp)
        check("finance exit 0", r.returncode == 0, r.stderr[:200])
        check("finance png", png_size(out) == (1920, 1240))
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("finance points=3", j["points"] == 3, str(j["points"]))
            check("finance range", j["range"] == ["2026-08-31", "2026-10-31"], str(j["range"]))

        print("== trend ==")
        out = tmp / "trend.png"
        r = run(["trend", "--out", str(out)], db, tmp)
        check("trend exit 0", r.returncode == 0, r.stderr[:200])
        check("trend png", png_size(out) == (1920, 1240))

        print("== 单分类饼图：整圆必须画出来（回归） ==")
        # 只有一类时 ang 恰好是 2PI。@napi-rs/canvas 对
        # arc(-PI/2, -PI/2+2PI) 会画出空路径，整个饼图消失、只剩图例。
        # 一个月只有一种开销时很容易撞上，所以单独钉一条。
        db4 = tmp / "t4.db"
        seed(db4)
        c = sqlite3.connect(db4)
        c.execute("DELETE FROM expenses WHERE category_l1 <> '餐饮'")
        c.commit(); c.close()
        # 只剩 3 条餐饮：35 + 62 + 28 = 125
        out = tmp / "pie1.png"
        r = run(["pie", "--from", "2026-10-01", "--to", "2026-10-07",
                 "--out", str(out)], db4, tmp)
        check("single-category pie exit 0", r.returncode == 0, r.stderr[:200])
        check("single-category png", png_size(out) == (1920, 1240), str(png_size(out)))
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("single-category items=1", j["items"] == 1, str(j["items"]))
            check("single-category total 125", abs(j["total"] - 125.0) < 0.01, str(j["total"]))
            px = pixel_at(out, 450, 750)
            check("单分类饼图圆内有颜色（不是空图）", is_colorful(px), str(px))

        print("== empty range（不该崩） ==")
        out = tmp / "empty.png"
        r = run(["pie", "--from", "2020-01-01", "--to", "2020-01-31",
                 "--out", str(out)], db, tmp)
        check("empty pie exit 0", r.returncode == 0, r.stderr[:200])
        check("empty pie still writes png", out.exists())
        if r.returncode == 0:
            check("empty pie total 0", json.loads(r.stdout)["total"] == 0)

        print("== 空 finance 表（不该崩） ==")
        db2 = tmp / "t2.db"
        seed(db2)
        c = sqlite3.connect(db2)
        c.execute("DELETE FROM finance_snapshots"); c.commit(); c.close()
        out = tmp / "fin0.png"
        r = run(["finance", "--out", str(out)], db2, tmp)
        check("finance empty exit 0", r.returncode == 0, r.stderr[:200])
        check("finance empty png", out.exists())

        print("== 单点折线（不该崩） ==")
        db3 = tmp / "t3.db"
        seed(db3)
        c = sqlite3.connect(db3)
        c.execute("DELETE FROM expenses WHERE occurred_at NOT LIKE '2026-10-07%'")
        c.commit(); c.close()
        out = tmp / "one.png"
        r = run(["line", "--metric", "day", "--from", "2026-10-01",
                 "--to", "2026-10-07", "--out", str(out)], db3, tmp)
        check("single-point line exit 0", r.returncode == 0, r.stderr[:200])
        check("single-point png", png_size(out) == (1920, 1240))

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        return 1
    print("all chart tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

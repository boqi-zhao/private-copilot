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

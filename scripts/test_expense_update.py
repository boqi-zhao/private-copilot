#!/usr/bin/env python3
"""expense-update 回归测试。

在临时库上跑，绝不碰真实 data/copilot.db。
用法：python3 scripts/test_expense_update.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATE = ROOT / "scripts" / "validate.py"
SCHEMA = ROOT / "scripts" / "schema.sql"
REAL_DB = ROOT / "data" / "copilot.db"

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def run(db: Path, *args: str) -> tuple[int, dict]:
    # 断言测试库绝不是真实库，避免误伤生产数据
    assert db.resolve() != REAL_DB.resolve(), "拒绝在真实库上运行测试"
    env = {**os.environ, "PC_DB": str(db)}
    p = subprocess.run([sys.executable, str(VALIDATE), *args],
                       capture_output=True, text=True, env=env)
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        out = {"_stdout": p.stdout, "_stderr": p.stderr}
    return p.returncode, out


def seed(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.execute(
        """INSERT INTO expenses (occurred_at,category_l1,category_l2,amount,platform,source,raw_desc)
           VALUES ('2026-09-30T11:48:00','餐饮','日常',42.0,'支付宝','text','晚饭')"""
    )
    conn.execute(
        """INSERT INTO expenses (occurred_at,category_l1,category_l2,amount,platform,source,raw_desc)
           VALUES ('2026-09-30T11:32:00','购物','家居',299.0,'信用卡','text','咖啡机')"""
    )
    conn.commit()
    conn.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pc-test-"))
    try:
        db = tmp / "test.db"
        seed(db)
        print("expense-update 回归测试\n")

        print("[1] expense-last 能列出最近记录")
        rc, out = run(db, "expense-last")
        check("退出码 0", rc == 0, f"rc={rc}")
        check("返回 2 笔", len(out.get("recent", [])) == 2, str(out)[:120])
        check("最新一笔在前（id=2）",
              out.get("recent", [{}])[0].get("id") == 2, str(out)[:120])

        print("\n[2] 改分类：餐饮→交通（晚饭 id=1）")
        rc, out = run(db, "expense-update", "--json",
                      json.dumps({"id": 1, "category_l1": "交通", "category_l2": "打车"}))
        check("退出码 0", rc == 0, f"rc={rc}")
        check("返回 ok", out.get("ok") is True, str(out)[:120])
        ch = out.get("detail", {}).get("changes", {})
        check("记录了 from/to", ch.get("category_l1") == {"from": "餐饮", "to": "交通"},
              str(ch)[:120])
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT category_l1,category_l2,amount,platform"
                           " FROM expenses WHERE id=1").fetchone()
        check("库内已更新", row == ("交通", "打车", 42.0, "支付宝"), str(row))
        check("金额/平台未被误改", row[2] == 42.0 and row[3] == "支付宝", str(row))
        logs = conn.execute("SELECT action FROM ingest_log WHERE target_id=1"
                            " ORDER BY id").fetchall()
        check("审计含 update", ("update",) in logs, str(logs))
        conn.close()

        print("\n[3] 非法一级分类应拒绝（不静默回退）")
        rc, out = run(db, "expense-update", "--json",
                      json.dumps({"id": 1, "category_l1": "星际旅行"}))
        check("退出码 1", rc == 1, f"rc={rc}")
        check("reason=invalid", out.get("reason") == "invalid", str(out)[:120])
        conn = sqlite3.connect(db)
        now = conn.execute("SELECT category_l1 FROM expenses WHERE id=1").fetchone()[0]
        check("库内未被改动", now == "交通", now)
        conn.close()

        print("\n[4] 非法平台应拒绝")
        rc, out = run(db, "expense-update", "--json",
                      json.dumps({"id": 1, "platform": "比特币"}))
        check("退出码 1", rc == 1, f"rc={rc}")

        print("\n[5] 不存在的 id 应拒绝")
        rc, out = run(db, "expense-update", "--json",
                      json.dumps({"id": 99999, "amount": 1}))
        check("退出码 1", rc == 1, f"rc={rc}")
        check("提示记录不存在", "不存在" in json.dumps(out, ensure_ascii=False),
              str(out)[:120])

        print("\n[6] 改成与另一笔完全相同应判重复")
        rc, out = run(db, "expense-update", "--json", json.dumps({
            "id": 1, "amount": 299, "occurred_at": "2026-09-30 11:32",
            "category_l1": "购物", "platform": "信用卡"}))
        check("退出码 2", rc == 2, f"rc={rc}")
        check("reason=duplicate", out.get("reason") == "duplicate", str(out)[:120])

        print("\n[7] 不带 id 时作用于最近一笔（咖啡机 id=2）")
        rc, out = run(db, "expense-update", "--dry-run", "--json",
                      json.dumps({"note": "备注"}))
        check("退出码 0", rc == 0, f"rc={rc}")
        check("命中 id=2", out.get("id") == 2, str(out)[:120])

        print("\n[8] dry-run 不落库")
        rc, out = run(db, "expense-update", "--dry-run", "--json",
                      json.dumps({"id": 2, "amount": 1}))
        check("退出码 0", rc == 0, f"rc={rc}")
        conn = sqlite3.connect(db)
        amt = conn.execute("SELECT amount FROM expenses WHERE id=2").fetchone()[0]
        check("金额仍为 299.0", amt == 299.0, str(amt))
        conn.close()

        print("\n[9] 无实际改动应视为 noop")
        rc, out = run(db, "expense-update", "--json", json.dumps({"id": 2, "amount": 299}))
        check("退出码 0", rc == 0, f"rc={rc}")
        check("标记 noop", out.get("detail", {}).get("noop") is True, str(out)[:120])

        print("\n[10] 空账本改最近一笔应友好报错")
        empty = tmp / "empty.db"
        conn = sqlite3.connect(empty)
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.commit()
        conn.close()
        rc, out = run(empty, "expense-update", "--json", json.dumps({"note": "x"}))
        check("退出码 1", rc == 1, f"rc={rc}")
        check("提示账本为空", "还没有任何记录" in json.dumps(out, ensure_ascii=False),
              str(out)[:140])

        print(f"\n{'=' * 40}\n通过 {PASS} / 失败 {FAIL}")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

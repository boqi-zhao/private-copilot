#!/usr/bin/env python3
"""body_measurements（身体尺寸）回归测试。

在临时库上跑，绝不碰真实数据（有断言保证）。

    python3 scripts/test_body.py
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


def seed_schema(db: Path):
    conn = sqlite3.connect(db)
    script = (ROOT / "scripts" / "schema.sql").read_text(encoding="utf-8")
    script = script.replace("PRAGMA journal_mode = WAL;", "")
    conn.executescript(script)
    conn.commit()
    conn.close()


def vrun(args, db):
    env = dict(os.environ, PC_DB=str(db))
    return subprocess.run([sys.executable, str(VALIDATE), *args], env=env,
                          capture_output=True, text=True, cwd=ROOT)


def qrun(args, db):
    env = dict(os.environ, PC_DB=str(db))
    return subprocess.run([sys.executable, str(QUERY), *args], env=env,
                          capture_output=True, text=True, cwd=ROOT)


def body_ok(db, **kwargs):
    r = vrun(["body", "--json", json.dumps(kwargs)], db)
    return r


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "t.db"
        assert db.resolve() != REAL_DB, "拒绝在真实库上跑测试"
        seed_schema(db)

        print("== 基本插入：六项齐全 ==")
        r = body_ok(db, measured_at="2026-03-01", waist=90, hip=100, chest=98,
                    bicep=34, thigh=56, calf=37)
        check("exit 0", r.returncode == 0, r.stderr[:300] + r.stdout[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("ok", j.get("ok") is True)
            check("id=1", j.get("id") == 1, str(j.get("id")))
            check("action=insert", j["detail"]["action"] == "insert")
            check("腰围入库", j["detail"]["waist"] == 90.0)
            check("小腿围入库", j["detail"]["calf"] == 37.0)

        print("== 允许缺项：只报腰围 ==")
        r = body_ok(db, measured_at="2026-03-02", waist=89)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("新 id=2", j.get("id") == 2, str(j.get("id")))
            check("只写了一项", set(j["detail"]) & {"waist", "hip", "chest",
                                                 "bicep", "thigh", "calf"} == {"waist"})
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT hip, calf FROM body_measurements WHERE id=2").fetchone()
        check("缺项存 NULL 而非 0", row == (None, None), str(row))
        conn.close()

        print("== 中文键名也认 ==")
        r = body_ok(db, measured_at="2026-03-03", **{"腰围": 88, "大腿围": 55})
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("腰围=88", j["detail"].get("waist") == 88.0, str(j["detail"]))
            check("大腿围=55", j["detail"].get("thigh") == 55.0, str(j["detail"]))

        print("== 同一天重复上报：局部合并，不清空其他项 ==")
        r = body_ok(db, measured_at="2026-03-01", bicep=35)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("回到 id=1", j.get("id") == 1, str(j.get("id")))
            check("action=update", j["detail"]["action"] == "update")
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT waist, hip, bicep FROM body_measurements WHERE id=1").fetchone()
        check("别的项没被清零", row[0] == 90.0 and row[1] == 100.0, str(row))
        check("新项已更新", row[2] == 35.0, str(row))
        n = conn.execute("SELECT COUNT(*) FROM body_measurements").fetchone()[0]
        check("没有多出一行", n == 3, str(n))
        conn.close()

        print("== 一项都不给 -> 拒绝 ==")
        r = body_ok(db, measured_at="2026-03-04")
        check("exit 1", r.returncode == 1, f"rc={r.returncode} {r.stdout[:200]}")
        if r.returncode == 1:
            check("reason=invalid", json.loads(r.stdout).get("reason") == "invalid")

        print("== 离谱数值 -> 拒绝 ==")
        for bad, why in ((3, "把 cm 写成 m"), (500, "明显超出人体范围")):
            r = body_ok(db, measured_at="2026-03-05", waist=bad)
            check(f"{why} exit 1", r.returncode == 1, f"rc={r.returncode}")
            if r.returncode == 1:
                j = json.loads(r.stdout)
                check(f"{why} 给出范围提示", "范围" in j.get("error", ""),
                      j.get("error", "")[:80])

        print("== 负数 -> 拒绝 ==")
        r = body_ok(db, measured_at="2026-03-05", waist=-10)
        check("exit 1", r.returncode == 1, f"rc={r.returncode}")

        print("== 时间缺失 -> 拒绝（围度不像饭照，不能默认 now）==")
        r = vrun(["body", "--json", json.dumps({"waist": 90})], db)
        check("exit 1", r.returncode == 1, f"rc={r.returncode} {r.stdout[:200]}")

        print("== 带单位的字符串能解析 ==")
        r = body_ok(db, measured_at="2026-03-06", waist="88.5cm", thigh="55 cm")
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("88.5", j["detail"].get("waist") == 88.5, str(j["detail"]))
            check("55.0", j["detail"].get("thigh") == 55.0, str(j["detail"]))

        print("== dry-run 不落盘 ==")
        conn = sqlite3.connect(db)
        before = conn.execute("SELECT COUNT(*) FROM body_measurements").fetchone()[0]
        conn.close()
        r = vrun(["body", "--dry-run", "--json",
                  json.dumps({"measured_at": "2026-09-09", "waist": 80})], db)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            check("dry_run 标记", json.loads(r.stdout).get("dry_run") is True)
        conn = sqlite3.connect(db)
        after = conn.execute("SELECT COUNT(*) FROM body_measurements").fetchone()[0]
        conn.close()
        check("行数没变", before == after, f"{before} -> {after}")

        print("== body-last 按日期倒序 ==")
        r = vrun(["body-last"], db)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            recent = j.get("recent", [])
            check("有数据", len(recent) > 0)
            dates = [x["measured_at"] for x in recent]
            check("倒序排列", dates == sorted(dates, reverse=True), str(dates))
            check("最新是 2026-03-06", dates[0] == "2026-03-06", str(dates))

        print("== 查询：变化量只在两边都有值时给出 ==")
        r = qrun(["body"], db)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("count=4", j.get("count") == 4, str(j.get("count")))
            items = j["items"]
            check("按日期升序", [x["measured_at"] for x in items] ==
                  sorted(x["measured_at"] for x in items))
            # 03-01(腰90,臀100) -> 03-02(腰89,臀 NULL)：臀围缺项不应出现在 delta
            d = items[1]["delta"]
            check("腰围有差值", d.get("waist") == -1.0, str(d))
            check("臀围缺项不进 delta", "hip" not in d, str(d))
            # 腰臀比：只有两边都有才算
            check("03-01 腰臀比 0.9", items[0]["whr"] == 0.9, str(items[0]["whr"]))
            check("03-02 无臀围 -> whr 为 None", items[1]["whr"] is None,
                  str(items[1]["whr"]))

        print("== 查询：compare 汇总跳过缺项 ==")
        if r.returncode == 0:
            j = json.loads(r.stdout)
            cmp_ = j.get("compare")
            check("有 compare", bool(cmp_), str(cmp_))
            if cmp_:
                check("from=最初", cmp_["from"] == "2026-03-01", str(cmp_))
                check("to=最新", cmp_["to"] == "2026-03-06", str(cmp_))
                changed = cmp_["changed"]
                # 03-01 腰90 -> 03-06 腰88.5
                check("腰围变化 -1.5", changed.get("waist") == -1.5, str(changed))
                check("缺项不参与对比", "hip" not in changed, str(changed))

        print("== 区间必须包含起始当天（回归：日期型列的下界比较）==")
        # 历史 bug：date_range_clause 给下界拼了 'T00:00:00'，而 measured_at 存的是
        # '2026-03-02'。字符串比较下 '2026-03-02' < '2026-03-02T00:00:00'，
        # 于是**起始当天被静默漏掉**，查单日直接返回 0 行。
        # finance_snapshots / health_metrics 同样是日期型列，一并受影响。
        r = qrun(["body", "--from", "2026-03-06", "--to", "2026-03-06"], db)
        check("单日查询 exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("单日查询命中 1 行（不是 0）", j.get("count") == 1, str(j.get("count")))

        r = qrun(["body", "--from", "2026-03-02", "--to", "2026-03-03"], db)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("count=2", j.get("count") == 2, str(j.get("count")))
            check("起始当天在内", j["items"][0]["measured_at"] == "2026-03-02",
                  str([x["measured_at"] for x in j["items"]]))

        print("== 空区间返回空而不是报错 ==")
        r = qrun(["body", "--from", "2020-01-01", "--to", "2020-12-31"], db)
        check("exit 0", r.returncode == 0, r.stderr[:300])
        if r.returncode == 0:
            j = json.loads(r.stdout)
            check("count=0", j.get("count") == 0, str(j.get("count")))
            check("无 compare", "compare" not in j, str(j.keys()))

        print("== 审计留痕 ==")
        conn = sqlite3.connect(db)
        logs = conn.execute(
            "SELECT action FROM ingest_log WHERE target_table='body_measurements'"
        ).fetchall()
        conn.close()
        actions = [x[0] for x in logs]
        check("有 insert 日志", "insert" in actions, str(actions))
        check("有 update 日志", "update" in actions, str(actions))
        check("日志条数=插入+更新", len(actions) == 5, str(len(actions)))

        print("== 单位固定 cm：不接受其他单位混淆 ==")
        # 传 1.1（误当米）应被范围校验拦下
        r = body_ok(db, measured_at="2026-03-07", waist=1.1)
        check("1.1 被拒绝", r.returncode == 1, f"rc={r.returncode}")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

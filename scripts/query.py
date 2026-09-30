#!/usr/bin/env python3
"""private-copilot 查询（智能问数）。

给 agent 用的只读查询接口。SQL 固化在这里，agent 只填参数，
避免每次都靠 LLM 现场拼 SQL（日期边界、时区、聚合口径容易错）。

用法：
    # 区间花销汇总
    python3 query.py spend --from 2026-10-01 --to 2026-10-07

    # 按一级分类
    python3 query.py spend --from 2026-10-01 --to 2026-10-07 --by category_l1

    # 按平台
    python3 query.py spend --from 2026-10-01 --to 2026-10-07 --by platform

    # 按天
    python3 query.py spend --from 2026-10-01 --to 2026-10-07 --by day

    # 明细
    python3 query.py list --from 2026-10-01 --to 2026-10-07 --limit 20

    # 财务快照趋势
    python3 query.py finance --limit 12

    # 只读自由 SQL（兜底，带护栏）
    python3 query.py sql --q "select count(*) from expenses"

所有输出为 JSON，便于 agent 直接组织成自然语言回复。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PC_DB", ROOT / "data" / "copilot.db"))


def connect_ro() -> sqlite3.Connection:
    """只读连接。物理上禁止写入，比关键字黑名单可靠。"""
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在: {DB_PATH}（先跑 validate.py init）")
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def norm_date(s: str | None, default: str | None = None) -> str | None:
    if not s:
        return default
    s = str(s).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)      # 只有月日 -> 补当年
    if m:
        import datetime
        return f"{datetime.date.today().year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    raise SystemExit(f"日期格式无法解析: {s!r}（用 YYYY-MM-DD）")


def date_range_clause(frm: str | None, to: str | None, col: str) -> tuple[str, list]:
    """构造半开区间 [from, to+1day)，避免时间戳跨天漏算。"""
    clauses, params = [], []
    if frm:
        clauses.append(f"{col} >= ?")
        params.append(frm + "T00:00:00")
    if to:
        clauses.append(f"{col} < ?")
        params.append(to + "T23:59:60")     # 含当天全部时刻
    return (" AND ".join(clauses) if clauses else "1=1"), params


# ---------------------------------------------------------------------
# 查询实现
# ---------------------------------------------------------------------
def q_spend(args) -> dict:
    frm = norm_date(args.frm)
    to = norm_date(args.to)
    if not frm and not to:
        # 默认本月
        import datetime
        today = datetime.date.today()
        frm = today.replace(day=1).isoformat()
        to = today.isoformat()

    where, params = date_range_clause(frm, to, "occurred_at")
    conn = connect_ro()
    try:
        total = conn.execute(
            f"SELECT ROUND(COALESCE(SUM(amount),0),2) AS total, COUNT(*) AS n"
            f" FROM expenses WHERE {where}", params
        ).fetchone()

        result = {
            "range": {"from": frm, "to": to},
            "total": total["total"],
            "count": total["n"],
        }

        if args.by == "category_l1":
            rows = conn.execute(
                f"""SELECT category_l1 AS k, ROUND(SUM(amount),2) AS v, COUNT(*) AS n
                    FROM expenses WHERE {where}
                    GROUP BY category_l1 ORDER BY v DESC""", params
            ).fetchall()
            result["by_category_l1"] = [dict(r) for r in rows]
        elif args.by == "category_l2":
            rows = conn.execute(
                f"""SELECT category_l1, IFNULL(category_l2,'(未细分)') AS category_l2,
                           ROUND(SUM(amount),2) AS v, COUNT(*) AS n
                    FROM expenses WHERE {where}
                    GROUP BY category_l1, category_l2 ORDER BY v DESC""", params
            ).fetchall()
            result["by_category_l2"] = [dict(r) for r in rows]
        elif args.by == "platform":
            rows = conn.execute(
                f"""SELECT platform AS k, ROUND(SUM(amount),2) AS v, COUNT(*) AS n
                    FROM expenses WHERE {where}
                    GROUP BY platform ORDER BY v DESC""", params
            ).fetchall()
            result["by_platform"] = [dict(r) for r in rows]
        elif args.by == "day":
            rows = conn.execute(
                f"""SELECT substr(occurred_at,1,10) AS day,
                           ROUND(SUM(amount),2) AS v, COUNT(*) AS n
                    FROM expenses WHERE {where}
                    GROUP BY day ORDER BY day""", params
            ).fetchall()
            result["by_day"] = [dict(r) for r in rows]

        if args.by is None:
            # 没指定维度时，给一个分类分布（最常用）
            rows = conn.execute(
                f"""SELECT category_l1 AS k, ROUND(SUM(amount),2) AS v, COUNT(*) AS n
                    FROM expenses WHERE {where}
                    GROUP BY category_l1 ORDER BY v DESC""", params
            ).fetchall()
            result["by_category_l1"] = [dict(r) for r in rows]

        return result
    finally:
        conn.close()


def q_list(args) -> dict:
    frm = norm_date(args.frm)
    to = norm_date(args.to)
    where, params = date_range_clause(frm, to, "occurred_at")
    conn = connect_ro()
    try:
        rows = conn.execute(
            f"""SELECT id, occurred_at, category_l1, category_l2, amount, platform,
                       source, raw_desc
                FROM expenses WHERE {where}
                ORDER BY occurred_at DESC LIMIT ?""",
            (*params, args.limit),
        ).fetchall()
        return {"count": len(rows), "items": [dict(r) for r in rows]}
    finally:
        conn.close()


def q_finance(args) -> dict:
    conn = connect_ro()
    try:
        rows = conn.execute(
            """SELECT snapshot_at, alipay, wechat, bank_balance, cash, wealth,
                      housing_fund, huabei, jd_baitiao, credit_card,
                      loan_outstanding, total_assets, total_debt, net_worth
               FROM finance_snapshots ORDER BY snapshot_at DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()
        items = [dict(r) for r in rows]
        out = {"count": len(items), "snapshots": items}

        if len(items) >= 2:
            latest, prev = items[0], items[1]
            out["change"] = {
                "from": prev["snapshot_at"], "to": latest["snapshot_at"],
                "total_assets_delta": round(latest["total_assets"] - prev["total_assets"], 2),
                "total_debt_delta": round(latest["total_debt"] - prev["total_debt"], 2),
                "net_worth_delta": round(latest["net_worth"] - prev["net_worth"], 2),
            }
        return out
    finally:
        conn.close()


def q_meal(args) -> dict:
    """食物照片记录。返回相对路径，agent 可拼成绝对路径发图。"""
    import datetime
    frm = norm_date(args.frm)
    to = norm_date(args.to)
    where, params = date_range_clause(frm, to, "eaten_at")
    conn = connect_ro()
    try:
        rows = conn.execute(
            f"""SELECT id, eaten_at, photo_path, note, source, created_at
                FROM meal_photos WHERE {where}
                ORDER BY eaten_at DESC LIMIT ?""",
            (*params, args.limit),
        ).fetchall()
        items = [dict(r) for r in rows]

        # 统计：按天聚合，便于"最近吃得怎么样"
        by_day = conn.execute(
            f"""SELECT substr(eaten_at,1,10) AS day, COUNT(*) AS n
                FROM meal_photos WHERE {where}
                GROUP BY day ORDER BY day DESC LIMIT 30""", params
        ).fetchall()

        # 照片绝对路径前缀：调用方（agent）需要绝对路径才能发送
        root = str(DB_PATH.parent.parent)
        for it in items:
            if it.get("photo_path"):
                it["photo_abs"] = f"{root}/{it['photo_path']}"
        return {"count": len(items), "range": [frm, to],
                "by_day": [dict(r) for r in by_day], "items": items}
    finally:
        conn.close()


FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b",
    re.IGNORECASE,
)


def q_sql(args) -> dict:
    sql = (args.q or "").strip().rstrip(";")
    if not sql:
        raise SystemExit("需要 --q")
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise SystemExit("只允许 SELECT / WITH 查询")
    if FORBIDDEN.search(sql):
        raise SystemExit(f"查询包含被禁止的关键字: {FORBIDDEN.search(sql).group(0)}")
    if ";" in sql:
        raise SystemExit("不允许多条语句")

    conn = connect_ro()
    try:
        rows = conn.execute(sql).fetchall()
        if len(rows) > args.limit:
            rows = rows[: args.limit]
            truncated = True
        else:
            truncated = False
        return {"count": len(rows), "truncated": truncated,
                "rows": [dict(r) for r in rows]}
    except sqlite3.Error as e:
        raise SystemExit(f"SQL 错误: {e}")
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="private-copilot 查询")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("spend", help="区间花销")
    p.add_argument("--from", dest="frm")
    p.add_argument("--to", dest="to")
    p.add_argument("--by", choices=["category_l1", "category_l2", "platform", "day"])
    p.set_defaults(func=q_spend)

    p = sub.add_parser("list", help="消费明细")
    p.add_argument("--from", dest="frm")
    p.add_argument("--to", dest="to")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=q_list)

    p = sub.add_parser("meal", help="食物照片记录")
    p.add_argument("--from", dest="frm")
    p.add_argument("--to", dest="to")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=q_meal)

    p = sub.add_parser("finance", help="财务快照趋势")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(func=q_finance)

    p = sub.add_parser("sql", help="只读自由 SQL")
    p.add_argument("--q", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=q_sql)

    args = ap.parse_args()
    print(json.dumps(args.func(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

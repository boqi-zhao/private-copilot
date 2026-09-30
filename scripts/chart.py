#!/usr/bin/env python3
"""private-copilot 图表生成（饼图 / 柱状图 / 折线图）。

给 agent 用的可视化接口。输出 PNG，可直接通过 message 工具发到飞书。

架构：本脚本只负责「查数据 + 组织成 JSON」，
     真正的渲染交给 chartgen/render.js（@napi-rs/canvas + Noto CJK）。

为什么这么分：
    纯 Python 画图（自己描点阵字体）中文基本没法看，
    而系统里已经有 Node 和中文字体，用 cairo 排版是对的。
    Python 侧保持零依赖，渲染侧只依赖一个 npm 包。

前置：
    apt install fonts-noto-cjk        # 中文字体，一次性
    cd scripts/chartgen && npm install

用法：
    python3 chart.py pie     --from 2026-10-01 --to 2026-10-07 --out /tmp/a.png
    python3 chart.py bar     --from 2026-10-01 --to 2026-10-31 --by platform --out /tmp/b.png
    python3 chart.py line    --from 2026-10-01 --to 2026-10-31 --metric day --out /tmp/c.png
    python3 chart.py finance --out /tmp/d.png
    python3 chart.py trend   --out /tmp/e.png

成功时打印 JSON：{"out": "...", "type": "...", "points": N, ...}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PC_DB", ROOT / "data" / "copilot.db"))
RENDERER = ROOT / "scripts" / "chartgen" / "render.js"

# 分类 / 平台的中文名直接用于图上标签（有中文字体了，不需要转英文）
FOOTER = "单位：人民币元 · 数据来自消费记录库"


class DataError(Exception):
    """数据不足以画图（例如空区间），不是程序错误。"""


# ---------------------------------------------------------------- 数据
def connect_ro() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(f"数据库不存在: {DB_PATH}")
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def norm_date(s: str | None):
    if not s:
        return None
    s = str(s).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if m:
        import datetime
        return f"{datetime.date.today().year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    raise SystemExit(f"日期格式无法解析: {s!r}（用 YYYY-MM-DD）")


def range_clause(frm, to, col):
    cl, ps = [], []
    if frm:
        cl.append(f"{col} >= ?"); ps.append(frm + "T00:00:00")
    if to:
        cl.append(f"{col} < ?"); ps.append(to + "T23:59:60")
    return (" AND ".join(cl) if cl else "1=1"), ps


def default_month():
    import datetime
    t = datetime.date.today()
    return t.replace(day=1).isoformat(), t.isoformat()


def resolve_range(frm, to):
    frm, to = norm_date(frm), norm_date(to)
    if not frm and not to:
        return default_month()
    return frm, to


def render(payload: dict, out: str) -> None:
    """调用 Node 渲染器，把 payload 写成 PNG。"""
    if not RENDERER.exists():
        raise SystemExit(f"渲染器不存在: {RENDERER}")
    if not (RENDERER.parent / "node_modules").exists():
        raise SystemExit(
            f"渲染依赖未安装。先执行：\n  cd {RENDERER.parent} && npm install")
    out_path = Path(out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        spec = f.name
    try:
        r = subprocess.run(["node", str(RENDERER), spec, str(out_path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"渲染失败: {r.stderr.strip()[:500]}")
        if r.stderr.strip():
            print(r.stderr.strip(), file=sys.stderr)
    finally:
        os.unlink(spec)

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit(f"渲染未产出文件: {out_path}")


# ---------------------------------------------------------------- 各图
def cmd_pie(a):
    frm, to = resolve_range(a.frm, a.to)
    where, ps = range_clause(frm, to, "occurred_at")
    conn = connect_ro()
    try:
        rows = conn.execute(
            f"SELECT category_l1 k, ROUND(SUM(amount),2) v, COUNT(*) n"
            f" FROM expenses WHERE {where} GROUP BY category_l1 ORDER BY v DESC", ps).fetchall()
        total = conn.execute(
            f"SELECT ROUND(COALESCE(SUM(amount),0),2) t, COUNT(*) c"
            f" FROM expenses WHERE {where}", ps).fetchone()
    finally:
        conn.close()

    items = [{"k": r["k"], "v": r["v"], "n": r["n"]} for r in rows]
    sub = (f"{frm} ~ {to} · 总计 ¥{total['t']:.2f} · 共 {total['c']} 笔"
           if items else f"{frm} ~ {to} · 无记录")
    render({"type": "pie", "title": "消费分类占比", "subtitle": sub,
            "items": items, "footer": FOOTER}, a.out)
    return {"out": a.out, "type": "pie", "range": [frm, to], "total": total["t"],
            "count": total["c"], "items": len(items), "size": [960, 620]}


def cmd_bar(a):
    frm, to = resolve_range(a.frm, a.to)
    where, ps = range_clause(frm, to, "occurred_at")
    col = {"category_l1": "category_l1", "platform": "platform",
           "day": "substr(occurred_at,1,10)"}[a.by]
    conn = connect_ro()
    try:
        rows = conn.execute(
            f"SELECT {col} k, ROUND(SUM(amount),2) v, COUNT(*) n FROM expenses"
            f" WHERE {where} GROUP BY k ORDER BY v DESC LIMIT 12", ps).fetchall()
    finally:
        conn.close()

    titles = {"category_l1": "分类支出", "platform": "各平台支出", "day": "每日支出"}
    items = [{"k": r["k"], "v": r["v"], "n": r["n"]} for r in rows]
    if a.by == "day":
        items.sort(key=lambda x: x["k"])
        items = items[-12:]
    render({"type": "bar", "title": titles[a.by], "subtitle": f"{frm} ~ {to}",
            "items": items, "footer": FOOTER}, a.out)
    return {"out": a.out, "type": "bar", "by": a.by, "range": [frm, to],
            "items": len(items), "size": [960, 620]}


def cmd_line(a):
    frm, to = resolve_range(a.frm, a.to)
    where, ps = range_clause(frm, to, "occurred_at")
    conn = connect_ro()
    try:
        if a.metric == "day":
            rows = conn.execute(
                f"SELECT substr(occurred_at,1,10) k, ROUND(SUM(amount),2) v"
                f" FROM expenses WHERE {where} GROUP BY k ORDER BY k", ps).fetchall()
            title = "每日消费趋势"
        else:
            rows = conn.execute(
                f"SELECT substr(occurred_at,1,7) k, ROUND(SUM(amount),2) v"
                f" FROM expenses WHERE {where} GROUP BY k ORDER BY k", ps).fetchall()
            title = "每月消费趋势"
    finally:
        conn.close()

    pts = [{"k": r["k"], "v": r["v"]} for r in rows]
    render({"type": "line", "title": title, "subtitle": f"{frm} ~ {to}",
            "points": pts, "footer": FOOTER}, a.out)
    return {"out": a.out, "type": "line", "metric": a.metric, "range": [frm, to],
            "points": len(pts), "size": [960, 620]}


def cmd_finance(a):
    conn = connect_ro()
    try:
        rows = conn.execute(
            "SELECT snapshot_at, total_assets, total_debt, net_worth"
            " FROM finance_snapshots ORDER BY snapshot_at DESC LIMIT ?",
            (a.limit,)).fetchall()
    finally:
        conn.close()
    rows = list(reversed(rows))
    labels = [r["snapshot_at"][2:] for r in rows]     # 2026-09-30 -> 26-09-30
    series = [
        {"name": "资产", "values": [r["total_assets"] for r in rows]},
        {"name": "负债", "values": [r["total_debt"] for r in rows]},
        {"name": "净资产", "values": [r["net_worth"] for r in rows]},
    ]
    render({"type": "multi_line", "title": "资产 / 负债 / 净资产",
            "subtitle": f"最近 {len(rows)} 期快照" if rows else "暂无快照",
            "labels": labels, "series": series, "footer": FOOTER}, a.out)
    return {"out": a.out, "type": "multi_line", "points": len(labels),
            "range": [rows[0]["snapshot_at"], rows[-1]["snapshot_at"]] if rows else None,
            "size": [960, 620]}


def cmd_trend(a):
    conn = connect_ro()
    try:
        rows = conn.execute(
            "SELECT substr(occurred_at,1,7) k, ROUND(SUM(amount),2) v"
            " FROM expenses GROUP BY k ORDER BY k").fetchall()
    finally:
        conn.close()
    pts = [{"k": r["k"], "v": r["v"]} for r in rows]
    render({"type": "line", "title": "消费趋势（全部历史）",
            "subtitle": "按月汇总", "points": pts, "footer": FOOTER}, a.out)
    return {"out": a.out, "type": "trend", "points": len(pts), "size": [960, 620]}


def main() -> int:
    ap = argparse.ArgumentParser(description="private-copilot 图表")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pie", help="分类占比饼图")
    p.add_argument("--from", dest="frm"); p.add_argument("--to", dest="to")
    p.add_argument("--out", required=True); p.set_defaults(func=cmd_pie)

    p = sub.add_parser("bar", help="柱状图")
    p.add_argument("--from", dest="frm"); p.add_argument("--to", dest="to")
    p.add_argument("--by", choices=["category_l1", "platform", "day"], default="category_l1")
    p.add_argument("--out", required=True); p.set_defaults(func=cmd_bar)

    p = sub.add_parser("line", help="折线图")
    p.add_argument("--from", dest="frm"); p.add_argument("--to", dest="to")
    p.add_argument("--metric", choices=["day", "month"], default="day")
    p.add_argument("--out", required=True); p.set_defaults(func=cmd_line)

    p = sub.add_parser("finance", help="资产/负债/净资产对比")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--out", required=True); p.set_defaults(func=cmd_finance)

    p = sub.add_parser("trend", help="历史月度趋势")
    p.add_argument("--out", required=True); p.set_defaults(func=cmd_trend)

    args = ap.parse_args()
    print(json.dumps(args.func(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

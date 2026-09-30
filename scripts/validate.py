#!/usr/bin/env python3
"""private-copilot 写入与查询。

两类写入：
    expense  —— 消费记录（打字或截图录入）
    finance  —— 财务情况快照（时点）

设计原则：Skill 管引导，脚本管兜底。
LLM 可能不守规则，所以任何写入都必须过这一层白名单校验。

用法：
    python3 validate.py init
    python3 validate.py categories
    python3 validate.py expense --json '{"occurred_at":"2026-10-01 12:30","category_l1":"餐饮","amount":38.5,"platform":"支付宝"}'
    python3 validate.py finance --json '{"snapshot_at":"2026-09-30","alipay":1200.5,"wechat":300}'

退出码：0 成功 / 1 校验失败 / 2 重复 / 3 用法错误
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(__import__("os").environ.get("PC_DB", ROOT / "data" / "copilot.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
CATEGORIES_PATH = ROOT / "config" / "categories.yaml"

FALLBACK_CATEGORY = "其他"
DEFAULT_PLATFORM = "其他"


class ValidationError(Exception):
    pass


class DuplicateError(Exception):
    def __init__(self, existing_id: int, detail: dict):
        super().__init__(f"疑似重复，已存在记录 id={existing_id}")
        self.existing_id = existing_id
        self.detail = detail


# ---------------------------------------------------------------------
# 配置加载：极简 YAML 解析，避免引入 PyYAML
# ---------------------------------------------------------------------
def load_config() -> tuple[list[str], list[str]]:
    """返回 (一级分类列表, 平台列表)。"""
    if not CATEGORIES_PATH.exists():
        raise SystemExit(f"分类词表不存在: {CATEGORIES_PATH}")
    text = CATEGORIES_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()

    cats: list[str] = []
    platforms: list[str] = []
    section = None
    for line in lines:
        if re.match(r"^primary:", line):
            section = "primary"
            continue
        if re.match(r"^platforms:", line):
            section = "platforms"
            continue
        if re.match(r"^[a-z_]+:", line):       # 其他顶层键
            section = None
            continue
        if section == "primary":
            m = re.match(r"^\s*-\s*key:\s*(.+?)\s*$", line)
            if m:
                cats.append(m.group(1).strip().strip("'\""))
        elif section == "platforms":
            m = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if m:
                platforms.append(m.group(1).strip().strip("'\""))

    if not cats:
        raise SystemExit("分类词表中未解析到任何一级分类")
    if not platforms:
        raise SystemExit("分类词表中未解析到任何平台")
    return cats, platforms


# ---------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    if not SCHEMA_PATH.exists():
        raise SystemExit(f"schema 不存在: {SCHEMA_PATH}")
    conn = connect()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    print(f"已初始化: {DB_PATH}")


def log_action(conn, skill, table, target_id, action, detail=None) -> None:
    conn.execute(
        "INSERT INTO ingest_log (skill, target_table, target_id, action, detail)"
        " VALUES (?,?,?,?,?)",
        (skill, table, target_id, action,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


# ---------------------------------------------------------------------
# 校验工具
# ---------------------------------------------------------------------
def parse_amount(raw, field: str = "金额") -> float:
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
        if not cleaned:
            raise ValidationError(f"无法解析{field}: {raw!r}")
        val = float(cleaned)
    else:
        raise ValidationError(f"{field}类型不支持: {type(raw).__name__}")
    if val < 0:
        raise ValidationError(f"{field}不能为负: {val}（负债填正数表示欠款金额）")
    return round(val, 2)


def normalize_time(raw, field: str = "时间") -> str:
    """统一成 ISO8601。缺失时拒绝，不默认取当前时间（会造成时间错位）。"""
    if not raw:
        raise ValidationError(f"{field}缺失；必须由截图或用户提供，不得默认当前时间")
    s = str(raw).strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%d", "%m-%d", "%Y-%m-%dT%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%m-%d":                       # 只有月日时补当年
                dt = dt.replace(year=datetime.now().year)
            return dt.strftime("%Y-%m-%dT%H:%M:%S" if "%H" in fmt else "%Y-%m-%d")
        except ValueError:
            continue
    raise ValidationError(f"无法解析{field}: {raw!r}")


def validate_category(raw, allowed: list[str]) -> tuple[str, bool]:
    if raw and str(raw).strip() in allowed:
        return str(raw).strip(), False
    return FALLBACK_CATEGORY, True


def validate_platform(raw, allowed: list[str]) -> str:
    if raw and str(raw).strip() in allowed:
        return str(raw).strip()
    if raw and str(raw).strip():
        return DEFAULT_PLATFORM
    return DEFAULT_PLATFORM


# ---------------------------------------------------------------------
# 写入：消费
# ---------------------------------------------------------------------
def insert_expense(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    cats, platforms = load_config()
    conn = connect()
    try:
        amount = parse_amount(payload.get("amount"))
        occurred_at = normalize_time(payload.get("occurred_at"), "消费时间")
        category_l1, fell_back = validate_category(payload.get("category_l1"), cats)
        category_l2 = (payload.get("category_l2") or "").strip() or None
        platform = validate_platform(payload.get("platform"), platforms)
        source = payload.get("source") or "text"
        if source not in ("text", "image"):
            source = "text"

        detail = {
            "amount": amount, "occurred_at": occurred_at,
            "category_l1": category_l1, "category_l2": category_l2,
            "platform": platform, "source": source,
            "requested_category": payload.get("category_l1"),
            "category_fallback": fell_back,
        }
        if fell_back:
            detail["reason"] = (
                f"一级分类 {payload.get('category_l1')!r} 不在受控词表中，已回退到 {FALLBACK_CATEGORY}"
            )

        dup = conn.execute(
            "SELECT id FROM expenses WHERE amount=? AND occurred_at=? AND category_l1=? AND platform=?",
            (amount, occurred_at, category_l1, platform),
        ).fetchone()
        if dup:
            log_action(conn, "expense-capture", "expenses", dup["id"], "reject_duplicate", detail)
            if not dry_run:
                conn.commit()
            raise DuplicateError(dup["id"], detail)

        if dry_run:
            return -1, detail

        cur = conn.execute(
            """INSERT INTO expenses
               (occurred_at, category_l1, category_l2, amount, platform, source, raw_desc, note)
               VALUES (?,?,?,?,?,?,?,?)""",
            (occurred_at, category_l1, category_l2, amount, platform, source,
             payload.get("raw_desc"), payload.get("note")),
        )
        row_id = cur.lastrowid
        log_action(conn, "expense-capture", "expenses", row_id, "insert", detail)
        conn.commit()
        return row_id, detail
    finally:
        conn.close()


# ---------------------------------------------------------------------
# 写入：财务快照
# ---------------------------------------------------------------------
FINANCE_FIELDS = [
    ("alipay", "支付宝"), ("wechat", "微信"), ("bank_balance", "银行卡余额"),
    ("cash", "现金总额"), ("wealth", "理财"), ("housing_fund", "住房公积金"),
    ("huabei", "花呗"), ("jd_baitiao", "京东白条"), ("credit_card", "信用卡"),
    ("loan_outstanding", "贷款待还总额"),
]


def insert_finance(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    conn = connect()
    try:
        snapshot_at = normalize_time(payload.get("snapshot_at"), "快照时点")
        # 快照只到日
        snapshot_at = snapshot_at[:10]

        # 只收集"用户实际提供了"的字段。未提供的字段：
        #   - 新建快照时视为 0（该账户无余额）
        #   - 更新已有快照时保持原值不动（部分更新，避免抹掉其他账户）
        provided: dict[str, float] = {}
        for field, label in FINANCE_FIELDS:
            raw = payload.get(field)
            if raw is not None and raw != "":
                provided[field] = parse_amount(raw, label)

        if dry_run:
            totals = dict.fromkeys((f for f, _ in FINANCE_FIELDS), 0.0)
            totals.update(provided)
            total_assets = round(sum(totals[f] for f, _ in FINANCE_FIELDS[:6]), 2)
            total_debt = round(sum(totals[f] for f, _ in FINANCE_FIELDS[6:]), 2)
            return -1, {**provided, "snapshot_at": snapshot_at,
                        "total_assets": total_assets, "total_debt": total_debt,
                        "net_worth": round(total_assets - total_debt, 2),
                        "partial": True}

        existing = conn.execute(
            "SELECT id FROM finance_snapshots WHERE snapshot_at=?", (snapshot_at,)
        ).fetchone()

        if existing:
            # 同一天重复提交 -> 部分更新，只覆盖本次提供的字段
            if not provided:
                raise ValidationError("没有提供任何余额字段，无法更新快照")
            sets = ",".join(f"{c}=?" for c in provided)
            conn.execute(
                f"UPDATE finance_snapshots SET {sets}, source=?, raw_desc=?, note=?"
                f" WHERE snapshot_at=?",
                (*provided.values(), payload.get("source") or "text",
                 payload.get("raw_desc"), payload.get("note"), snapshot_at),
            )
            row_id = existing["id"]
            action = "update"
            detail_extra = {"updated_fields": list(provided)}
        else:
            cols = [f for f, _ in FINANCE_FIELDS]
            vals = [provided.get(f, 0.0) for f in cols]
            placeholders = ",".join("?" for _ in cols)
            cur = conn.execute(
                f"INSERT INTO finance_snapshots (snapshot_at, {','.join(cols)}, source, raw_desc, note)"
                f" VALUES (?,{placeholders},?,?,?)",
                (snapshot_at, *vals, payload.get("source") or "text",
                 payload.get("raw_desc"), payload.get("note")),
            )
            row_id = cur.lastrowid
            action = "insert"
            detail_extra = {}

        # 回读实际落库的值（含生成列）
        row = conn.execute(
            "SELECT * FROM finance_snapshots WHERE id=?", (row_id,)
        ).fetchone()
        detail = {k: row[k] for k in row.keys() if k not in ("created_at",)}
        detail["action"] = action
        detail.update(detail_extra)

        log_action(conn, "finance-snapshot", "finance_snapshots", row_id, action, detail)
        conn.commit()
        detail["action"] = action
        return row_id, detail
    finally:
        conn.close()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="private-copilot 写入校验")
    ap.add_argument("kind", choices=["init", "expense", "finance", "categories"])
    ap.add_argument("--json", dest="payload", help="待写入记录的 JSON")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写入")
    args = ap.parse_args()

    if args.kind == "init":
        init_db()
        return 0

    if args.kind == "categories":
        cats, platforms = load_config()
        print("一级分类:", " ".join(cats))
        print("平台:", " ".join(platforms))
        return 0

    if not args.payload:
        print("错误：需要 --json", file=sys.stderr)
        return 3
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"错误：JSON 解析失败 - {e}", file=sys.stderr)
        return 3

    try:
        if args.kind == "expense":
            row_id, detail = insert_expense(payload, args.dry_run)
        else:
            row_id, detail = insert_finance(payload, args.dry_run)
    except DuplicateError as e:
        print(json.dumps({"ok": False, "reason": "duplicate",
                          "existing_id": e.existing_id, "detail": e.detail},
                         ensure_ascii=False))
        return 2
    except ValidationError as e:
        print(json.dumps({"ok": False, "reason": "invalid", "error": str(e)},
                         ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "id": row_id, "dry_run": args.dry_run,
                      "detail": detail}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

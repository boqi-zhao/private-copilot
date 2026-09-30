#!/usr/bin/env python3
"""private-copilot 写入校验与落库。

设计原则：Skill 管引导，脚本管兜底。
LLM 可能不守规则，所以任何写入都必须过这一层白名单校验。

用法：
    # 校验并写入一条消费记录
    python3 validate.py expense --json '{"amount": 38.5, "merchant": "星巴克", ...}'

    # 只校验不写入
    python3 validate.py expense --json '...' --dry-run

    # 健康指标
    python3 validate.py health --json '{"metric_key": "hba1c", "value": 5.6, ...}'

    # 初始化数据库
    python3 validate.py init

退出码：0 成功 / 1 校验失败 / 2 重复 / 3 用法错误
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("PC_DB", ROOT / "data" / "copilot.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
CATEGORIES_PATH = ROOT / "config" / "categories.yaml"
RECEIPTS_DIR = ROOT / "data" / "receipts"

FALLBACK_CATEGORY = "其他"


# ---------------------------------------------------------------------
# 配置加载：categories.yaml 只需要极简解析，避免引入 PyYAML 依赖
# ---------------------------------------------------------------------
def load_categories() -> list[str]:
    if not CATEGORIES_PATH.exists():
        raise SystemExit(f"分类词表不存在: {CATEGORIES_PATH}")
    keys: list[str] = []
    for line in CATEGORIES_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*-\s*key:\s*(.+?)\s*$", line)
        if m:
            keys.append(m.group(1).strip().strip("'\""))
    if not keys:
        raise SystemExit("分类词表中未解析到任何 key")
    return keys


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


def log_action(conn, media_id, skill, table, target_id, action, detail=None) -> None:
    conn.execute(
        "INSERT INTO ingest_log (media_id, skill, target_table, target_id, action, detail)"
        " VALUES (?,?,?,?,?,?)",
        (media_id, skill, table, target_id, action,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


# ---------------------------------------------------------------------
# 媒体落盘（带 sha256 去重）
# ---------------------------------------------------------------------
def register_media(conn, image_path: str | None, caption: str | None,
                   origin: str = "feishu") -> int | None:
    """把图片复制到 data/receipts 并登记。返回 media.id。

    同一 sha256 已存在时直接返回既有 id，不重复存储。
    """
    if not image_path:
        return None
    src = Path(image_path).expanduser()
    if not src.exists():
        raise ValidationError(f"图片不存在: {src}")

    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    row = conn.execute("SELECT id FROM media WHERE sha256 = ?", (digest,)).fetchone()
    if row:
        return row["id"]

    # 按年月分目录，避免单目录文件过多
    now = datetime.now()
    sub = Path(f"{now:%Y}") / f"{now:%m}"
    dest_dir = RECEIPTS_DIR / sub
    dest_dir.mkdir(parents=True, exist_ok=True)

    suffix = src.suffix.lower() or ".jpg"
    dest = dest_dir / f"{digest[:16]}{suffix}"
    if not dest.exists():
        shutil.copy2(src, dest)

    rel = str(dest.relative_to(RECEIPTS_DIR))
    cur = conn.execute(
        "INSERT INTO media (sha256, rel_path, mime_type, byte_size, origin, caption)"
        " VALUES (?,?,?,?,?,?)",
        (digest, rel, guess_mime(suffix), src.stat().st_size, origin, caption),
    )
    return cur.lastrowid


def guess_mime(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".heic": "image/heic",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")


# ---------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------
class ValidationError(Exception):
    pass


def parse_amount(raw) -> float:
    if isinstance(raw, (int, float)):
        val = float(raw)
    elif isinstance(raw, str):
        cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", ""))
        if not cleaned:
            raise ValidationError(f"无法解析金额: {raw!r}")
        val = float(cleaned)
    else:
        raise ValidationError(f"金额类型不支持: {type(raw).__name__}")
    if val < 0:
        raise ValidationError(f"金额不能为负: {val}")
    return round(val, 2)


def normalize_time(raw: str | None) -> str:
    """统一成 ISO8601。缺失时拒绝，不默认取当前时间（会造成时间错位）。"""
    if not raw:
        raise ValidationError("occurred_at 缺失；交易时间必须由截图或用户提供，不得默认当前时间")
    s = str(raw).strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    raise ValidationError(f"无法解析时间: {raw!r}")


def validate_category(raw: str | None, allowed: list[str]) -> tuple[str, bool]:
    """返回 (最终分类, 是否回退)。非法分类回退到 fallback。"""
    if raw and raw.strip() in allowed:
        return raw.strip(), False
    return FALLBACK_CATEGORY, True


# ---------------------------------------------------------------------
# 写入：消费
# ---------------------------------------------------------------------
def insert_expense(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    allowed = load_categories()
    conn = connect()
    try:
        amount = parse_amount(payload.get("amount"))
        occurred_at = normalize_time(payload.get("occurred_at"))
        merchant = (payload.get("merchant") or "").strip() or None
        category, fell_back = validate_category(payload.get("category"), allowed)

        detail = {
            "amount": amount, "merchant": merchant,
            "requested_category": payload.get("category"),
            "final_category": category, "category_fallback": fell_back,
            "occurred_at": occurred_at,
        }

        if fell_back:
            detail["reason"] = (
                f"分类 {payload.get('category')!r} 不在受控词表中，已回退到 {FALLBACK_CATEGORY}"
            )

        # 业务去重：金额 + 商户 + 分钟级时间
        dup = conn.execute(
            "SELECT id FROM transactions WHERE direction='expense' AND amount=?"
            " AND IFNULL(merchant,'')=IFNULL(?,'') AND substr(occurred_at,1,16)=?",
            (amount, merchant, occurred_at[:16]),
        ).fetchone()
        if dup:
            log_action(conn, None, "expense-capture", "transactions", dup["id"],
                       "reject_duplicate", detail)
            if not dry_run:
                conn.commit()
            raise DuplicateError(dup["id"], detail)

        if dry_run:
            return -1, detail

        media_id = register_media(conn, payload.get("image_path"),
                                  payload.get("raw_desc"),
                                  payload.get("origin", "feishu"))

        cur = conn.execute(
            """INSERT INTO transactions
               (amount, currency, direction, merchant, category, category_src,
                occurred_at, pay_method, raw_desc, media_id, confidence,
                needs_review, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                amount,
                payload.get("currency") or "CNY",
                payload.get("direction") or "expense",
                merchant,
                category,
                payload.get("category_src") or ("image" if payload.get("image_path") else "user_text"),
                occurred_at,
                payload.get("pay_method"),
                payload.get("raw_desc"),
                media_id,
                payload.get("confidence"),
                1 if (fell_back or payload.get("needs_review")) else 0,
                payload.get("note"),
            ),
        )
        tx_id = cur.lastrowid
        log_action(conn, media_id, "expense-capture", "transactions", tx_id, "insert", detail)
        conn.commit()
        detail["media_id"] = media_id
        return tx_id, detail
    finally:
        conn.close()


class DuplicateError(Exception):
    def __init__(self, existing_id: int, detail: dict):
        super().__init__(f"疑似重复，已存在记录 id={existing_id}")
        self.existing_id = existing_id
        self.detail = detail


# ---------------------------------------------------------------------
# 写入：健康指标
# ---------------------------------------------------------------------
def resolve_metric_key(conn, raw: str | None) -> str:
    if not raw or not str(raw).strip():
        raise ValidationError("metric_key 缺失")
    key = str(raw).strip().lower()
    row = conn.execute(
        "SELECT metric_key FROM metric_aliases WHERE lower(alias)=?", (key,)
    ).fetchone()
    return row["metric_key"] if row else key


def insert_health(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    conn = connect()
    try:
        metric_key = resolve_metric_key(conn, payload.get("metric_key"))
        if payload.get("value") is None:
            raise ValidationError("value 缺失")
        value = float(payload["value"])
        measured_at = normalize_time(payload.get("measured_at"))

        ref_low = payload.get("ref_low")
        ref_high = payload.get("ref_high")
        abnormal = payload.get("abnormal")
        if abnormal is None and (ref_low is not None or ref_high is not None):
            if ref_high is not None and value > float(ref_high):
                abnormal = 1
            elif ref_low is not None and value < float(ref_low):
                abnormal = -1
            else:
                abnormal = 0

        detail = {
            "metric_key": metric_key, "value": value,
            "unit": payload.get("unit"), "measured_at": measured_at,
            "abnormal": abnormal,
        }
        if dry_run:
            return -1, detail

        media_id = register_media(conn, payload.get("image_path"),
                                  payload.get("raw_desc"),
                                  payload.get("origin", "feishu"))

        cur = conn.execute(
            """INSERT INTO health_metrics
               (metric_key, display_name, value, unit, ref_low, ref_high, abnormal,
                measured_at, panel, institution, media_id, confidence, source_ref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                metric_key, payload.get("display_name"), value, payload.get("unit"),
                ref_low, ref_high, abnormal, measured_at,
                payload.get("panel"), payload.get("institution"),
                media_id, payload.get("confidence"), payload.get("source_ref"),
            ),
        )
        hm_id = cur.lastrowid
        log_action(conn, media_id, "health-report", "health_metrics", hm_id, "insert", detail)
        conn.commit()
        detail["media_id"] = media_id
        return hm_id, detail
    finally:
        conn.close()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="private-copilot 写入校验")
    ap.add_argument("kind", choices=["init", "expense", "health", "categories"])
    ap.add_argument("--json", dest="payload", help="待写入记录的 JSON")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写入")
    args = ap.parse_args()

    if args.kind == "init":
        init_db()
        return 0

    if args.kind == "categories":
        print("\n".join(load_categories()))
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
            row_id, detail = insert_health(payload, args.dry_run)
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

#!/usr/bin/env python3
"""private-copilot 写入与查询。

写入：
    expense         —— 新增消费记录（打字或截图录入）
    expense-update  —— 修改已有消费记录（用户在对话里回复纠正）
    expense-last    —— 查看最近 5 笔，用于确定要改哪一笔
    finance         —— 财务情况快照（时点）
    health          —— 健康指标（体检 / 化验 / 身体测量）

设计原则：Skill 管引导，脚本管兜底。
LLM 可能不守规则，所以任何写入都必须过这一层白名单校验。

用法：
    python3 validate.py init
    python3 validate.py categories
    python3 validate.py expense --json '{"occurred_at":"2026-10-01 12:30","category_l1":"餐饮","amount":38.5,"platform":"支付宝"}'
    python3 validate.py expense-update --json '{"id":20,"category_l1":"交通"}'
    python3 validate.py expense-last
    python3 validate.py finance --json '{"snapshot_at":"2026-09-30","alipay":1200.5,"wechat":300}'
    python3 validate.py health --json '{"metric_key":"腰围","value":113,"unit":"cm","measured_at":"2026-10-01"}'

退出码：0 成功 / 1 校验失败 / 2 重复 / 3 用法错误 / 4 目标不唯一
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


def parse_number(raw, field: str = "数值") -> float:
    """解析数值。与 parse_amount 不同，这里**允许负数**（部分指标可为负）。"""
    if isinstance(raw, bool) or raw is None:
        raise ValidationError(f"无法解析{field}: {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        cleaned = re.sub(r"[^\d.\-+]", "", raw.replace(",", ""))
        if not cleaned:
            raise ValidationError(f"无法解析{field}: {raw!r}")
        try:
            return float(cleaned)
        except ValueError:
            raise ValidationError(f"无法解析{field}: {raw!r}")
    raise ValidationError(f"{field}类型不支持: {type(raw).__name__}")


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
# 修改：消费（回复纠正）
#
# 用户在飞书里回一句「分类改成交通」，agent 解析出要改的字段后走这里。
# 关键设计：
#   * 只允许改白名单字段，不允许 agent 直接写 SQL
#   * 走与 insert 完全相同的分类/平台/金额校验
#   * 改完写 ingest_log(action='update') 留痕，记录改前改后
# ---------------------------------------------------------------------
UPDATABLE_FIELDS = ("occurred_at", "category_l1", "category_l2",
                    "amount", "platform", "raw_desc", "note")

# 超过这个小时数就必须二次确认（由 Skill 层执行），脚本只做提示
STALE_EDIT_HOURS = 24


class AmbiguousTargetError(Exception):
    """无法唯一确定要改哪一笔。"""

    def __init__(self, candidates: list[dict]):
        super().__init__("存在多笔候选，请指定 id")
        self.candidates = candidates


def find_expense_target(conn, payload: dict) -> dict:
    """定位要修改的记录。

    target_id 优先；没有则取最近一笔。允许多种参数名，避免 agent 猜错键名。
    """
    raw_id = payload.get("id")
    if raw_id is None:
        raw_id = payload.get("target_id")

    if raw_id not in (None, ""):
        try:
            target_id = int(str(raw_id).strip())
        except (TypeError, ValueError):
            raise ValidationError(f"id 不是整数: {raw_id!r}")
        row = conn.execute("SELECT * FROM expenses WHERE id=?", (target_id,)).fetchone()
        if not row:
            raise ValidationError(f"记录不存在: id={target_id}")
        return dict(row)

    # 没给 id：默认最近一笔，但如果刚写入后又被插入过，用户可能指的是别的
    rows = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 2").fetchall()
    if not rows:
        raise ValidationError("账本里还没有任何记录，无法修改")
    return dict(rows[0])


def update_expense(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    cats, platforms = load_config()
    conn = connect()
    try:
        before = find_expense_target(conn, payload)

        sets: list[str] = []
        values: list = []
        changes: dict[str, dict] = {}

        for field in UPDATABLE_FIELDS:
            if field not in payload:
                continue
            raw = payload.get(field)

            if field == "occurred_at":
                new_val = normalize_time(raw, "消费时间")
            elif field == "category_l1":
                new_val, fell_back = validate_category(raw, cats)
                if fell_back:
                    raise ValidationError(
                        f"一级分类 {raw!r} 不在受控词表中（受控词表：{'、'.join(cats)}）"
                    )
            elif field == "category_l2":
                new_val = (str(raw).strip() or None) if raw is not None else None
            elif field == "amount":
                new_val = parse_amount(raw)
            elif field == "platform":
                if not raw or str(raw).strip() not in platforms:
                    raise ValidationError(
                        f"支付平台 {raw!r} 不在允许列表（{'、'.join(platforms)}）"
                    )
                new_val = str(raw).strip()
            else:
                new_val = (str(raw).strip() or None) if raw is not None else None

            old_val = before.get(field)
            # 归一化后比较，避免 "42" vs 42.0 误判成有改动
            if old_val == new_val:
                continue
            changes[field] = {"from": old_val, "to": new_val}
            sets.append(f"{field}=?")
            values.append(new_val)

        if not sets:
            return before["id"], {"id": before["id"], "changed": {}, "noop": True,
                                  "message": "没有需要修改的字段"}

        # 改完是否会与已有记录重复（排除自己）
        merged = dict(before)
        for f, c in changes.items():
            merged[f] = c["to"]
        dup = conn.execute(
            "SELECT id FROM expenses WHERE amount=? AND occurred_at=? AND category_l1=?"
            " AND platform=? AND id<>?",
            (merged["amount"], merged["occurred_at"], merged["category_l1"],
             merged["platform"], before["id"]),
        ).fetchone()
        if dup:
            log_action(conn, "expense-capture", "expenses", dup["id"], "reject_duplicate",
                       {"reason": "修改后与其他记录重复", "target_id": before["id"],
                        "changes": changes})
            if not dry_run:
                conn.commit()
            raise DuplicateError(dup["id"], {"changes": changes,
                                             "reason": "修改后会与这条重复"})

        detail = {
            "id": before["id"], "changes": changes, "dry_run": dry_run,
            "before": {k: before.get(k) for k in UPDATABLE_FIELDS},
        }
        if dry_run:
            detail["after"] = merged
            return before["id"], detail

        values.append(before["id"])
        conn.execute(f"UPDATE expenses SET {', '.join(sets)} WHERE id=?", values)
        detail["after"] = {k: merged.get(k) for k in UPDATABLE_FIELDS}
        log_action(conn, "expense-capture", "expenses", before["id"], "update", detail)
        conn.commit()
        return before["id"], detail
    finally:
        conn.close()


# ---------------------------------------------------------------------
# 写入：健康指标（体检 / 化验 / 身体测量）
#
# 与 expense 的差异：
#   * 数值允许负数，单位必须一并记录（同一指标不同单位不可直接比较）。
#   * metric_key 先过 metric_aliases 归一，表里没有的原样保留。
#   * abnormal 由 ref_low/ref_high 自动推导，只有报告标了箭头又没有区间时才手工传。
# ---------------------------------------------------------------------
def insert_health(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    conn = connect()
    try:
        raw_key = (payload.get("metric_key") or payload.get("name") or "").strip()
        if not raw_key:
            raise ValidationError("metric_key 缺失（指标的标准化名称）")

        alias = conn.execute(
            "SELECT metric_key, unit FROM metric_aliases WHERE alias=?", (raw_key,)
        ).fetchone()
        if alias:
            metric_key, alias_unit = alias["metric_key"], alias["unit"]
        else:
            metric_key, alias_unit = raw_key, None   # 别名表没有 -> 原样保留

        value = parse_number(payload.get("value"), "指标数值")

        raw_time = payload.get("measured_at") or payload.get("date")
        if raw_time:
            measured_at = normalize_time(raw_time, "测量时间")[:10]
        else:
            measured_at = datetime.now().strftime("%Y-%m-%d")

        def opt_float(name: str):
            raw = payload.get(name)
            if raw is None or raw == "":
                return None
            return parse_number(raw, name)

        ref_low, ref_high = opt_float("ref_low"), opt_float("ref_high")

        abnormal = payload.get("abnormal")
        if abnormal is None and (ref_low is not None or ref_high is not None):
            if ref_low is not None and value < ref_low:
                abnormal = -1
            elif ref_high is not None and value > ref_high:
                abnormal = 1
            else:
                abnormal = 0

        source = payload.get("source") or "text"
        if source not in ("text", "image"):
            source = "text"

        detail = {
            "metric_key": metric_key,
            "display_name": payload.get("display_name") or raw_key,
            "value": value, "unit": (payload.get("unit") or alias_unit or "").strip() or None,
            "ref_low": ref_low, "ref_high": ref_high, "abnormal": abnormal,
            "measured_at": measured_at, "panel": payload.get("panel"),
            "institution": payload.get("institution"),
            "source_ref": payload.get("source_ref"),
            "image_path": payload.get("image_path"), "source": source,
        }
        if not alias:
            detail["alias_missing"] = True

        if dry_run:
            return -1, detail

        cur = conn.execute(
            """INSERT INTO health_metrics
               (metric_key, display_name, value, unit, ref_low, ref_high, abnormal,
                measured_at, panel, institution, source_ref, image_path, source, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (metric_key, detail["display_name"], value, detail["unit"], ref_low, ref_high,
             abnormal, measured_at, payload.get("panel"), payload.get("institution"),
             payload.get("source_ref"), payload.get("image_path"), source,
             payload.get("note")),
        )
        row_id = int(cur.lastrowid)
        log_action(conn, "health-report", "health_metrics", row_id, "insert", detail)
        conn.commit()
        return row_id, detail
    finally:
        conn.close()


# ---------------------------------------------------------------------
# 写入：身体尺寸（围度）
#
# 与 health 的差异：
#   * 宽表而非长表：一次测量 = 一行，六项围度各占一列。
#   * 允许缺项（NULL），**绝不补 0** —— 没量就是没量。
#   * 单位固定 cm，不接受单位换算，也不接受明显离谱的值。
# ---------------------------------------------------------------------
BODY_FIELDS = [
    ("waist", "腰围"), ("hip", "臀围"), ("chest", "胸围"),
    ("bicep", "大臂围"), ("thigh", "大腿围"), ("calf", "小腿围"),
]
BODY_MIN_CM = 10.0      # 比这还小的围度一定是录错了（把 cm 当 m 之类的）
BODY_MAX_CM = 300.0


def insert_body(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    conn = connect()
    try:
        raw_time = payload.get("measured_at") or payload.get("date") or payload.get("时间")
        measured_at = normalize_time(raw_time, "测量日期")[:10]

        values: dict[str, float] = {}
        for col, label in BODY_FIELDS:
            raw = payload.get(col, payload.get(label))
            if raw is None or raw == "":
                continue
            val = parse_number(raw, label)
            if not (BODY_MIN_CM <= val <= BODY_MAX_CM):
                raise ValidationError(
                    f"{label} {val} 超出合理范围（{BODY_MIN_CM:.0f}-{BODY_MAX_CM:.0f} cm），"
                    "请确认是否把单位写错")
            values[col] = round(val, 1)

        if not values:
            raise ValidationError(
                "至少要提供一项围度（" + " / ".join(label for _, label in BODY_FIELDS) + "）")

        source = payload.get("source") or "text"
        if source not in ("text", "image"):
            source = "text"

        detail = {"measured_at": measured_at, "source": source, **values}

        existing = conn.execute(
            "SELECT * FROM body_measurements WHERE measured_at=?", (measured_at,)
        ).fetchone()

        if dry_run:
            detail["action"] = "update" if existing else "insert"
            if existing:
                detail["existing"] = {c: existing[c] for c in dict(BODY_FIELDS) if existing[c] is not None}
            return (int(existing["id"]) if existing else -1), detail

        if existing:
            # 同一天重复上报：只更新本次给出的项，未提及的项**保持原值**。
            # （与 finance 快照同一原则：局部更新不能把别的字段清零）
            sets = ",".join(f"{c}=?" for c in values)
            conn.execute(
                f"UPDATE body_measurements SET {sets}, source=?, raw_desc=COALESCE(?, raw_desc)"
                " WHERE measured_at=?",
                [*values.values(), source, payload.get("raw_desc"), measured_at],
            )
            row_id = int(existing["id"])
            detail["action"] = "update"
            log_action(conn, "body-measure", "body_measurements", row_id, "update", detail)
        else:
            cols = ["measured_at", *values.keys(), "source", "raw_desc", "note"]
            vals = [measured_at, *values.values(), source,
                    payload.get("raw_desc"), payload.get("note")]
            cur = conn.execute(
                f"INSERT INTO body_measurements ({','.join(cols)})"
                f" VALUES ({','.join('?' * len(cols))})", vals)
            row_id = int(cur.lastrowid)
            detail["action"] = "insert"
            log_action(conn, "body-measure", "body_measurements", row_id, "insert", detail)

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


# ---- 插入：食物照片 ----
#
# 与 expense 的关键差异：
#   * 消费记录金额必填；食物照片多数没有金额（家里做的饭），所以只要求时间和照片。
#   * 照片文件由 store_photo() 落盘，表里存相对路径。
#   * 去重按 photo_path（同一张图不允许入库两次），不按内容字段。

PHOTO_ROOT_REL = "data/photos"          # 相对仓库根
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
MAX_PHOTO_BYTES = 25 * 1024 * 1024      # 单张 25MB 上限


def _hash_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def store_photo(src: str, eaten_at: str) -> str:
    """把照片从来源路径复制到 data/photos/YYYY/MM/，返回相对仓库根的路径。

    为什么必须复制而不是直接用 inbound 路径：
        ~/.openclaw/media/inbound/ 是 OpenClaw 自己的暂存区，
        不保证长期存在（重启/清理会没）。食物照片是要长期留存的，
        必须落到我们自己的目录。

    文件名用内容哈希，天然去重：同一张图重复发只会有一份文件。
    """
    import shutil

    src_path = Path(src).expanduser()
    if not src_path.exists():
        raise ValidationError(f"照片文件不存在: {src_path}")
    if not src_path.is_file():
        raise ValidationError(f"不是文件: {src_path}")

    size = src_path.stat().st_size
    if size == 0:
        raise ValidationError(f"照片是空文件: {src_path}")
    if size > MAX_PHOTO_BYTES:
        raise ValidationError(
            f"照片过大（{size/1024/1024:.1f}MB），上限 {MAX_PHOTO_BYTES//1024//1024}MB")

    ext = src_path.suffix.lower()
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValidationError(
            f"不支持的图片格式 {ext!r}；允许: {', '.join(sorted(ALLOWED_IMAGE_EXT))}")

    year, month = eaten_at[:4], eaten_at[5:7]
    dest_dir = ROOT / PHOTO_ROOT_REL / year / month
    dest_dir.mkdir(parents=True, exist_ok=True)

    digest = _hash_file(src_path)
    dest_name = f"{eaten_at[:10].replace('-', '')}-{digest}{ext}"
    dest = dest_dir / dest_name

    if not dest.exists():
        # 先写临时文件再 rename，避免复制一半被中断留下坏文件
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copy2(src_path, tmp)
        tmp.replace(dest)

    return str(dest.relative_to(ROOT))


def insert_meal(payload: dict, dry_run: bool = False) -> tuple[int, dict]:
    """写入一条食物记录。payload 需要 eaten_at + photo（本地路径）。

    时间可以缺省：食物照片是"随拍随发"，默认取当前时间合理
    （这与消费记录不同 —— 消费截图有交易时间，不该默认 now）。
    """
    raw_time = payload.get("eaten_at") or payload.get("time")
    if raw_time:
        eaten_at = normalize_time(raw_time, "用餐时间")
    else:
        eaten_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    note = (payload.get("note") or "").strip() or None

    photo = (payload.get("photo") or payload.get("photo_path") or "").strip()
    source = payload.get("source") or ("image" if photo else "text")
    if source not in ("image", "text"):
        raise ValidationError(f"source 只能是 image 或 text，收到 {source!r}")

    if source == "image" and not photo:
        raise ValidationError("照片记录必须提供 photo 路径")
    if source == "text" and not note:
        raise ValidationError("纯文字记录必须提供 note（否则这行没任何信息）")

    # 落盘（dry_run 时只校验来源文件存在，不真复制）
    if photo:
        if dry_run:
            p = Path(photo).expanduser()
            if not p.exists():
                raise ValidationError(f"照片文件不存在: {p}")
            photo_rel = f"(dry-run 未落盘) {photo}"
        else:
            photo_rel = store_photo(photo, eaten_at)
    else:
        photo_rel = None

    detail = {"eaten_at": eaten_at, "photo_path": photo_rel, "note": note,
              "source": source}

    if dry_run:
        return 0, detail

    conn = connect()
    try:
        existing = conn.execute(
            "SELECT id FROM meal_photos WHERE photo_path = ?", (photo_rel,)
        ).fetchone()
        if existing:
            log_action(conn, "food-capture", "meal_photos", existing["id"],
                       "reject_duplicate", {"photo_path": photo_rel})
            conn.commit()
            raise DuplicateError(existing["id"], detail)

        try:
            cur = conn.execute(
                "INSERT INTO meal_photos (eaten_at, photo_path, note, source)"
                " VALUES (?,?,?,?)",
                (eaten_at, photo_rel, note, source))
        except sqlite3.IntegrityError as e:
            # 老库的 photo_path 还是 NOT NULL（没跑迁移）时，纯文字记录会撞这里。
            # 给出可操作的提示，而不是甩一个 traceback。
            if "photo_path" in str(e) and photo_rel is None:
                raise ValidationError(
                    "数据库的 meal_photos.photo_path 仍是 NOT NULL，纯文字记录写不进去。"
                    "请先执行: python3 scripts/migrate_meal_photo_nullable.py")
            raise
        row_id = int(cur.lastrowid)
        log_action(conn, "food-capture", "meal_photos", row_id, "insert", detail)
        conn.commit()
        return row_id, detail
    finally:
        conn.close()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="private-copilot 写入校验")
    ap.add_argument("kind",
                    choices=["init", "expense", "expense-update", "expense-last",
                             "finance", "meal", "meal-last", "health", "body",
                             "body-last", "categories"])
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

    if args.kind == "meal-last":
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, eaten_at, photo_path, note, source, created_at"
                " FROM meal_photos ORDER BY id DESC LIMIT 5"
            ).fetchall()
        finally:
            conn.close()
        print(json.dumps({"ok": True, "recent": [dict(r) for r in rows]},
                         ensure_ascii=False))
        return 0

    if args.kind == "body-last":
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, measured_at, waist, hip, chest, bicep, thigh, calf,"
                " source, note, created_at"
                " FROM body_measurements ORDER BY measured_at DESC LIMIT 5"
            ).fetchall()
        finally:
            conn.close()
        print(json.dumps({"ok": True, "recent": [dict(r) for r in rows]},
                         ensure_ascii=False))
        return 0

    if args.kind == "expense-last":
        # 给 agent 用：先看清楚最近几笔长什么样，再决定改哪一笔
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT id, occurred_at, category_l1, category_l2, amount,"
                " platform, raw_desc, created_at FROM expenses ORDER BY id DESC LIMIT 5"
            ).fetchall()
        finally:
            conn.close()
        print(json.dumps({"ok": True, "recent": [dict(r) for r in rows]},
                         ensure_ascii=False))
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
        elif args.kind == "expense-update":
            row_id, detail = update_expense(payload, args.dry_run)
        elif args.kind == "meal":
            row_id, detail = insert_meal(payload, args.dry_run)
        elif args.kind == "health":
            row_id, detail = insert_health(payload, args.dry_run)
        elif args.kind == "body":
            row_id, detail = insert_body(payload, args.dry_run)
        elif args.kind == "finance":
            row_id, detail = insert_finance(payload, args.dry_run)
        else:
            raise SystemExit(f"未知写入类型: {args.kind}")
    except DuplicateError as e:
        print(json.dumps({"ok": False, "reason": "duplicate",
                          "existing_id": e.existing_id, "detail": e.detail},
                         ensure_ascii=False))
        return 2
    except AmbiguousTargetError as e:
        print(json.dumps({"ok": False, "reason": "ambiguous",
                          "candidates": e.candidates}, ensure_ascii=False))
        return 4
    except ValidationError as e:
        print(json.dumps({"ok": False, "reason": "invalid", "error": str(e)},
                         ensure_ascii=False))
        return 1

    print(json.dumps({"ok": True, "id": row_id, "dry_run": args.dry_run,
                      "detail": detail}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

-- private-copilot 数据库 schema
-- SQLite 3。所有时间统一 ISO8601（本地时区 +08:00）。
--
-- 设计要点：
--   1. 图片一律落盘，库里只存路径 + sha256，不存 BLOB。
--   2. 每张表都带 source/source_ref，可追溯到原始截图。
--   3. 分类用受控词表，脚本层做白名单校验（见 validate.py）。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- 通用：原始媒体
-- ---------------------------------------------------------------
-- 一张截图可能对应多条记录（例如外卖单同时产生消费和饮食），
-- 所以媒体独立成表，业务表通过 media_id 引用。
CREATE TABLE IF NOT EXISTS media (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,      -- 去重依据
    rel_path      TEXT NOT NULL,             -- 相对 data/receipts 的路径
    mime_type     TEXT,
    byte_size     INTEGER,
    origin        TEXT,                      -- feishu / manual / import
    caption       TEXT,                      -- 用户随图发的描述
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_media_created ON media(created_at);

-- ---------------------------------------------------------------
-- 财务：交易
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    amount        REAL NOT NULL CHECK (amount >= 0),   -- 实付金额，正数
    currency      TEXT NOT NULL DEFAULT 'CNY',
    direction     TEXT NOT NULL DEFAULT 'expense'
                  CHECK (direction IN ('expense','income','transfer')),
    merchant      TEXT,
    category      TEXT NOT NULL,             -- 受控词表，见 config/categories.yaml
    category_src  TEXT
                  CHECK (category_src IN ('image','user_text','both','fallback')),
    occurred_at   TEXT NOT NULL,             -- 交易时间，非截图时间
    pay_method    TEXT,                      -- 支付宝/微信/银行卡/其他
    raw_desc      TEXT,                      -- 用户原始描述
    media_id      INTEGER REFERENCES media(id) ON DELETE SET NULL,
    confidence    REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    needs_review  INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0,1)),
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_tx_occurred  ON transactions(occurred_at);
CREATE INDEX IF NOT EXISTS idx_tx_category  ON transactions(category);
CREATE INDEX IF NOT EXISTS idx_tx_merchant  ON transactions(merchant);

-- 业务去重：同一笔钱可能被不同截图重复提交。
-- 用 (金额, 时间窗口, 商户) 做软去重，时间精确到分钟。
CREATE UNIQUE INDEX IF NOT EXISTS uniq_tx_dedupe
    ON transactions(amount, merchant, substr(occurred_at, 1, 16))
    WHERE direction = 'expense';

-- ---------------------------------------------------------------
-- 健康：体检 / 化验指标
-- ---------------------------------------------------------------
-- 关键设计：metric_key 是归一化后的标准名，display_name 保留原文。
-- 同一指标在不同报告里叫 A1c / HbA1c / 糖化血红蛋白，都映射到同一个 key。
CREATE TABLE IF NOT EXISTS health_metrics (
    id            INTEGER PRIMARY KEY,
    metric_key    TEXT NOT NULL,             -- 归一化标准名，如 hba1c
    display_name  TEXT,                      -- 报告原文，如 "糖化血红蛋白"
    value         REAL NOT NULL,
    unit          TEXT,
    ref_low       REAL,                      -- 参考区间下界
    ref_high      REAL,                      -- 参考区间上界
    abnormal      INTEGER                    -- 1 偏高 / -1 偏低 / 0 正常 / NULL 未知
                  CHECK (abnormal IS NULL OR abnormal IN (-1,0,1)),
    measured_at   TEXT NOT NULL,             -- 采样日期
    panel         TEXT,                      -- 所属报告，如 "2026-09 年度体检"
    institution   TEXT,
    media_id      INTEGER REFERENCES media(id) ON DELETE SET NULL,
    confidence    REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    source_ref    TEXT,                      -- 报告内定位，如 "第2页 血常规"
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_hm_key_time ON health_metrics(metric_key, measured_at);
CREATE INDEX IF NOT EXISTS idx_hm_panel    ON health_metrics(panel);

-- 指标别名表：让 LLM 判断不准时也能靠查表归一
CREATE TABLE IF NOT EXISTS metric_aliases (
    alias       TEXT PRIMARY KEY,
    metric_key  TEXT NOT NULL,
    unit        TEXT                         -- 该别名通常伴随的单位
);

-- ---------------------------------------------------------------
-- 饮食
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meals (
    id            INTEGER PRIMARY KEY,
    meal_type     TEXT NOT NULL
                  CHECK (meal_type IN ('breakfast','lunch','dinner','snack')),
    eaten_at      TEXT NOT NULL,
    description   TEXT NOT NULL,             -- 自然语言，如 "牛肉面加卤蛋"
    items         TEXT,                      -- JSON 数组：[{"name","qty","unit"}]
    kcal          REAL,
    protein_g     REAL,
    fat_g         REAL,
    carb_g        REAL,
    estimated     INTEGER NOT NULL DEFAULT 1 -- 1=模型估算 0=精确称重
                  CHECK (estimated IN (0,1)),
    media_id      INTEGER REFERENCES media(id) ON DELETE SET NULL,
    confidence    REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_meals_eaten ON meals(eaten_at);

-- ---------------------------------------------------------------
-- 运动
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workouts (
    id            INTEGER PRIMARY KEY,
    activity      TEXT NOT NULL,             -- 跑步/骑行/力量训练/游泳...
    started_at    TEXT NOT NULL,
    duration_min  REAL,
    distance_km   REAL,
    kcal          REAL,
    avg_hr        INTEGER,
    max_hr        INTEGER,
    note          TEXT,
    media_id      INTEGER REFERENCES media(id) ON DELETE SET NULL,
    confidence    REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_wo_started ON workouts(started_at);

-- ---------------------------------------------------------------
-- 审计：所有写入留痕，便于回溯模型判断错误
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_log (
    id            INTEGER PRIMARY KEY,
    media_id      INTEGER REFERENCES media(id) ON DELETE SET NULL,
    skill         TEXT NOT NULL,             -- expense-capture / health-report ...
    target_table  TEXT NOT NULL,
    target_id     INTEGER,
    action        TEXT NOT NULL
                  CHECK (action IN ('insert','update','reject_duplicate','reject_invalid','clarify')),
    detail        TEXT,                      -- JSON：原始提取结果、拒绝原因
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_ingest_media ON ingest_log(media_id);

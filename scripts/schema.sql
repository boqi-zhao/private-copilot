-- private-copilot 数据库 schema
-- SQLite 3。时间统一 ISO8601（本地时区）。
--
-- 设计要点：
--   1. 不存图片（媒体只保留可选的去重指纹，不落盘原图）。
--   2. 支持两种录入途径：打字（text）与截图（image），记在 source 字段。
--   3. 分类为两级：一级分类受控，二级分类自由。
--   4. 财务情况表是"时点快照"，资产总计为计算列，不手工录入。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------
-- 消费表
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY,
    occurred_at  TEXT NOT NULL,              -- 消费时间，非记录时间
    category_l1  TEXT NOT NULL,              -- 一级分类（受控词表）
    category_l2  TEXT,                       -- 二级分类（自由，可空）
    amount       REAL NOT NULL CHECK (amount >= 0),
    platform     TEXT NOT NULL
                 CHECK (platform IN ('支付宝','微信','现金','信用卡','其他')),
    source       TEXT NOT NULL DEFAULT 'text'
                 CHECK (source IN ('text','image')),   -- 录入途径：打字 / 截图
    raw_desc     TEXT,                       -- 用户原始描述或 OCR 摘要
    note         TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_exp_occurred ON expenses(occurred_at);
CREATE INDEX IF NOT EXISTS idx_exp_cat1     ON expenses(category_l1);
CREATE INDEX IF NOT EXISTS idx_exp_cat2     ON expenses(category_l1, category_l2);
CREATE INDEX IF NOT EXISTS idx_exp_platform ON expenses(platform);

-- 业务去重：金额 + 分类 + 分钟级时间（同一笔钱被重复录入时拦下）
CREATE UNIQUE INDEX IF NOT EXISTS uniq_exp_dedupe
    ON expenses(amount, occurred_at, category_l1, platform);

-- ---------------------------------------------------------------
-- 财务情况表（时点快照）
-- ---------------------------------------------------------------
-- 每一行 = 某一个时点的完整财务快照（通常是月末）。
-- 资产类字段为正数；负债类字段填正数表示"欠多少"，不要填负数。
CREATE TABLE IF NOT EXISTS finance_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_at     TEXT NOT NULL UNIQUE,    -- 快照时点，如 2026-09-30
    -- 资产
    alipay          REAL NOT NULL DEFAULT 0, -- 支付宝余额
    wechat          REAL NOT NULL DEFAULT 0, -- 微信余额
    bank_balance    REAL NOT NULL DEFAULT 0, -- 银行卡余额
    cash            REAL NOT NULL DEFAULT 0, -- 现金总额
    wealth          REAL NOT NULL DEFAULT 0, -- 理财
    housing_fund    REAL NOT NULL DEFAULT 0, -- 住房公积金
    -- 负债（填正数，表示欠款金额）
    huabei          REAL NOT NULL DEFAULT 0, -- 花呗
    jd_baitiao      REAL NOT NULL DEFAULT 0, -- 京东白条
    credit_card     REAL NOT NULL DEFAULT 0, -- 信用卡
    loan_outstanding REAL NOT NULL DEFAULT 0,-- 贷款待还总额
    -- 汇总
    total_assets    REAL GENERATED ALWAYS AS (
        alipay + wechat + bank_balance + cash + wealth + housing_fund
    ) VIRTUAL,                               -- 资产总计（自动计算，勿手工录入）
    total_debt      REAL GENERATED ALWAYS AS (
        huabei + jd_baitiao + credit_card + loan_outstanding
    ) VIRTUAL,                               -- 负债总计（自动计算）
    net_worth       REAL GENERATED ALWAYS AS (
        (alipay + wechat + bank_balance + cash + wealth + housing_fund)
        - (huabei + jd_baitiao + credit_card + loan_outstanding)
    ) VIRTUAL,                               -- 净资产（自动计算）
    source          TEXT NOT NULL DEFAULT 'text'
                    CHECK (source IN ('text','image')),
    raw_desc        TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE INDEX IF NOT EXISTS idx_fin_snapshot ON finance_snapshots(snapshot_at);

-- ---------------------------------------------------------------
-- 审计：所有写入留痕，便于回溯模型判断错误
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_log (
    id            INTEGER PRIMARY KEY,
    skill         TEXT NOT NULL,             -- expense-capture / finance-snapshot ...
    target_table  TEXT NOT NULL,
    target_id     INTEGER,
    action        TEXT NOT NULL
                  CHECK (action IN ('insert','update','reject_duplicate','reject_invalid','clarify')),
    detail        TEXT,                      -- JSON：原始提取结果、拒绝原因
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------------------------------------------------------------
-- schema 迁移记录（为后续动态表预留）
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    description TEXT NOT NULL,
    sql         TEXT
);

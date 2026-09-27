"""SQLite 存储层。

设计要点：
- 授权范围（grants）与合同状态均为双时间轴：valid 时间（业务生效日）+ recorded 时间（系统记录时刻），
  支持补充协议追溯生效，也支持按「日期 × 知情时刻」还原授权版图。
- 已确认结算期只增不改：迟到销售、退款与追溯修订以调整条目进入后续开放结算期。
- 销售明细以 (source, line_ref) 唯一约束实现幂等导入。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    rights_type    TEXT NOT NULL,
    owner_party_id TEXT NOT NULL,
    notes          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    id         TEXT PRIMARY KEY,
    party_id   TEXT NOT NULL,
    title      TEXT NOT NULL,
    sensitive  INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT NOT NULL,
    valid_to   TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

-- 合同生命周期事件（event_date 为业务生效日，recorded_at 为系统记录时刻）
CREATE TABLE IF NOT EXISTS contract_state_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id TEXT NOT NULL,
    state       TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);

-- 授权范围版本：每次记录生成新版本；被取代时通过 grant_retractions 截断
CREATE TABLE IF NOT EXISTS grants (
    id          TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    rights_type TEXT NOT NULL,
    territories TEXT NOT NULL,          -- JSON 数组，"*" 表示全部
    media       TEXT NOT NULL,          -- JSON 数组，"*" 表示全部
    exclusive   INTEGER NOT NULL DEFAULT 0,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT,                   -- NULL 表示无限期
    source      TEXT NOT NULL,          -- base / 申请ID / 补充协议ID
    recorded_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grant_retractions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id       TEXT NOT NULL,
    retracted_from TEXT NOT NULL,       -- 自该业务日期起失效
    source         TEXT NOT NULL,       -- 终止事件 / 补充协议ID
    recorded_at    TEXT NOT NULL,
    recorded_by    TEXT NOT NULL
);

-- 分成规则版本（费率 + 多方比例），effective_from 可早于记录时刻（追溯生效）
CREATE TABLE IF NOT EXISTS share_rules (
    id              TEXT PRIMARY KEY,
    contract_id     TEXT NOT NULL,
    effective_from  TEXT NOT NULL,
    royalty_rate_bp INTEGER NOT NULL,
    splits          TEXT NOT NULL,      -- JSON: [{"party_id","bp"}]
    source          TEXT NOT NULL,      -- base / 比例修订ID / 补充协议ID
    reason          TEXT NOT NULL DEFAULT '',
    recorded_at     TEXT NOT NULL,
    recorded_by     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id          TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    grants      TEXT NOT NULL,          -- JSON: 申请的授权范围
    state       TEXT NOT NULL,          -- 已提交 / 已批准 / 已拒绝
    reason      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT
);

CREATE TABLE IF NOT EXISTS milestones (
    id          TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    due_date    TEXT,
    state       TEXT NOT NULL,          -- 待交付 / 已交付 / 已验收
    evidence    TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_lines (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,       -- 数据来源（渠道/商户系统）
    line_ref       TEXT NOT NULL,       -- 来源侧行号，幂等键的一部分
    contract_id    TEXT NOT NULL,
    period         TEXT NOT NULL,       -- 业务所属结算期 YYYY-MM
    kind           TEXT NOT NULL,       -- 销售回报 / 退款冲销
    amount_cents   INTEGER NOT NULL,    -- 销售为正，退款为负
    ref_line_ref   TEXT,                -- 退款对应的原销售行
    batch_id       TEXT NOT NULL,
    routed_period  TEXT NOT NULL,       -- 实际进入的结算期（迟到数据改道开放期）
    settled_period TEXT,                -- 已被哪个结算期吸收（未结算为 NULL）
    imported_at    TEXT NOT NULL,
    imported_by    TEXT NOT NULL,
    UNIQUE (source, line_ref)
);

CREATE TABLE IF NOT EXISTS import_batches (
    id          TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    total       INTEGER NOT NULL,
    inserted    INTEGER NOT NULL,
    duplicates  INTEGER NOT NULL,
    imported_at TEXT NOT NULL,
    imported_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settlements (
    period       TEXT PRIMARY KEY,      -- YYYY-MM
    state        TEXT NOT NULL,         -- 开放 / 待确认 / 已确认
    computed_at  TEXT,
    computed_by  TEXT,
    confirmed_at TEXT,
    confirmed_by TEXT
);

CREATE TABLE IF NOT EXISTS settlement_lines (
    id           TEXT PRIMARY KEY,
    period       TEXT NOT NULL,
    contract_id  TEXT NOT NULL,
    party_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,         -- 销售回报 / 比例修订 / 补充协议
    amount_cents INTEGER NOT NULL,
    calc         TEXT NOT NULL,         -- JSON：该笔分成的完整计算依据
    origin_period TEXT,                 -- 调整行指向的原始结算期
    created_at   TEXT NOT NULL
);

-- 追溯调整条目：由比例修订 / 补充协议触发，等待被开放结算期吸收
CREATE TABLE IF NOT EXISTS adjustment_entries (
    id             TEXT PRIMARY KEY,
    contract_id    TEXT NOT NULL,
    origin_period  TEXT NOT NULL,       -- 被追溯的已确认结算期
    kind           TEXT NOT NULL,       -- 比例修订 / 补充协议
    target_period  TEXT NOT NULL,       -- 计划进入的开放结算期
    deltas         TEXT NOT NULL,       -- JSON: {party_id: 差额分}
    calc           TEXT NOT NULL,       -- JSON：新旧规则与差额依据
    settled_period TEXT,
    source         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    created_by     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    role        TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    reason      TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    """按操作开启短连接，写操作统一走事务。"""

    def __init__(self, path):
        self.path = str(path)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def tx(self):
        """写事务：提交前任何异常都会整体回滚。"""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

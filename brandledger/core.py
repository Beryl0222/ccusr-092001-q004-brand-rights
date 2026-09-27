"""领域核心：授权台账、合同生命周期、结算与追溯调整、时间还原与审计。

业务约定：
- 金额一律为整数「分」；费率与分成比例为万分比（10000 = 100%）。
- 授权范围与合同状态为双时间轴：valid 时间（业务生效）+ recorded 时间（系统记录），
  因此补充协议可追溯生效，且能在任意「日期 × 知情时刻」还原授权版图。
- 已确认结算期只增不改：迟到销售、退款与追溯修订以调整条目进入后续开放结算期。
- 销售明细以 (source, line_ref) 为幂等键，重复导入不会改变已确认账本。
- 任何金额或权利范围的变化都写入审计日志（前后版本 + 操作人）。
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .errors import ConflictError, DomainError, ForbiddenError, NotFoundError, StateError
from .money import BP_BASE, mul_bp, split_amount
from .vocab import load_vocab

MAX_DATE = "9999-12-31"

# 合同生命周期允许的状态流转（状态名取自 domain.json「合同版本」）
TRANSITIONS = {
    "草拟": {"会签"},
    "会签": {"草拟", "生效"},
    "生效": {"终止", "争议冻结"},
    "争议冻结": {"生效", "终止"},
    "终止": set(),
}

# 调整类型（取自 domain.json「调整类型」）
KIND_SALES = "销售回报"
KIND_REFUND = "退款冲销"
KIND_RATE = "比例修订"
KIND_AMEND = "补充协议"

ROLE_ADMIN = "admin"
ROLE_LEGAL = "legal"
ROLE_COMMERCIAL = "commercial"
ROLE_FINANCE = "finance"
ROLES = {ROLE_ADMIN, ROLE_LEGAL, ROLE_COMMERCIAL, ROLE_FINANCE}
# 敏感合同的条款与权利范围仅这些角色可见
SENSITIVE_VIEWERS = {ROLE_ADMIN, ROLE_LEGAL}

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Ctx:
    """一次操作的身份上下文。"""

    actor: str
    role: str


def _parse_date(value, field):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise DomainError(f"{field} 不是合法日期（YYYY-MM-DD）：{value!r}")


def _check_period(period):
    if not isinstance(period, str) or not _PERIOD_RE.match(period):
        raise DomainError(f"结算期格式应为 YYYY-MM：{period!r}")


def _period_of(date_iso):
    return date_iso[:7]


def _next_period(period):
    y, m = int(period[:4]), int(period[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def _period_end(period):
    y, m = int(period[:4]), int(period[5:7])
    return f"{period}-{monthrange(y, m)[1]:02d}"


def _day_before(date_iso):
    return (date.fromisoformat(date_iso) - timedelta(days=1)).isoformat()


def _overlap(a_start, a_end, b_start, b_end):
    """闭区间重叠判断；None 端点视为无限。"""
    a_end = a_end or MAX_DATE
    b_end = b_end or MAX_DATE
    return a_start <= b_end and b_start <= a_end


def _sets_overlap(a, b):
    """集合重叠，任一侧含 "*" 视为全覆盖。"""
    return "*" in a or "*" in b or bool(set(a) & set(b))


def _check_amount(value, field="amount_cents"):
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(f"{field} 必须为整数（单位：分）")


class Core:
    """领域服务入口。clock 可注入以便测试确定的时间。"""

    def __init__(self, store, vocab=None, clock=None):
        self.store = store
        self.vocab = vocab or load_vocab()
        self._clock = clock or datetime.now

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self):
        return self._clock()

    def _now_iso(self):
        return self._now().isoformat(timespec="microseconds")

    def _today(self):
        return self._now().date().isoformat()

    @staticmethod
    def _next_id(conn, prefix):
        row = conn.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO counters (name, value) VALUES (?, 1)", (prefix,))
            value = 1
        else:
            value = row["value"] + 1
            conn.execute("UPDATE counters SET value = ? WHERE name = ?", (value, prefix))
        return f"{prefix}-{value:06d}"

    def _audit(self, conn, ctx, action, entity_type, entity_id, before=None, after=None, reason=""):
        conn.execute(
            "INSERT INTO audit_log (ts, actor, role, action, entity_type, entity_id,"
            " before_json, after_json, reason) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                self._now_iso(),
                ctx.actor,
                ctx.role,
                action,
                entity_type,
                entity_id,
                json.dumps(before, ensure_ascii=False) if before is not None else None,
                json.dumps(after, ensure_ascii=False) if after is not None else None,
                reason,
            ),
        )

    @staticmethod
    def _require_role(ctx, *roles):
        allowed = set(roles) | {ROLE_ADMIN}
        if ctx.role not in allowed:
            raise ForbiddenError(f"角色 {ctx.role} 无权执行该操作（需要：{'、'.join(sorted(allowed))}）")

    def _get_contract_row(self, conn, contract_id):
        row = conn.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"合同不存在：{contract_id}")
        return row

    def _state_at(self, conn, contract_id, on_date, known_at):
        """指定业务日期与知情时刻下的合同状态。"""
        row = conn.execute(
            "SELECT state FROM contract_state_events"
            " WHERE contract_id = ? AND event_date <= ? AND recorded_at <= ?"
            " ORDER BY event_date DESC, id DESC LIMIT 1",
            (contract_id, on_date, known_at),
        ).fetchone()
        return row["state"] if row else None

    def _state_now(self, conn, contract_id):
        return self._state_at(conn, contract_id, self._today(), self._now_iso())

    def _retractions(self, conn, grant_ids, known_at):
        """各授权在 known_at 之前的截断记录：grant_id -> [retracted_from, ...]"""
        result = {g: [] for g in grant_ids}
        if not grant_ids:
            return result
        marks = ",".join("?" for _ in grant_ids)
        rows = conn.execute(
            f"SELECT grant_id, retracted_from FROM grant_retractions"
            f" WHERE grant_id IN ({marks}) AND recorded_at <= ?",
            (*grant_ids, known_at),
        ).fetchall()
        for r in rows:
            result[r["grant_id"]].append(r["retracted_from"])
        return result

    @staticmethod
    def _grant_span(grant_row, retracted_from_list):
        """授权的有效区间 [start, end]（闭区间）；被截断后 end 可能小于 start（完全失效）。"""
        start = grant_row["valid_from"]
        end = grant_row["valid_to"] or MAX_DATE
        for r in retracted_from_list:
            if r <= end:
                end = _day_before(r)
        return start, end

    def _effective_grants(self, conn, contract_id, on_date, known_at):
        """on_date 当日有效、且 known_at 时已知的授权范围。"""
        rows = conn.execute(
            "SELECT * FROM grants WHERE contract_id = ? AND recorded_at <= ? ORDER BY id",
            (contract_id, known_at),
        ).fetchall()
        retractions = self._retractions(conn, [r["id"] for r in rows], known_at)
        effective = []
        for r in rows:
            start, end = self._grant_span(r, retractions[r["id"]])
            if start <= on_date <= end:
                effective.append((r, start, end))
        return effective

    def _share_rule_at(self, conn, contract_id, on_date, known_at=None):
        """on_date 当日有效的分成规则（追溯修订后按最新知情版本）。"""
        known_at = known_at or self._now_iso()
        return conn.execute(
            "SELECT * FROM share_rules WHERE contract_id = ? AND effective_from <= ?"
            " AND recorded_at <= ?"
            " ORDER BY effective_from DESC, recorded_at DESC, rowid DESC LIMIT 1",
            (contract_id, on_date, known_at),
        ).fetchone()

    # ------------------------------------------------------------------
    # 合作方与素材
    # ------------------------------------------------------------------
    def create_party(self, ctx, name, kind):
        self._require_role(ctx, ROLE_COMMERCIAL, ROLE_LEGAL, ROLE_FINANCE)
        if not name:
            raise DomainError("合作方名称不能为空")
        with self.store.tx() as conn:
            pid = self._next_id(conn, "PTY")
            conn.execute(
                "INSERT INTO parties (id, name, kind, created_at, created_by) VALUES (?,?,?,?,?)",
                (pid, name, kind or "未分类", self._now_iso(), ctx.actor),
            )
            self._audit(conn, ctx, "新建合作方", "party", pid, after={"name": name, "kind": kind})
            return pid

    def list_parties(self, ctx):
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM parties ORDER BY id")]

    def create_asset(self, ctx, name, rights_type, owner_party_id, notes=""):
        self._require_role(ctx, ROLE_COMMERCIAL, ROLE_LEGAL)
        self.vocab.check_rights_type(rights_type)
        with self.store.tx() as conn:
            if conn.execute("SELECT 1 FROM parties WHERE id = ?", (owner_party_id,)).fetchone() is None:
                raise NotFoundError(f"权属方不存在：{owner_party_id}")
            aid = self._next_id(conn, "AST")
            conn.execute(
                "INSERT INTO assets (id, name, rights_type, owner_party_id, notes, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?)",
                (aid, name, rights_type, owner_party_id, notes, self._now_iso(), ctx.actor),
            )
            self._audit(
                conn, ctx, "登记素材", "asset", aid,
                after={"name": name, "rights_type": rights_type, "owner_party_id": owner_party_id},
            )
            return aid

    def list_assets(self, ctx):
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM assets ORDER BY id")]

    # ------------------------------------------------------------------
    # 授权范围与分成规则的载荷校验
    # ------------------------------------------------------------------
    def _check_grant_payload(self, g):
        self.vocab.check_rights_type(g.get("rights_type"))
        for field in ("territories", "media"):
            value = g.get(field)
            if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
                raise DomainError(f"授权范围的 {field} 必须是非空字符串数组")
        start = _parse_date(g.get("valid_from"), "valid_from")
        if g.get("valid_to") is not None:
            end = _parse_date(g["valid_to"], "valid_to")
            if end < start:
                raise DomainError("授权窗口 valid_to 早于 valid_from")

    def _check_share_rule_payload(self, conn, rule):
        rate = rule.get("royalty_rate_bp")
        _check_amount(rate, "royalty_rate_bp")
        if not 0 <= rate <= BP_BASE:
            raise DomainError("费率须在 0～10000 万分比之间")
        splits = rule.get("splits")
        if not isinstance(splits, list) or not splits:
            raise DomainError("分成比例 splits 不能为空")
        seen = set()
        for s in splits:
            pid, bp = s.get("party_id"), s.get("bp")
            if pid in seen:
                raise DomainError(f"分成方重复：{pid}")
            seen.add(pid)
            _check_amount(bp, "bp")
            if bp <= 0:
                raise DomainError("分成比例须为正数")
            if conn.execute("SELECT 1 FROM parties WHERE id = ?", (pid,)).fetchone() is None:
                raise NotFoundError(f"分成方不存在：{pid}")
        total = sum(s["bp"] for s in splits)
        if total != BP_BASE:
            raise DomainError(f"分成比例合计须为 10000（当前 {total}）")

    def _insert_grant(self, conn, ctx, contract_id, g, source):
        gid = self._next_id(conn, "GR")
        conn.execute(
            "INSERT INTO grants (id, contract_id, rights_type, territories, media, exclusive,"
            " valid_from, valid_to, source, recorded_at, recorded_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                gid, contract_id, g["rights_type"],
                json.dumps(g["territories"], ensure_ascii=False),
                json.dumps(g["media"], ensure_ascii=False),
                1 if g.get("exclusive") else 0,
                g["valid_from"], g.get("valid_to"), source,
                self._now_iso(), ctx.actor,
            ),
        )
        return gid

    def _insert_share_rule(self, conn, ctx, contract_id, rule, effective_from, source, reason=""):
        rid = self._next_id(conn, "SR")
        conn.execute(
            "INSERT INTO share_rules (id, contract_id, effective_from, royalty_rate_bp, splits,"
            " source, reason, recorded_at, recorded_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                rid, contract_id, effective_from, rule["royalty_rate_bp"],
                json.dumps(rule["splits"], ensure_ascii=False),
                source, reason, self._now_iso(), ctx.actor,
            ),
        )
        return rid

    # ------------------------------------------------------------------
    # 合同
    # ------------------------------------------------------------------
    def create_contract(self, ctx, party_id, title, valid_from, valid_to=None,
                        sensitive=False, grants=(), share_rule=None):
        """新建合同（初始状态：草拟），同时登记初始授权范围与分成规则。"""
        self._require_role(ctx, ROLE_LEGAL)
        _parse_date(valid_from, "valid_from")
        if valid_to is not None:
            if _parse_date(valid_to, "valid_to") < _parse_date(valid_from, "valid_from"):
                raise DomainError("合同 valid_to 早于 valid_from")
        grants = list(grants or [])
        for g in grants:
            self._check_grant_payload(g)
        if share_rule is None:
            raise DomainError("合同必须包含分成规则 share_rule")
        with self.store.tx() as conn:
            if conn.execute("SELECT 1 FROM parties WHERE id = ?", (party_id,)).fetchone() is None:
                raise NotFoundError(f"合作方不存在：{party_id}")
            self._check_share_rule_payload(conn, share_rule)
            cid = self._next_id(conn, "CT")
            conn.execute(
                "INSERT INTO contracts (id, party_id, title, sensitive, valid_from, valid_to,"
                " created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
                (cid, party_id, title, 1 if sensitive else 0, valid_from, valid_to,
                 self._now_iso(), ctx.actor),
            )
            conn.execute(
                "INSERT INTO contract_state_events (contract_id, state, event_date, reason,"
                " recorded_at, recorded_by) VALUES (?,?,?,?,?,?)",
                (cid, "草拟", self._today(), "新建合同", self._now_iso(), ctx.actor),
            )
            grant_ids = [self._insert_grant(conn, ctx, cid, g, "base") for g in grants]
            rule_id = self._insert_share_rule(conn, ctx, cid, share_rule, valid_from, "base")
            self._audit(
                conn, ctx, "新建合同", "contract", cid,
                after={
                    "party_id": party_id, "title": title, "sensitive": bool(sensitive),
                    "valid_from": valid_from, "valid_to": valid_to,
                    "grants": grant_ids, "share_rule": share_rule,
                },
            )
            return cid

    def transition_contract(self, ctx, contract_id, to_state, event_date=None, reason=""):
        """合同状态流转；终止时自动截断全部有效授权。"""
        self._require_role(ctx, ROLE_LEGAL)
        self.vocab.check_contract_state(to_state)
        event_date = event_date or self._today()
        _parse_date(event_date, "event_date")
        with self.store.tx() as conn:
            self._get_contract_row(conn, contract_id)
            current = self._state_now(conn, contract_id)
            if to_state not in TRANSITIONS.get(current, set()):
                raise StateError(f"合同状态不允许从「{current}」流转到「{to_state}」")
            conn.execute(
                "INSERT INTO contract_state_events (contract_id, state, event_date, reason,"
                " recorded_at, recorded_by) VALUES (?,?,?,?,?,?)",
                (contract_id, to_state, event_date, reason, self._now_iso(), ctx.actor),
            )
            truncated = []
            if to_state == "终止":
                truncated = self._retract_grants(conn, ctx, contract_id, event_date, source="合同终止")
            self._audit(
                conn, ctx, "合同状态流转", "contract", contract_id,
                before={"state": current}, after={"state": to_state, "event_date": event_date},
                reason=reason,
            )
            if truncated:
                self._audit(
                    conn, ctx, "授权随终止截断", "contract", contract_id,
                    before={"effective_grants": truncated}, after={"effective_grants": []},
                    reason=f"合同于 {event_date} 终止",
                )
            return {"contract_id": contract_id, "state": to_state, "event_date": event_date}

    def _retract_grants(self, conn, ctx, contract_id, retracted_from, source):
        """截断 retracted_from 当日及之后仍有效的全部授权，返回被截断的授权 id。"""
        rows = conn.execute(
            "SELECT id FROM grants WHERE contract_id = ?", (contract_id,)
        ).fetchall()
        known = self._now_iso()
        retractions = self._retractions(conn, [r["id"] for r in rows], known)
        truncated = []
        for r in rows:
            grant = conn.execute("SELECT * FROM grants WHERE id = ?", (r["id"],)).fetchone()
            _start, end = self._grant_span(grant, retractions[r["id"]])
            if end >= retracted_from:
                conn.execute(
                    "INSERT INTO grant_retractions (grant_id, retracted_from, source,"
                    " recorded_at, recorded_by) VALUES (?,?,?,?,?)",
                    (r["id"], retracted_from, source, known, ctx.actor),
                )
                truncated.append(r["id"])
        return truncated

    def _contract_detail(self, conn, ctx, row):
        cid = row["id"]
        known = self._now_iso()
        detail = {
            "id": cid,
            "party_id": row["party_id"],
            "title": row["title"],
            "sensitive": bool(row["sensitive"]),
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "state": self._state_now(conn, cid),
            "state_events": [
                dict(r) for r in conn.execute(
                    "SELECT state, event_date, reason, recorded_at, recorded_by"
                    " FROM contract_state_events WHERE contract_id = ? ORDER BY id", (cid,))
            ],
        }
        if row["sensitive"] and ctx.role not in SENSITIVE_VIEWERS:
            # 敏感合同：仅暴露存在性与状态，条款与权利范围按职责隐藏
            return {**detail, "redacted": True, "title": "（敏感合同，按职责不可见）",
                    "party_id": None, "grants": None, "share_rules": None}
        grants = []
        grant_rows = conn.execute(
            "SELECT * FROM grants WHERE contract_id = ? ORDER BY id", (cid,)).fetchall()
        retractions = self._retractions(conn, [g["id"] for g in grant_rows], known)
        for g in grant_rows:
            start, end = self._grant_span(g, retractions[g["id"]])
            grants.append({
                "id": g["id"],
                "rights_type": g["rights_type"],
                "territories": json.loads(g["territories"]),
                "media": json.loads(g["media"]),
                "exclusive": bool(g["exclusive"]),
                "valid_from": g["valid_from"],
                "valid_to": g["valid_to"],
                "effective_from": start,
                "effective_to": None if end == MAX_DATE else end,
                "active": end >= self._today(),
                "source": g["source"],
                "recorded_at": g["recorded_at"],
                "recorded_by": g["recorded_by"],
            })
        rules = [
            {
                "id": r["id"],
                "effective_from": r["effective_from"],
                "royalty_rate_bp": r["royalty_rate_bp"],
                "splits": json.loads(r["splits"]),
                "source": r["source"],
                "reason": r["reason"],
                "recorded_at": r["recorded_at"],
                "recorded_by": r["recorded_by"],
            }
            for r in conn.execute(
                "SELECT * FROM share_rules WHERE contract_id = ? ORDER BY effective_from, recorded_at",
                (cid,))
        ]
        return {**detail, "redacted": False, "grants": grants, "share_rules": rules}

    def get_contract(self, ctx, contract_id):
        with self.store.read() as conn:
            row = self._get_contract_row(conn, contract_id)
            return self._contract_detail(conn, ctx, row)

    def list_contracts(self, ctx):
        with self.store.read() as conn:
            rows = conn.execute("SELECT * FROM contracts ORDER BY id").fetchall()
            result = []
            for row in rows:
                d = self._contract_detail(conn, ctx, row)
                result.append({
                    "id": d["id"], "title": d["title"], "party_id": d["party_id"],
                    "sensitive": d["sensitive"], "state": d["state"], "redacted": d["redacted"],
                })
            return result

    # ------------------------------------------------------------------
    # 授权申请与审批（防止过期或互斥授权被批准）
    # ------------------------------------------------------------------
    def submit_application(self, ctx, contract_id, grants):
        self._require_role(ctx, ROLE_COMMERCIAL, ROLE_LEGAL)
        grants = list(grants or [])
        if not grants:
            raise DomainError("申请至少包含一项授权范围")
        for g in grants:
            self._check_grant_payload(g)
        with self.store.tx() as conn:
            self._get_contract_row(conn, contract_id)
            aid = self._next_id(conn, "AP")
            conn.execute(
                "INSERT INTO applications (id, contract_id, grants, state, reason, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?)",
                (aid, contract_id, json.dumps(grants, ensure_ascii=False), "已提交", "",
                 self._now_iso(), ctx.actor),
            )
            self._audit(conn, ctx, "提交授权申请", "application", aid,
                        after={"contract_id": contract_id, "grants": grants})
            return aid

    def _find_conflicts(self, conn, contract_id, g):
        """检查拟授权与既有有效授权的互斥冲突（排他任一即冲突）。"""
        rows = conn.execute(
            "SELECT * FROM grants WHERE contract_id <> ? AND rights_type = ?",
            (contract_id, g["rights_type"]),
        ).fetchall()
        known = self._now_iso()
        retractions = self._retractions(conn, [r["id"] for r in rows], known)
        conflicts = []
        for r in rows:
            other_state = self._state_now(conn, r["contract_id"])
            if other_state not in ("生效", "争议冻结"):
                continue
            start, end = self._grant_span(r, retractions[r["id"]])
            if not _overlap(g["valid_from"], g.get("valid_to"), start, end):
                continue
            if not _sets_overlap(g["territories"], json.loads(r["territories"])):
                continue
            if not _sets_overlap(g["media"], json.loads(r["media"])):
                continue
            if r["exclusive"] or g.get("exclusive"):
                conflicts.append({
                    "grant_id": r["id"],
                    "contract_id": r["contract_id"],
                    "rights_type": r["rights_type"],
                    "territories": json.loads(r["territories"]),
                    "media": json.loads(r["media"]),
                    "exclusive": bool(r["exclusive"]),
                    "effective_from": start,
                    "effective_to": None if end == MAX_DATE else end,
                })
        return conflicts

    def approve_application(self, ctx, application_id, reason=""):
        """批准申请：合同须生效、窗口未过期且在合同期内、无互斥冲突。"""
        self._require_role(ctx, ROLE_LEGAL)
        with self.store.tx() as conn:
            app = conn.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            if app is None:
                raise NotFoundError(f"申请不存在：{application_id}")
            if app["state"] != "已提交":
                raise StateError(f"申请当前状态为「{app['state']}」，不能批准")
            contract = self._get_contract_row(conn, app["contract_id"])
            state = self._state_now(conn, contract["id"])
            if state != "生效":
                raise StateError(f"合同当前状态为「{state}」，仅生效合同可批准授权")
            today = self._today()
            grants = json.loads(app["grants"])
            for g in grants:
                if g["valid_from"] < contract["valid_from"]:
                    raise DomainError(f"授权起始 {g['valid_from']} 早于合同起始 {contract['valid_from']}")
                if contract["valid_to"] and (g.get("valid_to") or MAX_DATE) > contract["valid_to"]:
                    raise DomainError(f"授权截止超出合同截止 {contract['valid_to']}")
                if (g.get("valid_to") or MAX_DATE) < today:
                    raise DomainError(f"授权窗口已于 {g.get('valid_to')} 过期，禁止批准")
                conflicts = self._find_conflicts(conn, contract["id"], g)
                if conflicts:
                    raise ConflictError("存在互斥授权，禁止批准", details={"conflicts": conflicts})
            grant_ids = [self._insert_grant(conn, ctx, contract["id"], g, application_id)
                         for g in grants]
            conn.execute(
                "UPDATE applications SET state = '已批准', decided_at = ?, decided_by = ?, reason = ?"
                " WHERE id = ?",
                (self._now_iso(), ctx.actor, reason, application_id),
            )
            self._audit(
                conn, ctx, "批准授权申请", "application", application_id,
                before={"state": "已提交"},
                after={"state": "已批准", "grants": grant_ids, "approver": ctx.actor},
                reason=reason,
            )
            return {"application_id": application_id, "state": "已批准", "grants": grant_ids}

    def reject_application(self, ctx, application_id, reason=""):
        self._require_role(ctx, ROLE_LEGAL)
        with self.store.tx() as conn:
            app = conn.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            if app is None:
                raise NotFoundError(f"申请不存在：{application_id}")
            if app["state"] != "已提交":
                raise StateError(f"申请当前状态为「{app['state']}」，不能拒绝")
            conn.execute(
                "UPDATE applications SET state = '已拒绝', decided_at = ?, decided_by = ?, reason = ?"
                " WHERE id = ?",
                (self._now_iso(), ctx.actor, reason, application_id),
            )
            self._audit(conn, ctx, "拒绝授权申请", "application", application_id,
                        before={"state": "已提交"}, after={"state": "已拒绝"}, reason=reason)
            return {"application_id": application_id, "state": "已拒绝"}

    def get_application(self, ctx, application_id):
        with self.store.read() as conn:
            app = conn.execute(
                "SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
            if app is None:
                raise NotFoundError(f"申请不存在：{application_id}")
            contract = self._get_contract_row(conn, app["contract_id"])
            result = dict(app)
            result["grants"] = json.loads(app["grants"])
            if contract["sensitive"] and ctx.role not in SENSITIVE_VIEWERS:
                result["grants"] = None
                result["redacted"] = True
            return result

    def list_applications(self, ctx, contract_id=None):
        with self.store.read() as conn:
            if contract_id:
                rows = conn.execute(
                    "SELECT * FROM applications WHERE contract_id = ? ORDER BY id",
                    (contract_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM applications ORDER BY id").fetchall()
            result = []
            for app in rows:
                contract = self._get_contract_row(conn, app["contract_id"])
                item = {k: app[k] for k in ("id", "contract_id", "state", "reason",
                                            "created_at", "created_by", "decided_at", "decided_by")}
                if contract["sensitive"] and ctx.role not in SENSITIVE_VIEWERS:
                    item["grants"] = None
                    item["redacted"] = True
                else:
                    item["grants"] = json.loads(app["grants"])
                result.append(item)
            return result

    # ------------------------------------------------------------------
    # 分阶段交付
    # ------------------------------------------------------------------
    def add_milestone(self, ctx, contract_id, name, due_date=None):
        self._require_role(ctx, ROLE_COMMERCIAL, ROLE_LEGAL)
        if not name:
            raise DomainError("里程碑名称不能为空")
        if due_date is not None:
            _parse_date(due_date, "due_date")
        with self.store.tx() as conn:
            self._get_contract_row(conn, contract_id)
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM milestones WHERE contract_id = ?",
                (contract_id,)).fetchone()
            mid = self._next_id(conn, "MS")
            conn.execute(
                "INSERT INTO milestones (id, contract_id, seq, name, due_date, state, evidence,"
                " updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, contract_id, row["m"] + 1, name, due_date, "待交付", "",
                 self._now_iso(), ctx.actor),
            )
            self._audit(conn, ctx, "新增交付里程碑", "milestone", mid,
                        after={"contract_id": contract_id, "name": name, "due_date": due_date})
            return mid

    def _advance_milestone(self, ctx, milestone_id, from_state, to_state, action, evidence=None):
        with self.store.tx() as conn:
            ms = conn.execute(
                "SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
            if ms is None:
                raise NotFoundError(f"里程碑不存在：{milestone_id}")
            if ms["state"] != from_state:
                raise StateError(f"里程碑当前状态为「{ms['state']}」，不能{action}")
            conn.execute(
                "UPDATE milestones SET state = ?, evidence = COALESCE(?, evidence),"
                " updated_at = ?, updated_by = ? WHERE id = ?",
                (to_state, evidence, self._now_iso(), ctx.actor, milestone_id),
            )
            self._audit(conn, ctx, action, "milestone", milestone_id,
                        before={"state": from_state}, after={"state": to_state})
            return {"milestone_id": milestone_id, "state": to_state}

    def deliver_milestone(self, ctx, milestone_id, evidence=""):
        self._require_role(ctx, ROLE_COMMERCIAL)
        return self._advance_milestone(ctx, milestone_id, "待交付", "已交付", "交付里程碑", evidence)

    def accept_milestone(self, ctx, milestone_id):
        self._require_role(ctx, ROLE_COMMERCIAL, ROLE_LEGAL)
        return self._advance_milestone(ctx, milestone_id, "已交付", "已验收", "验收里程碑")

    def list_milestones(self, ctx, contract_id):
        with self.store.read() as conn:
            self._get_contract_row(conn, contract_id)
            return [dict(r) for r in conn.execute(
                "SELECT * FROM milestones WHERE contract_id = ? ORDER BY seq", (contract_id,))]

    # ------------------------------------------------------------------
    # 销售明细导入（幂等）与迟到数据改道
    # ------------------------------------------------------------------
    def _get_settlement_row(self, conn, period):
        return conn.execute("SELECT * FROM settlements WHERE period = ?", (period,)).fetchone()

    def _open_target_period(self, conn):
        """迟到/调整数据的落点：含今日在内的第一个未确认结算期（按需创建）。"""
        period = _period_of(self._today())
        while True:
            row = self._get_settlement_row(conn, period)
            if row is None:
                conn.execute("INSERT INTO settlements (period, state) VALUES (?, '开放')", (period,))
                return period
            if row["state"] != "已确认":
                return period
            period = _next_period(period)

    def _route_period(self, conn, period):
        """销售明细的入账结算期：所属期已确认则改道当前开放期（迟到数据）。"""
        row = self._get_settlement_row(conn, period)
        if row is not None and row["state"] == "已确认":
            return self._open_target_period(conn)
        return period

    def import_sales(self, ctx, source, lines):
        """导入销售/退款明细。幂等键为 (source, line_ref)：重复导入逐行跳过。"""
        self._require_role(ctx, ROLE_COMMERCIAL, ROLE_FINANCE)
        if not source or not isinstance(source, str):
            raise DomainError("source（数据来源）不能为空")
        if not isinstance(lines, list) or not lines:
            raise DomainError("导入明细不能为空")
        inserted, duplicates = 0, []
        with self.store.tx() as conn:
            batch_id = self._next_id(conn, "IB")
            for i, line in enumerate(lines):
                ref = line.get("line_ref")
                if not ref or not isinstance(ref, str):
                    raise DomainError(f"第 {i + 1} 行缺少 line_ref")
                contract_id = line.get("contract_id")
                self._get_contract_row(conn, contract_id)
                period = line.get("period")
                _check_period(period)
                kind = line.get("kind")
                if kind not in (KIND_SALES, KIND_REFUND):
                    raise DomainError(f"第 {i + 1} 行 kind 须为 销售回报/退款冲销")
                amount = line.get("amount_cents")
                _check_amount(amount)
                if kind == KIND_SALES and amount <= 0:
                    raise DomainError(f"第 {i + 1} 行销售回报金额须为正数")
                if kind == KIND_REFUND and amount >= 0:
                    raise DomainError(f"第 {i + 1} 行退款冲销金额须为负数")
                exists = conn.execute(
                    "SELECT 1 FROM sales_lines WHERE source = ? AND line_ref = ?",
                    (source, ref)).fetchone()
                if exists:
                    duplicates.append(ref)
                    continue
                lid = self._next_id(conn, "SL")
                conn.execute(
                    "INSERT INTO sales_lines (id, source, line_ref, contract_id, period, kind,"
                    " amount_cents, ref_line_ref, batch_id, routed_period, imported_at, imported_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (lid, source, ref, contract_id, period, kind, amount,
                     line.get("ref_line_ref"), batch_id, self._route_period(conn, period),
                     self._now_iso(), ctx.actor),
                )
                inserted += 1
            conn.execute(
                "INSERT INTO import_batches (id, source, total, inserted, duplicates,"
                " imported_at, imported_by) VALUES (?,?,?,?,?,?,?)",
                (batch_id, source, len(lines), inserted, len(duplicates),
                 self._now_iso(), ctx.actor),
            )
            self._audit(
                conn, ctx, "导入销售明细", "import_batch", batch_id,
                after={"source": source, "total": len(lines),
                       "inserted": inserted, "duplicates": len(duplicates)},
            )
            return {"batch_id": batch_id, "total": len(lines),
                    "inserted": inserted, "duplicates": duplicates}

    def list_sales(self, ctx, contract_id=None, period=None):
        sql = "SELECT * FROM sales_lines WHERE 1=1"
        params = []
        if contract_id:
            sql += " AND contract_id = ?"
            params.append(contract_id)
        if period:
            sql += " AND period = ?"
            params.append(period)
        sql += " ORDER BY id"
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    # ------------------------------------------------------------------
    # 结算：运行、确认、计算依据
    # ------------------------------------------------------------------
    def run_settlement(self, ctx, period):
        """计算结算期：吸收所有应结未结的销售明细与追溯调整条目。

        可重复运行（覆盖未确认结果）；确认后禁止再运行。
        争议冻结中的合同本期跳过，明细留待解冻后的结算期吸收。
        """
        self._require_role(ctx, ROLE_FINANCE)
        _check_period(period)
        with self.store.tx() as conn:
            row = self._get_settlement_row(conn, period)
            if row is None:
                conn.execute("INSERT INTO settlements (period, state) VALUES (?, '开放')", (period,))
            elif row["state"] == "已确认":
                raise StateError(f"结算期 {period} 已确认，账本锁定，禁止重算")
            # 清掉上一轮未确认的计算结果，保证重算幂等
            conn.execute("UPDATE sales_lines SET settled_period = NULL WHERE settled_period = ?",
                         (period,))
            conn.execute(
                "UPDATE adjustment_entries SET settled_period = NULL WHERE settled_period = ?",
                (period,))
            conn.execute("DELETE FROM settlement_lines WHERE period = ?", (period,))

            sales = conn.execute(
                "SELECT * FROM sales_lines WHERE settled_period IS NULL AND routed_period <= ?"
                " ORDER BY id", (period,)).fetchall()
            by_contract = {}
            for s in sales:
                by_contract.setdefault(s["contract_id"], []).append(s)

            line_count, total_cents = 0, 0
            for cid, rows in sorted(by_contract.items()):
                if self._state_now(conn, cid) == "争议冻结":
                    continue  # 争议冻结：明细留待后续结算期
                rule = self._share_rule_at(conn, cid, _period_end(period))
                if rule is None:
                    raise DomainError(f"合同 {cid} 在 {period} 缺少分成规则，无法结算")
                gross = sum(r["amount_cents"] for r in rows if r["kind"] == KIND_SALES)
                refunds = sum(r["amount_cents"] for r in rows if r["kind"] == KIND_REFUND)
                net = gross + refunds
                royalty = mul_bp(net, rule["royalty_rate_bp"])
                splits = json.loads(rule["splits"])
                shares = split_amount(royalty, [(s["party_id"], s["bp"]) for s in splits])
                base_calc = {
                    "type": KIND_SALES,
                    "sales_line_ids": [r["id"] for r in rows],
                    "late_line_ids": [r["id"] for r in rows if r["period"] != period],
                    "gross_cents": gross,
                    "refund_cents": refunds,
                    "net_cents": net,
                    "royalty_rate_bp": rule["royalty_rate_bp"],
                    "royalty_cents": royalty,
                    "rule_source": rule["source"],
                    "rounding": "最大余数法",
                    "formula": "净销售额 × 费率 = 分成总额，再按各方万分比以最大余数法分摊",
                }
                share_map = dict(shares)
                for s in splits:
                    amount = share_map[s["party_id"]]
                    calc = {**base_calc, "party_bp": s["bp"], "party_amount_cents": amount}
                    lid = self._next_id(conn, "STL")
                    conn.execute(
                        "INSERT INTO settlement_lines (id, period, contract_id, party_id, kind,"
                        " amount_cents, calc, origin_period, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (lid, period, cid, s["party_id"], KIND_SALES, amount,
                         json.dumps(calc, ensure_ascii=False), None, self._now_iso()),
                    )
                    line_count += 1
                    total_cents += amount
                marks = ",".join("?" for _ in rows)
                conn.execute(
                    f"UPDATE sales_lines SET settled_period = ? WHERE id IN ({marks})",
                    (period, *[r["id"] for r in rows]),
                )

            entries = conn.execute(
                "SELECT * FROM adjustment_entries WHERE settled_period IS NULL"
                " AND target_period <= ? ORDER BY id", (period,)).fetchall()
            for e in entries:
                entry_calc = json.loads(e["calc"])
                for party_id, amount in json.loads(e["deltas"]).items():
                    calc = {**entry_calc, "party_id": party_id, "party_amount_cents": amount}
                    lid = self._next_id(conn, "STL")
                    conn.execute(
                        "INSERT INTO settlement_lines (id, period, contract_id, party_id, kind,"
                        " amount_cents, calc, origin_period, created_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (lid, period, e["contract_id"], party_id, e["kind"], amount,
                         json.dumps(calc, ensure_ascii=False), e["origin_period"],
                         self._now_iso()),
                    )
                    line_count += 1
                    total_cents += amount
                conn.execute(
                    "UPDATE adjustment_entries SET settled_period = ? WHERE id = ?",
                    (period, e["id"]))

            conn.execute(
                "UPDATE settlements SET state = '待确认', computed_at = ?, computed_by = ?"
                " WHERE period = ?",
                (self._now_iso(), ctx.actor, period))
            self._audit(conn, ctx, "结算计算", "settlement", period,
                        after={"lines": line_count, "total_cents": total_cents})
            return {"period": period, "state": "待确认",
                    "lines": line_count, "total_cents": total_cents}

    def confirm_settlement(self, ctx, period):
        """确认结算期：账本锁定，之后的迟到数据与追溯修订只能进入后续开放期。"""
        self._require_role(ctx, ROLE_FINANCE)
        _check_period(period)
        with self.store.tx() as conn:
            row = self._get_settlement_row(conn, period)
            if row is None:
                raise NotFoundError(f"结算期不存在：{period}")
            if row["state"] != "待确认":
                raise StateError(f"结算期 {period} 当前状态为「{row['state']}」，不能确认")
            conn.execute(
                "UPDATE settlements SET state = '已确认', confirmed_at = ?, confirmed_by = ?"
                " WHERE period = ?",
                (self._now_iso(), ctx.actor, period))
            totals = conn.execute(
                "SELECT kind, COUNT(*) AS n, SUM(amount_cents) AS total FROM settlement_lines"
                " WHERE period = ? GROUP BY kind", (period,)).fetchall()
            after = {r["kind"]: {"lines": r["n"], "total_cents": r["total"]} for r in totals}
            self._audit(conn, ctx, "确认结算", "settlement", period,
                        before={"state": "待确认"}, after={"state": "已确认", "totals": after})
            return {"period": period, "state": "已确认", "totals": after}

    def get_settlement(self, ctx, period):
        _check_period(period)
        with self.store.read() as conn:
            row = self._get_settlement_row(conn, period)
            if row is None:
                raise NotFoundError(f"结算期不存在：{period}")
            totals = conn.execute(
                "SELECT contract_id, kind, COUNT(*) AS n, SUM(amount_cents) AS total"
                " FROM settlement_lines WHERE period = ? GROUP BY contract_id, kind",
                (period,)).fetchall()
            return {
                **dict(row),
                "totals": [
                    {"contract_id": t["contract_id"], "kind": t["kind"],
                     "lines": t["n"], "total_cents": t["total"]}
                    for t in totals
                ],
            }

    def list_settlements(self, ctx):
        with self.store.read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM settlements ORDER BY period")]

    def explain_settlement(self, ctx, period):
        """某结算期每一笔分成的计算依据。"""
        self._require_role(ctx, ROLE_LEGAL, ROLE_FINANCE, ROLE_COMMERCIAL)
        _check_period(period)
        with self.store.read() as conn:
            row = self._get_settlement_row(conn, period)
            if row is None:
                raise NotFoundError(f"结算期不存在：{period}")
            lines = conn.execute(
                "SELECT * FROM settlement_lines WHERE period = ? ORDER BY id", (period,)).fetchall()
            return {
                "period": period,
                "state": row["state"],
                "computed_by": row["computed_by"],
                "confirmed_by": row["confirmed_by"],
                "lines": [
                    {
                        "id": l["id"],
                        "contract_id": l["contract_id"],
                        "party_id": l["party_id"],
                        "kind": l["kind"],
                        "amount_cents": l["amount_cents"],
                        "origin_period": l["origin_period"],
                        "calc": json.loads(l["calc"]),
                    }
                    for l in lines
                ],
            }

    # ------------------------------------------------------------------
    # 比例修订与补充协议（可追溯生效）
    # ------------------------------------------------------------------
    def add_share_revision(self, ctx, contract_id, effective_from, royalty_rate_bp,
                           splits, reason=""):
        """比例修订：新费率/新比例自 effective_from 起生效，已确认期间自动生成差额调整。"""
        self._require_role(ctx, ROLE_LEGAL)
        _parse_date(effective_from, "effective_from")
        rule = {"royalty_rate_bp": royalty_rate_bp, "splits": splits}
        with self.store.tx() as conn:
            self._get_contract_row(conn, contract_id)
            self._check_share_rule_payload(conn, rule)
            before = self._share_rule_at(conn, contract_id, self._today())
            rid = self._insert_share_rule(conn, ctx, contract_id, rule, effective_from,
                                          KIND_RATE, reason)
            entries = self._retro_adjust(conn, ctx, contract_id, effective_from, KIND_RATE, rid)
            self._audit(
                conn, ctx, "比例修订", "contract", contract_id,
                before={"share_rule": self._rule_snapshot(before)},
                after={"share_rule": rule, "effective_from": effective_from,
                       "adjustment_entries": entries},
                reason=reason,
            )
            return {"share_rule_id": rid, "adjustment_entries": entries}

    def add_amendment(self, ctx, contract_id, effective_from, grants=None,
                      share_rule=None, reason=""):
        """补充协议：可追溯生效。

        - 提供 grants 时视为自 effective_from 起的完整新授权范围：旧范围自该日起截断；
        - 提供 share_rule 时新规则自 effective_from 起生效，已确认期间自动生成差额调整。
        """
        self._require_role(ctx, ROLE_LEGAL)
        _parse_date(effective_from, "effective_from")
        if grants is None and share_rule is None:
            raise DomainError("补充协议须至少变更授权范围或分成规则之一")
        grants = list(grants) if grants is not None else None
        if grants is not None:
            for g in grants:
                self._check_grant_payload(g)
        with self.store.tx() as conn:
            self._get_contract_row(conn, contract_id)
            state = self._state_now(conn, contract_id)
            if state not in ("生效", "争议冻结"):
                raise StateError(f"合同当前状态为「{state}」，仅生效或争议冻结中的合同可签补充协议")
            if share_rule is not None:
                self._check_share_rule_payload(conn, share_rule)
            known = self._now_iso()
            before_grants = [
                {"id": g["id"], "rights_type": g["rights_type"],
                 "effective_from": s, "effective_to": None if e == MAX_DATE else e}
                for g, s, e in self._effective_grants(conn, contract_id, self._today(), known)
            ]
            before_rule = self._share_rule_at(conn, contract_id, self._today())
            aid = self._next_id(conn, "AMD")
            truncated, new_grants, entries = [], [], []
            if grants is not None:
                truncated = self._retract_grants(conn, ctx, contract_id, effective_from, source=aid)
                new_grants = [self._insert_grant(conn, ctx, contract_id, g, aid) for g in grants]
            if share_rule is not None:
                self._insert_share_rule(conn, ctx, contract_id, share_rule, effective_from, aid, reason)
                entries = self._retro_adjust(conn, ctx, contract_id, effective_from, KIND_AMEND, aid)
            self._audit(
                conn, ctx, "补充协议", "contract", contract_id,
                before={"grants": before_grants, "share_rule": self._rule_snapshot(before_rule)},
                after={
                    "amendment_id": aid, "effective_from": effective_from,
                    "truncated_grants": truncated, "new_grants": new_grants,
                    "share_rule": share_rule, "adjustment_entries": entries,
                },
                reason=reason,
            )
            return {"amendment_id": aid, "truncated_grants": truncated,
                    "new_grants": new_grants, "adjustment_entries": entries}

    @staticmethod
    def _rule_snapshot(rule_row):
        if rule_row is None:
            return None
        return {
            "royalty_rate_bp": rule_row["royalty_rate_bp"],
            "splits": json.loads(rule_row["splits"]),
            "effective_from": rule_row["effective_from"],
            "source": rule_row["source"],
        }

    def _attributed_amounts(self, conn, contract_id, origin_period):
        """某已确认结算期当前已归属各方的金额（含已确认调整与在途调整条目）。"""
        result = {}
        rows = conn.execute(
            "SELECT sl.party_id, sl.amount_cents FROM settlement_lines sl"
            " JOIN settlements s ON s.period = sl.period"
            " WHERE s.state = '已确认' AND sl.contract_id = ?"
            " AND ((sl.period = ? AND sl.origin_period IS NULL) OR sl.origin_period = ?)",
            (contract_id, origin_period, origin_period),
        ).fetchall()
        for r in rows:
            result[r["party_id"]] = result.get(r["party_id"], 0) + r["amount_cents"]
        pending = conn.execute(
            "SELECT deltas FROM adjustment_entries WHERE contract_id = ? AND origin_period = ?"
            " AND (settled_period IS NULL OR settled_period NOT IN"
            "      (SELECT period FROM settlements WHERE state = '已确认'))",
            (contract_id, origin_period),
        ).fetchall()
        for r in pending:
            for party_id, amount in json.loads(r["deltas"]).items():
                result[party_id] = result.get(party_id, 0) + amount
        return result

    def _retro_adjust(self, conn, ctx, contract_id, effective_from, kind, source):
        """追溯生效后，为受影响的已确认结算期生成差额调整条目（进入当前开放期）。"""
        created = []
        periods = conn.execute(
            "SELECT DISTINCT sl.period FROM settlement_lines sl"
            " JOIN settlements s ON s.period = sl.period"
            " WHERE sl.contract_id = ? AND sl.origin_period IS NULL AND s.state = '已确认'",
            (contract_id,),
        ).fetchall()
        for (origin,) in periods:
            if _period_end(origin) < effective_from:
                continue  # 新规则对该期间尚不生效
            base = conn.execute(
                "SELECT calc FROM settlement_lines WHERE period = ? AND contract_id = ?"
                " AND origin_period IS NULL LIMIT 1",
                (origin, contract_id),
            ).fetchone()
            if base is None:
                continue
            net = json.loads(base["calc"])["net_cents"]
            rule = self._share_rule_at(conn, contract_id, _period_end(origin))
            royalty = mul_bp(net, rule["royalty_rate_bp"])
            splits = json.loads(rule["splits"])
            new_amounts = dict(split_amount(royalty, [(s["party_id"], s["bp"]) for s in splits]))
            attributed = self._attributed_amounts(conn, contract_id, origin)
            deltas = {}
            for party_id in set(new_amounts) | set(attributed):
                delta = new_amounts.get(party_id, 0) - attributed.get(party_id, 0)
                if delta != 0:
                    deltas[party_id] = delta
            if not deltas:
                continue
            entry_id = self._next_id(conn, "AE")
            calc = {
                "type": kind,
                "origin_period": origin,
                "net_cents": net,
                "new_rule": {"royalty_rate_bp": rule["royalty_rate_bp"], "splits": splits},
                "new_amounts": new_amounts,
                "attributed_amounts": attributed,
                "deltas": deltas,
                "source": source,
                "formula": "按追溯后规则重算原始期间应得分成，与已归属金额之差即调整额",
            }
            conn.execute(
                "INSERT INTO adjustment_entries (id, contract_id, origin_period, kind,"
                " target_period, deltas, calc, source, created_at, created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (entry_id, contract_id, origin, kind, self._open_target_period(conn),
                 json.dumps(deltas, ensure_ascii=False),
                 json.dumps(calc, ensure_ascii=False),
                 source, self._now_iso(), ctx.actor),
            )
            created.append(entry_id)
        return created

    # ------------------------------------------------------------------
    # 授权版图还原 / 年度收入 / 审计
    # ------------------------------------------------------------------
    def landscape(self, ctx, on_date, known_at=None):
        """还原 on_date 当日有效的授权版图；known_at 可指定「以当时已知的信息」。"""
        _parse_date(on_date, "date")
        known_at = known_at or self._now_iso()
        result = []
        with self.store.read() as conn:
            contracts = conn.execute("SELECT * FROM contracts ORDER BY id").fetchall()
            for c in contracts:
                state = self._state_at(conn, c["id"], on_date, known_at)
                if state not in ("生效", "争议冻结"):
                    continue
                entry = {
                    "contract_id": c["id"],
                    "state": state,
                    "sensitive": bool(c["sensitive"]),
                }
                if c["sensitive"] and ctx.role not in SENSITIVE_VIEWERS:
                    entry.update({"redacted": True, "title": "（敏感合同，按职责不可见）",
                                  "party_id": None, "grants": None, "share_rule": None})
                    result.append(entry)
                    continue
                grants = [
                    {
                        "grant_id": g["id"],
                        "rights_type": g["rights_type"],
                        "territories": json.loads(g["territories"]),
                        "media": json.loads(g["media"]),
                        "exclusive": bool(g["exclusive"]),
                        "effective_from": s,
                        "effective_to": None if e == MAX_DATE else e,
                        "source": g["source"],
                    }
                    for g, s, e in self._effective_grants(conn, c["id"], on_date, known_at)
                ]
                rule = self._share_rule_at(conn, c["id"], on_date, known_at)
                entry.update({
                    "redacted": False,
                    "title": c["title"],
                    "party_id": c["party_id"],
                    "grants": grants,
                    "share_rule": self._rule_snapshot(rule),
                })
                result.append(entry)
        return {"date": on_date, "known_at": known_at, "contracts": result}

    def annual_report(self, ctx, year):
        """全年衍生收入：按合同 × 结算期汇总已确认分成。"""
        self._require_role(ctx, ROLE_LEGAL, ROLE_FINANCE)
        if not re.match(r"^\d{4}$", str(year)):
            raise DomainError(f"年份格式应为 YYYY：{year!r}")
        with self.store.read() as conn:
            rows = conn.execute(
                "SELECT sl.contract_id, sl.period, sl.kind, SUM(sl.amount_cents) AS total"
                " FROM settlement_lines sl JOIN settlements s ON s.period = sl.period"
                " WHERE s.state = '已确认' AND sl.period LIKE ?"
                " GROUP BY sl.contract_id, sl.period, sl.kind ORDER BY sl.contract_id, sl.period",
                (f"{year}-%",),
            ).fetchall()
        by_contract = {}
        grand = 0
        for r in rows:
            c = by_contract.setdefault(r["contract_id"], {"total_cents": 0, "periods": {}})
            p = c["periods"].setdefault(r["period"], {})
            p[r["kind"]] = r["total"]
            c["total_cents"] += r["total"]
            grand += r["total"]
        return {"year": str(year), "confirmed_only": True,
                "contracts": by_contract, "grand_total_cents": grand}

    def list_audit(self, ctx, entity_type=None, entity_id=None):
        self._require_role(ctx, ROLE_LEGAL, ROLE_FINANCE)
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if entity_type:
            sql += " AND entity_type = ?"
            params.append(entity_type)
        if entity_id:
            sql += " AND entity_id = ?"
            params.append(entity_id)
        sql += " ORDER BY id"
        with self.store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                **{k: r[k] for k in ("id", "ts", "actor", "role", "action",
                                     "entity_type", "entity_id", "reason")},
                "before": json.loads(r["before_json"]) if r["before_json"] else None,
                "after": json.loads(r["after_json"]) if r["after_json"] else None,
            }
            for r in rows
        ]

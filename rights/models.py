"""赛事品牌权益的领域词汇、实体与通用规则。

词汇以 domain.json 为准：权利类型、合同版本（状态）与调整类型。
约定：
- 金额一律以“分”为单位的整数（cents）；
- 分成比例以基点（万分之一）表示，各方合计必须为 10000；
- 合同条款版本化：分成比例按结算月生效，权利范围按日生效；
- 状态（合同/授权）按生效日记录，可还原任意日期当时有效的状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from typing import Any, Optional

RIGHT_TYPES = ["标识", "静态素材", "比赛片段", "衍生设计"]
CONTRACT_STATUSES = ["草拟", "会签", "生效", "终止", "争议冻结"]
AMENDMENT_TYPES = ["比例修订", "补充协议"]

UNIT_KIND_SALES = "销售回报"
UNIT_KIND_REFUND = "退款冲销"
UNIT_KIND_RETRO = "追溯调整"
UNIT_KINDS = [UNIT_KIND_SALES, UNIT_KIND_REFUND, UNIT_KIND_RETRO]

ROLES = ("legal", "commercial", "admin")
PRIVILEGED_ROLES = ("legal", "admin")

# 合同状态机：草拟 → 会签 → 生效 →（争议冻结 ↔ 生效）→ 终止
CONTRACT_FLOW = {
    "草拟": {"会签", "终止"},
    "会签": {"草拟", "生效", "终止"},
    "生效": {"争议冻结", "终止"},
    "争议冻结": {"生效", "终止"},
    "终止": set(),
}

LICENSE_STATUSES = ("申请中", "已批准", "已拒绝", "已撤销")


class DomainError(Exception):
    """业务规则错误：code 便于程序判断，status 供 HTTP 映射。"""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def parse_date(value: Any, field_name: str = "date") -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise DomainError("bad_date", f"{field_name} 不是合法的 ISO 日期：{value!r}")


def period_of(d: date) -> str:
    """日期所属结算期（自然月，YYYY-MM）。"""
    return f"{d.year:04d}-{d.month:02d}"


def parse_period(value: Any) -> str:
    try:
        year_s, month_s = str(value).split("-")
        return period_of(date(int(year_s), int(month_s), 1))
    except (ValueError, AttributeError):
        raise DomainError("bad_period", f"结算期格式应为 YYYY-MM：{value!r}")


def status_at(events: list["StatusEvent"], on: date) -> Optional[str]:
    """on 当日生效的状态：生效日不晚于 on 的事件中最近的一条。

    生效日与记录时间都相同时，后写入的事件胜出（last write wins）。
    """
    best: Optional["StatusEvent"] = None
    for event in events:
        if event.effective_date > on:
            continue
        if best is None or (event.effective_date, event.recorded_at) >= (
            best.effective_date, best.recorded_at
        ):
            best = event
    return best.status if best else None


def split_amount(net_cents: int, splits: list[dict]) -> list[dict]:
    """多方分成：按基点计算，最大余数法舍入，保证各方之和精确等于净额。

    净额为负（退款冲销）时按绝对值舍入后恢复符号。
    返回每方 {party, share_bp, exact_cents, amount_cents, rounding_cents}。
    """
    if not splits:
        raise DomainError("bad_splits", "缺少分成比例")
    total_bp = sum(int(s["share_bp"]) for s in splits)
    if total_bp != 10000:
        raise DomainError("bad_splits", f"分成比例合计必须为 10000 基点，当前为 {total_bp}")
    sign = 1 if net_cents >= 0 else -1
    total = abs(int(net_cents))
    rows = []
    for s in splits:
        exact = Fraction(total * int(s["share_bp"]), 10000)
        rows.append({
            "party": s["party"],
            "share_bp": int(s["share_bp"]),
            "exact": exact,
            "floor": exact.numerator // exact.denominator,
            "rounded_up": False,
        })
    remainder = total - sum(r["floor"] for r in rows)
    # 最大余数法：小数部分大者优先补足 1 分；并列按责任方名称排序，保证结果确定
    for r in sorted(rows, key=lambda r: (-(r["exact"] - r["floor"]), r["party"]))[:remainder]:
        r["floor"] += 1
        r["rounded_up"] = True
    return [
        {
            "party": r["party"],
            "share_bp": r["share_bp"],
            "exact_cents": round(float(r["exact"]), 4),
            "amount_cents": sign * r["floor"],
            "rounding_cents": sign if r["rounded_up"] else 0,
        }
        for r in rows
    ]


@dataclass
class StatusEvent:
    status: str
    effective_date: date
    recorded_at: datetime
    actor: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "effective_date": self.effective_date.isoformat(),
            "recorded_at": self.recorded_at.isoformat(timespec="seconds"),
            "actor": self.actor,
            "reason": self.reason,
        }


@dataclass
class Asset:
    """品牌素材：权属方、地域与媒介限制是授权范围的硬边界。"""

    id: str
    name: str
    right_type: str
    owner: str
    territories: list[str]
    media: list[str]
    created_at: datetime
    created_by: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "right_type": self.right_type,
            "owner": self.owner,
            "territories": list(self.territories),
            "media": list(self.media),
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "created_by": self.created_by,
        }


@dataclass
class ContractVersion:
    """合同条款的一个版本；补充协议/比例修订会追加新版本而不改写旧版本。"""

    version: int
    effective_from: date
    recorded_at: datetime
    splits: list[dict]
    valid_from: date
    valid_to: date
    territory: list[str]
    media: list[str]
    amendment_id: Optional[str]
    actor: str
    approver: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "effective_from": self.effective_from.isoformat(),
            "recorded_at": self.recorded_at.isoformat(timespec="seconds"),
            "splits": [dict(s) for s in self.splits],
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "territory": list(self.territory),
            "media": list(self.media),
            "amendment_id": self.amendment_id,
            "actor": self.actor,
            "approver": self.approver,
            "reason": self.reason,
        }


@dataclass
class Contract:
    id: str
    counterparty: str
    asset_ids: list[str]
    right_types: list[str]
    sensitive: bool
    status_events: list[StatusEvent]
    versions: list[ContractVersion]

    def latest_status(self, on: date) -> Optional[str]:
        return status_at(self.status_events, on)

    def current_version(self) -> ContractVersion:
        return max(self.versions, key=lambda v: (v.effective_from, v.version))

    def version_for_date(self, d: date) -> ContractVersion:
        """权利范围按日生效：取生效日不晚于 d 的最新版本。"""
        eligible = [v for v in self.versions if v.effective_from <= d]
        if not eligible:
            return min(self.versions, key=lambda v: (v.effective_from, v.version))
        return max(eligible, key=lambda v: (v.effective_from, v.version))

    def version_for_period(self, period: str) -> ContractVersion:
        """分成比例按结算月生效：取生效月不晚于 period 的最新版本。"""
        eligible = [v for v in self.versions if period_of(v.effective_from) <= period]
        if not eligible:
            return min(self.versions, key=lambda v: (v.effective_from, v.version))
        return max(eligible, key=lambda v: (period_of(v.effective_from), v.version))

    def to_dict(self, on: date) -> dict:
        return {
            "id": self.id,
            "counterparty": self.counterparty,
            "asset_ids": list(self.asset_ids),
            "right_types": list(self.right_types),
            "sensitive": self.sensitive,
            "status": self.latest_status(on),
            "status_events": [e.to_dict() for e in self.status_events],
            "current_version": self.current_version().version,
            "versions": [v.to_dict() for v in self.versions],
        }


@dataclass
class Amendment:
    """补充协议/比例修订：effective_from 可早于记录日（追溯生效）。

    applied_in_period 记录其追溯差额已在哪个结算期入账，防止重复调整。
    """

    id: str
    contract_id: str
    type: str
    effective_from: date
    recorded_at: datetime
    changes: dict
    actor: str
    approver: str
    reason: str
    applied_in_period: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "type": self.type,
            "effective_from": self.effective_from.isoformat(),
            "recorded_at": self.recorded_at.isoformat(timespec="seconds"),
            "changes": self.changes,
            "actor": self.actor,
            "approver": self.approver,
            "reason": self.reason,
            "applied_in_period": self.applied_in_period,
        }


@dataclass
class Milestone:
    seq: int
    name: str
    due_date: date
    delivered_at: Optional[date] = None
    note: str = ""

    def to_dict(self, today: Optional[date] = None) -> dict:
        overdue = (
            self.delivered_at is None and today is not None and self.due_date < today
        )
        return {
            "seq": self.seq,
            "name": self.name,
            "due_date": self.due_date.isoformat(),
            "delivered_at": self.delivered_at.isoformat() if self.delivered_at else None,
            "note": self.note,
            "overdue": overdue,
        }


@dataclass
class License:
    """授权：申请 → 批准/拒绝；批准后可撤销。状态按生效日记录。"""

    id: str
    contract_id: str
    asset_id: str
    right_type: str
    territory: list[str]
    media: list[str]
    exclusive: bool
    start: date
    end: date
    status_events: list[StatusEvent]
    milestones: list[Milestone] = field(default_factory=list)

    def latest_status(self, on: date) -> Optional[str]:
        return status_at(self.status_events, on)

    def to_dict(self, today: Optional[date] = None) -> dict:
        return {
            "id": self.id,
            "contract_id": self.contract_id,
            "asset_id": self.asset_id,
            "right_type": self.right_type,
            "territory": list(self.territory),
            "media": list(self.media),
            "exclusive": self.exclusive,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.latest_status(today) if today else None,
            "status_events": [e.to_dict() for e in self.status_events],
            "milestones": [m.to_dict(today) for m in self.milestones],
        }


@dataclass
class SaleLine:
    """销售明细：uid = batch_id:line_id 为幂等键，重复导入不会产生第二条。"""

    uid: str
    batch_id: str
    line_id: str
    license_id: str
    sale_date: date
    amount_cents: int
    channel: str
    reported_at: datetime
    settled_in: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "batch_id": self.batch_id,
            "line_id": self.line_id,
            "license_id": self.license_id,
            "sale_date": self.sale_date.isoformat(),
            "amount_cents": self.amount_cents,
            "channel": self.channel,
            "reported_at": self.reported_at.isoformat(timespec="seconds"),
            "settled_in": self.settled_in,
        }


@dataclass
class Refund:
    """退款冲销：挂在原销售明细上，以 refund_id 幂等。"""

    id: str
    sale_uid: str
    amount_cents: int
    refund_date: date
    reason: str
    recorded_at: datetime
    settled_in: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sale_uid": self.sale_uid,
            "amount_cents": self.amount_cents,
            "refund_date": self.refund_date.isoformat(),
            "reason": self.reason,
            "recorded_at": self.recorded_at.isoformat(timespec="seconds"),
            "settled_in": self.settled_in,
        }

"""内存台账：全部实体、结算快照与审计流水。

审计流水（audit）只追加不修改；任何金额或权利范围的变化都会在此留下
前后值、操作人、批准人与时间，供法务追溯。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from .models import Amendment, Asset, Contract, License, Refund, SaleLine


class Store:
    def __init__(self, now: Optional[Callable[[], datetime]] = None):
        self._now = now or datetime.now
        self.assets: dict[str, Asset] = {}
        self.contracts: dict[str, Contract] = {}
        self.licenses: dict[str, License] = {}
        self.amendments: dict[str, Amendment] = {}
        self.sales: dict[str, SaleLine] = {}
        self.refunds: dict[str, Refund] = {}
        self.statements: dict[str, dict] = {}
        self.audit: list[dict] = []
        self._seq: dict[str, int] = {}

    def now(self) -> datetime:
        return self._now()

    def next_id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}-{self._seq[prefix]:04d}"

    def log(
        self,
        *,
        actor: str,
        role: str,
        action: str,
        entity_type: str,
        entity_id: str,
        before=None,
        after=None,
        approver: Optional[str] = None,
        reason: str = "",
    ) -> dict:
        entry = {
            "seq": len(self.audit) + 1,
            "ts": self.now().isoformat(timespec="seconds"),
            "actor": actor,
            "role": role,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before": before,
            "after": after,
            "approver": approver or actor,
            "reason": reason,
        }
        self.audit.append(entry)
        return entry

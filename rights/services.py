"""赛事品牌权益台账的业务规则。

覆盖：素材登记、合同生命周期、补充协议追溯、授权审批防护（过期/互斥）、
分阶段交付、销售导入幂等、退款冲销、多方分成舍入、结算与关账、
指定日期授权版图还原、按职责的数据可见性与审计查询。

核心不变量：
- 已确认（关账）的结算账本不可变更；迟到销售、退款、追溯修订一律以
  后续结算期的调整单元入账；
- 销售明细以 batch_id:line_id 幂等，退款以 refund_id 幂等，重复导入
  不会改变已确认账本；
- 分成采用最大余数法，任一结算单元各方之和精确等于净额。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from .models import (
    AMENDMENT_TYPES,
    CONTRACT_FLOW,
    CONTRACT_STATUSES,
    PRIVILEGED_ROLES,
    RIGHT_TYPES,
    ROLES,
    UNIT_KIND_REFUND,
    UNIT_KIND_RETRO,
    UNIT_KIND_SALES,
    Amendment,
    Asset,
    Contract,
    ContractVersion,
    DomainError,
    License,
    Milestone,
    Refund,
    SaleLine,
    StatusEvent,
    parse_date,
    parse_period,
    period_of,
    split_amount,
)
from .store import Store

# 结算单元中会被追溯调整覆盖的基础类型
_BASE_UNIT_KINDS = (UNIT_KIND_SALES, UNIT_KIND_REFUND)


class RightsService:
    """法务与商业团队共用的权益台账服务。"""

    def __init__(self, store: Store):
        self.store = store

    # ---------------- 基础 ----------------

    def _today(self) -> date:
        return self.store.now().date()

    @staticmethod
    def _require_role(role: str, allowed) -> None:
        if role not in allowed:
            raise DomainError("forbidden", f"角色 {role or '未知'} 无权执行此操作", 403)

    @staticmethod
    def _require_fields(data: dict, fields) -> None:
        missing = [f for f in fields if data.get(f) in (None, "", [])]
        if missing:
            raise DomainError("missing_field", f"缺少必填字段：{'、'.join(missing)}")

    def _contract_or_404(self, contract_id: str) -> Contract:
        contract = self.store.contracts.get(contract_id)
        if contract is None:
            raise DomainError("not_found", f"合同不存在：{contract_id}", 404)
        return contract

    def _license_or_404(self, license_id: str) -> License:
        lic = self.store.licenses.get(license_id)
        if lic is None:
            raise DomainError("not_found", f"授权不存在：{license_id}", 404)
        return lic

    @staticmethod
    def _check_visible(contract: Contract, role: str) -> None:
        if contract.sensitive and role not in PRIVILEGED_ROLES:
            raise DomainError("forbidden", "敏感合同仅法务/管理职责可见", 403)

    # ---------------- 素材 ----------------

    def create_asset(self, actor: str, role: str, data: dict) -> dict:
        self._require_role(role, PRIVILEGED_ROLES)
        self._require_fields(data, ["name", "right_type", "owner", "territories", "media"])
        if data["right_type"] not in RIGHT_TYPES:
            raise DomainError("bad_right_type", f"权利类型须为 {RIGHT_TYPES} 之一")
        asset = Asset(
            id=self.store.next_id("AST"),
            name=str(data["name"]),
            right_type=data["right_type"],
            owner=str(data["owner"]),
            territories=[str(t) for t in data["territories"]],
            media=[str(m) for m in data["media"]],
            created_at=self.store.now(),
            created_by=actor,
        )
        self.store.assets[asset.id] = asset
        self.store.log(actor=actor, role=role, action="asset_create",
                       entity_type="asset", entity_id=asset.id, after=asset.to_dict())
        return asset.to_dict()

    def list_assets(self, role: str) -> list[dict]:
        self._require_role(role, ROLES)
        return [a.to_dict() for a in self.store.assets.values()]

    # ---------------- 合同 ----------------

    def create_contract(self, actor: str, role: str, data: dict) -> dict:
        self._require_role(role, PRIVILEGED_ROLES)
        self._require_fields(
            data, ["counterparty", "asset_ids", "right_types", "territory", "media",
                   "valid_from", "valid_to", "splits"])
        for rt in data["right_types"]:
            if rt not in RIGHT_TYPES:
                raise DomainError("bad_right_type", f"权利类型须为 {RIGHT_TYPES} 之一：{rt}")
        assets = []
        for aid in data["asset_ids"]:
            asset = self.store.assets.get(aid)
            if asset is None:
                raise DomainError("not_found", f"素材不存在：{aid}", 404)
            assets.append(asset)
        valid_from = parse_date(data["valid_from"], "valid_from")
        valid_to = parse_date(data["valid_to"], "valid_to")
        if valid_to < valid_from:
            raise DomainError("bad_validity", "合同有效期止日早于起日")
        splits = [dict(s) for s in data["splits"]]
        split_amount(0, splits)  # 校验比例合计为 10000 基点
        territory = [str(t) for t in data["territory"]]
        media = [str(m) for m in data["media"]]
        # 合同范围不得超出素材权属限制（各地域/媒介取并集）
        allowed_territories = {t for a in assets for t in a.territories}
        allowed_media = {m for a in assets for m in a.media}
        if not set(territory) <= allowed_territories:
            raise DomainError("scope_out_of_asset", "合同地域超出素材权属限制")
        if not set(media) <= allowed_media:
            raise DomainError("scope_out_of_asset", "合同媒介超出素材权属限制")

        now = self.store.now()
        today = now.date()
        contract = Contract(
            id=self.store.next_id("CT"),
            counterparty=str(data["counterparty"]),
            asset_ids=[str(a) for a in data["asset_ids"]],
            right_types=[str(rt) for rt in data["right_types"]],
            sensitive=bool(data.get("sensitive", False)),
            status_events=[StatusEvent("草拟", today, now, actor, "合同创建")],
            versions=[ContractVersion(
                version=1,
                effective_from=valid_from,
                recorded_at=now,
                splits=splits,
                valid_from=valid_from,
                valid_to=valid_to,
                territory=territory,
                media=media,
                amendment_id=None,
                actor=actor,
                approver=actor,
                reason="初始条款",
            )],
        )
        self.store.contracts[contract.id] = contract
        self.store.log(actor=actor, role=role, action="contract_create",
                       entity_type="contract", entity_id=contract.id,
                       after=contract.to_dict(today))
        return contract.to_dict(today)

    def get_contract(self, role: str, contract_id: str) -> dict:
        self._require_role(role, ROLES)
        contract = self._contract_or_404(contract_id)
        self._check_visible(contract, role)
        view = contract.to_dict(self._today())
        view["amendments"] = [
            a.to_dict() for a in self.store.amendments.values()
            if a.contract_id == contract.id
        ]
        return view

    def list_contracts(self, role: str) -> list[dict]:
        self._require_role(role, ROLES)
        today = self._today()
        out = []
        for c in self.store.contracts.values():
            if c.sensitive and role not in PRIVILEGED_ROLES:
                continue  # 敏感合同对其他职责不可见
            version = c.current_version()
            out.append({
                "id": c.id,
                "counterparty": c.counterparty,
                "asset_ids": list(c.asset_ids),
                "right_types": list(c.right_types),
                "sensitive": c.sensitive,
                "status": c.latest_status(today),
                "valid_from": version.valid_from.isoformat(),
                "valid_to": version.valid_to.isoformat(),
            })
        return out

    def change_contract_status(self, actor: str, role: str, contract_id: str, data: dict) -> dict:
        self._require_role(role, PRIVILEGED_ROLES)
        contract = self._contract_or_404(contract_id)
        self._require_fields(data, ["to"])
        to = data["to"]
        if to not in CONTRACT_STATUSES:
            raise DomainError("bad_status", f"合同状态须为 {CONTRACT_STATUSES} 之一")
        today = self._today()
        current = contract.latest_status(today)
        if to not in CONTRACT_FLOW.get(current, set()):
            raise DomainError("bad_transition", f"合同状态不允许从「{current}」流转到「{to}」", 409)
        effective = parse_date(data.get("effective_date") or today, "effective_date")
        contract.status_events.append(StatusEvent(
            to, effective, self.store.now(), actor, str(data.get("reason", ""))))
        self.store.log(actor=actor, role=role, action="contract_status",
                       entity_type="contract", entity_id=contract.id,
                       before={"status": current}, after={"status": to},
                       reason=str(data.get("reason", "")))
        return contract.to_dict(today)

    def amend_contract(self, actor: str, role: str, contract_id: str, data: dict) -> dict:
        """补充协议/比例修订：追加条款版本，可追溯生效；留前后值与批准人。"""
        self._require_role(role, PRIVILEGED_ROLES)
        contract = self._contract_or_404(contract_id)
        self._require_fields(data, ["type", "effective_from", "changes"])
        if data["type"] not in AMENDMENT_TYPES:
            raise DomainError("bad_amendment_type", f"调整类型须为 {AMENDMENT_TYPES} 之一")
        today = self._today()
        if contract.latest_status(today) == "终止":
            raise DomainError("contract_terminated", "合同已终止，不能再签补充协议", 409)
        effective = parse_date(data["effective_from"], "effective_from")
        changes = {k: data["changes"][k] for k in
                   ("splits", "valid_from", "valid_to", "territory", "media")
                   if k in data["changes"]}
        if not changes:
            raise DomainError("empty_amendment", "补充协议未包含任何条款变更")

        base = contract.current_version()
        new_terms = {
            "splits": [dict(s) for s in base.splits],
            "valid_from": base.valid_from,
            "valid_to": base.valid_to,
            "territory": list(base.territory),
            "media": list(base.media),
        }
        before, after = {}, {}
        for key, value in changes.items():
            if key == "splits":
                value = [dict(s) for s in value]
                split_amount(0, value)  # 校验比例合计
            elif key in ("valid_from", "valid_to"):
                value = parse_date(value, key)
            else:
                value = [str(v) for v in value]
                if not value:
                    raise DomainError("bad_amendment", f"{key} 不能为空")
            before[key] = new_terms[key]
            after[key] = value
            new_terms[key] = value
        if new_terms["valid_to"] < new_terms["valid_from"]:
            raise DomainError("bad_validity", "修订后合同有效期止日早于起日")

        now = self.store.now()
        amendment = Amendment(
            id=self.store.next_id("AMD"),
            contract_id=contract.id,
            type=data["type"],
            effective_from=effective,
            recorded_at=now,
            changes=after,
            actor=actor,
            approver=str(data.get("approver") or actor),
            reason=str(data.get("reason", "")),
        )
        contract.versions.append(ContractVersion(
            version=max(v.version for v in contract.versions) + 1,
            effective_from=effective,
            recorded_at=now,
            amendment_id=amendment.id,
            actor=actor,
            approver=amendment.approver,
            reason=amendment.reason,
            **new_terms,
        ))
        self.store.amendments[amendment.id] = amendment
        self.store.log(actor=actor, role=role, action=f"contract_amend:{data['type']}",
                       entity_type="contract", entity_id=contract.id,
                       before=before, after=after,
                       approver=amendment.approver, reason=amendment.reason)
        return amendment.to_dict()

    # ---------------- 授权 ----------------

    def apply_license(self, actor: str, role: str, data: dict) -> dict:
        self._require_role(role, ROLES)
        self._require_fields(data, ["contract_id", "asset_id", "right_type",
                                    "territory", "media", "start", "end"])
        contract = self._contract_or_404(data["contract_id"])
        self._check_visible(contract, role)
        asset = self.store.assets.get(data["asset_id"])
        if asset is None:
            raise DomainError("not_found", f"素材不存在：{data['asset_id']}", 404)
        start = parse_date(data["start"], "start")
        end = parse_date(data["end"], "end")
        if end < start:
            raise DomainError("bad_period_range", "授权止日早于起日")
        self._check_license_scope(contract, asset, data["right_type"],
                                  data["territory"], data["media"])
        now = self.store.now()
        milestones = [
            Milestone(seq=i + 1, name=str(m["name"]),
                      due_date=parse_date(m["due_date"], "due_date"))
            for i, m in enumerate(data.get("milestones", []))
        ]
        lic = License(
            id=self.store.next_id("LIC"),
            contract_id=contract.id,
            asset_id=asset.id,
            right_type=data["right_type"],
            territory=[str(t) for t in data["territory"]],
            media=[str(m) for m in data["media"]],
            exclusive=bool(data.get("exclusive", False)),
            start=start,
            end=end,
            status_events=[StatusEvent("申请中", now.date(), now, actor, "授权申请")],
            milestones=milestones,
        )
        self.store.licenses[lic.id] = lic
        self.store.log(actor=actor, role=role, action="license_apply",
                       entity_type="license", entity_id=lic.id,
                       after=lic.to_dict(self._today()))
        return lic.to_dict(self._today())

    def _check_license_scope(self, contract: Contract, asset: Asset,
                             right_type: str, territory, media) -> None:
        if asset.id not in contract.asset_ids:
            raise DomainError("scope_out_of_contract", "素材不在合同授权资产范围内")
        if right_type != asset.right_type:
            raise DomainError("bad_right_type", "授权权利类型与素材类型不符")
        if right_type not in contract.right_types:
            raise DomainError("scope_out_of_contract", "权利类型不在合同范围内")
        version = contract.version_for_date(self._today())
        if not set(territory) <= set(version.territory):
            raise DomainError("scope_out_of_contract", "授权地域超出合同范围")
        if not set(media) <= set(version.media):
            raise DomainError("scope_out_of_contract", "授权媒介超出合同范围")
        if not set(territory) <= set(asset.territories):
            raise DomainError("scope_out_of_asset", "授权地域超出素材权属限制")
        if not set(media) <= set(asset.media):
            raise DomainError("scope_out_of_asset", "授权媒介超出素材权属限制")

    def get_license(self, role: str, license_id: str) -> dict:
        self._require_role(role, ROLES)
        lic = self._license_or_404(license_id)
        self._check_visible(self.store.contracts[lic.contract_id], role)
        return lic.to_dict(self._today())

    def approve_license(self, actor: str, role: str, license_id: str, data: dict) -> dict:
        """批准授权：拦截过期授权与互斥（独家冲突）授权。"""
        self._require_role(role, PRIVILEGED_ROLES)
        lic = self._license_or_404(license_id)
        today = self._today()
        if lic.latest_status(today) != "申请中":
            raise DomainError("bad_state", "仅「申请中」的授权可以批准", 409)
        contract = self.store.contracts[lic.contract_id]
        contract_status = contract.latest_status(today)
        if contract_status != "生效":
            raise DomainError("contract_not_active",
                              f"合同当前为「{contract_status}」，授权不得批准", 409)
        version = contract.version_for_date(today)
        if lic.start < version.valid_from or lic.end > version.valid_to:
            raise DomainError(
                "license_out_of_validity",
                f"授权期间 {lic.start}~{lic.end} 超出合同有效期 "
                f"{version.valid_from}~{version.valid_to}，疑似过期授权", 409)
        if not set(lic.territory) <= set(version.territory):
            raise DomainError("scope_out_of_contract", "授权地域超出现行合同范围", 409)
        if not set(lic.media) <= set(version.media):
            raise DomainError("scope_out_of_contract", "授权媒介超出现行合同范围", 409)
        self._check_exclusivity(lic, today)
        effective = parse_date(data.get("effective_date") or today, "effective_date")
        approver = str(data.get("approver") or actor)
        lic.status_events.append(StatusEvent("已批准", effective, self.store.now(),
                                             actor, str(data.get("reason", ""))))
        self.store.log(actor=actor, role=role, action="license_approve",
                       entity_type="license", entity_id=lic.id,
                       before={"status": "申请中"}, after={"status": "已批准"},
                       approver=approver)
        return lic.to_dict(today)

    def _check_exclusivity(self, lic: License, today: date) -> None:
        for other in self.store.licenses.values():
            if other.id == lic.id or other.asset_id != lic.asset_id:
                continue
            if other.right_type != lic.right_type:
                continue
            if other.latest_status(today) != "已批准":
                continue
            if other.end < lic.start or lic.end < other.start:
                continue
            if not set(other.territory) & set(lic.territory):
                continue
            if not set(other.media) & set(lic.media):
                continue
            if lic.exclusive or other.exclusive:
                raise DomainError(
                    "exclusivity_conflict",
                    f"与已批准授权 {other.id} 在期间/地域/媒介上重叠且涉及独家，"
                    "构成互斥授权，不得批准", 409)

    def reject_license(self, actor: str, role: str, license_id: str, data: dict) -> dict:
        self._require_role(role, PRIVILEGED_ROLES)
        lic = self._license_or_404(license_id)
        today = self._today()
        if lic.latest_status(today) != "申请中":
            raise DomainError("bad_state", "仅「申请中」的授权可以拒绝", 409)
        reason = str(data.get("reason") or "")
        if not reason:
            raise DomainError("missing_field", "拒绝授权必须填写原因")
        lic.status_events.append(StatusEvent("已拒绝", today, self.store.now(), actor, reason))
        self.store.log(actor=actor, role=role, action="license_reject",
                       entity_type="license", entity_id=lic.id,
                       before={"status": "申请中"}, after={"status": "已拒绝"},
                       reason=reason)
        return lic.to_dict(today)

    def revoke_license(self, actor: str, role: str, license_id: str, data: dict) -> dict:
        self._require_role(role, PRIVILEGED_ROLES)
        lic = self._license_or_404(license_id)
        today = self._today()
        if lic.latest_status(today) != "已批准":
            raise DomainError("bad_state", "仅「已批准」的授权可以撤销", 409)
        effective = parse_date(data.get("effective_date") or today, "effective_date")
        lic.status_events.append(StatusEvent("已撤销", effective, self.store.now(),
                                             actor, str(data.get("reason", ""))))
        self.store.log(actor=actor, role=role, action="license_revoke",
                       entity_type="license", entity_id=lic.id,
                       before={"status": "已批准"}, after={"status": "已撤销"},
                       reason=str(data.get("reason", "")))
        return lic.to_dict(today)

    def deliver_milestone(self, actor: str, role: str, license_id: str, seq: int,
                          data: dict) -> dict:
        """分阶段交付：登记某一阶段素材的实际交付。"""
        self._require_role(role, ROLES)
        lic = self._license_or_404(license_id)
        contract = self.store.contracts[lic.contract_id]
        self._check_visible(contract, role)
        today = self._today()
        if lic.latest_status(today) != "已批准":
            raise DomainError("bad_state", "授权未批准，不能登记交付", 409)
        milestone = next((m for m in lic.milestones if m.seq == seq), None)
        if milestone is None:
            raise DomainError("not_found", f"交付阶段不存在：{seq}", 404)
        if milestone.delivered_at is not None:
            raise DomainError("bad_state", "该阶段已登记交付", 409)
        milestone.delivered_at = parse_date(data.get("delivered_date") or today,
                                            "delivered_date")
        milestone.note = str(data.get("note", ""))
        self.store.log(actor=actor, role=role, action="milestone_deliver",
                       entity_type="license", entity_id=lic.id,
                       after={"seq": seq, "delivered_at": milestone.delivered_at.isoformat()})
        return lic.to_dict(today)

    # ---------------- 销售与退款 ----------------

    def import_sales(self, actor: str, role: str, data: dict) -> dict:
        """批量导入销售明细；以 batch_id:line_id 幂等，重复导入不产生新明细。"""
        self._require_role(role, ROLES)
        self._require_fields(data, ["batch_id", "lines"])
        batch_id = str(data["batch_id"])
        results = []
        accepted = duplicates = rejected = 0
        for line in data["lines"]:
            line_id = str(line.get("line_id") or "")
            uid = f"{batch_id}:{line_id}"
            if not line_id:
                results.append({"line_id": line_id, "status": "rejected",
                                "reason": "缺少 line_id"})
                rejected += 1
                continue
            if uid in self.store.sales:
                results.append({"line_id": line_id, "status": "duplicate"})
                duplicates += 1
                continue
            problem = self._validate_sale_line(line)
            if problem is not None:
                results.append({"line_id": line_id, "status": "rejected",
                                "reason": problem})
                rejected += 1
                continue
            sale = SaleLine(
                uid=uid,
                batch_id=batch_id,
                line_id=line_id,
                license_id=str(line["license_id"]),
                sale_date=parse_date(line["sale_date"], "sale_date"),
                amount_cents=int(line["amount_cents"]),
                channel=str(line.get("channel", "")),
                reported_at=self.store.now(),
            )
            self.store.sales[uid] = sale
            results.append({"line_id": line_id, "status": "accepted"})
            accepted += 1
        summary = {"batch_id": batch_id, "accepted": accepted,
                   "duplicates": duplicates, "rejected": rejected, "lines": results}
        self.store.log(actor=actor, role=role, action="sales_import",
                       entity_type="sales_batch", entity_id=batch_id,
                       after={"accepted": accepted, "duplicates": duplicates,
                              "rejected": rejected})
        return summary

    def _validate_sale_line(self, line: dict) -> Optional[str]:
        lic = self.store.licenses.get(str(line.get("license_id") or ""))
        if lic is None:
            return f"授权不存在：{line.get('license_id')}"
        amount = line.get("amount_cents")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return "amount_cents 必须为正整数（分）"
        try:
            sale_date = parse_date(line.get("sale_date"), "sale_date")
        except DomainError as exc:
            return exc.message
        if not (lic.start <= sale_date <= lic.end):
            return "销售日期不在授权期间内"
        if lic.latest_status(sale_date) != "已批准":
            return "销售发生时授权未处于已批准状态"
        return None

    def record_refund(self, actor: str, role: str, data: dict) -> dict:
        """退款冲销：挂在原销售明细上，以 refund_id 幂等；累计退款不得超过原销售额。"""
        self._require_role(role, ROLES)
        self._require_fields(data, ["refund_id", "batch_id", "line_id",
                                    "amount_cents", "refund_date"])
        refund_id = str(data["refund_id"])
        if refund_id in self.store.refunds:
            return {"status": "duplicate", "refund_id": refund_id}
        sale_uid = f"{data['batch_id']}:{data['line_id']}"
        sale = self.store.sales.get(sale_uid)
        if sale is None:
            raise DomainError("not_found", f"原销售明细不存在：{sale_uid}", 404)
        amount = data["amount_cents"]
        if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            raise DomainError("bad_amount", "退款金额必须为正整数（分）")
        refund_date = parse_date(data["refund_date"], "refund_date")
        if refund_date < sale.sale_date:
            raise DomainError("bad_refund_date", "退款日期早于原销售日期")
        refunded = sum(r.amount_cents for r in self.store.refunds.values()
                       if r.sale_uid == sale_uid)
        if refunded + amount > sale.amount_cents:
            raise DomainError("refund_exceeds_sale",
                              f"累计退款 {refunded + amount} 分超过原销售额 "
                              f"{sale.amount_cents} 分", 409)
        refund = Refund(
            id=refund_id,
            sale_uid=sale_uid,
            amount_cents=amount,
            refund_date=refund_date,
            reason=str(data.get("reason", "")),
            recorded_at=self.store.now(),
        )
        self.store.refunds[refund.id] = refund
        self.store.log(actor=actor, role=role, action="refund_record",
                       entity_type="refund", entity_id=refund.id,
                       after=refund.to_dict())
        return {"status": "accepted", "refund_id": refund.id}

    # ---------------- 结算 ----------------

    def run_settlement(self, actor: str, role: str, period: str) -> dict:
        """生成（或重算）某结算期的草稿账本；已确认期间不可重跑。

        入账内容：
        1. 销售回报：本期及以前未结算的销售明细（迟到数据标记 late）；
        2. 退款冲销：未结算的退款，按原销售所属期间以负数入账；
        3. 追溯调整：补充协议/比例修订追溯生效后，对已确认期间按新比例
           重算的差额（已确认账本本身不被改写）。
        争议冻结的合同暂缓结算，其明细留待解冻后的结算期。
        """
        self._require_role(role, ROLES)
        period = parse_period(period)
        existing = self.store.statements.get(period)
        if existing and existing["status"] == "confirmed":
            raise DomainError("period_confirmed",
                              f"结算期 {period} 已确认关账，账本不可变更", 409)
        today = self._today()
        frozen = {c.id for c in self.store.contracts.values()
                  if c.latest_status(today) == "争议冻结"}
        units: list[dict] = []

        # 1) 销售回报（含迟到数据）
        sale_groups: dict[tuple, list[SaleLine]] = defaultdict(list)
        for sale in self.store.sales.values():
            if sale.settled_in is not None:
                continue
            source_period = period_of(sale.sale_date)
            if source_period > period:
                continue
            lic = self.store.licenses[sale.license_id]
            if lic.contract_id in frozen:
                continue
            sale_groups[(sale.license_id, source_period)].append(sale)
        for (license_id, source_period), lines in sorted(sale_groups.items()):
            lic = self.store.licenses[license_id]
            contract = self.store.contracts[lic.contract_id]
            version = contract.version_for_period(source_period)
            gross = sum(l.amount_cents for l in lines)
            units.append(self._make_unit(
                kind=UNIT_KIND_SALES, lic=lic, contract=contract,
                source_period=source_period, gross=gross, refund=0,
                version=version, sale_line_ids=[l.uid for l in lines],
                refund_ids=[], late=(source_period != period)))

        # 2) 退款冲销
        refund_groups: dict[tuple, list[Refund]] = defaultdict(list)
        for refund in self.store.refunds.values():
            if refund.settled_in is not None:
                continue
            if period_of(refund.refund_date) > period:
                continue
            sale = self.store.sales[refund.sale_uid]
            lic = self.store.licenses[sale.license_id]
            if lic.contract_id in frozen:
                continue
            refund_groups[(sale.license_id, period_of(sale.sale_date))].append(refund)
        for (license_id, source_period), lines in sorted(refund_groups.items()):
            lic = self.store.licenses[license_id]
            contract = self.store.contracts[lic.contract_id]
            version = contract.version_for_period(source_period)
            total = sum(r.amount_cents for r in lines)
            units.append(self._make_unit(
                kind=UNIT_KIND_REFUND, lic=lic, contract=contract,
                source_period=source_period, gross=0, refund=total,
                version=version, sale_line_ids=[],
                refund_ids=[r.id for r in lines], late=False))

        # 3) 追溯调整（补充协议/比例修订追溯生效）
        units.extend(self._retro_units(frozen))

        totals: dict[str, int] = defaultdict(int)
        for unit in units:
            for share in unit["shares"]:
                totals[share["party"]] += share["amount_cents"]
        statement = {
            "period": period,
            "status": "open",
            "generated_at": self.store.now().isoformat(timespec="seconds"),
            "generated_by": actor,
            "confirmed_at": None,
            "confirmed_by": None,
            "frozen_contracts": sorted(frozen),
            "units": units,
            "totals": dict(sorted(totals.items())),
        }
        self.store.statements[period] = statement
        self.store.log(actor=actor, role=role, action="settlement_run",
                       entity_type="statement", entity_id=period,
                       after={"unit_count": len(units), "totals": statement["totals"]})
        return self._statement_view(statement, role)

    def _make_unit(self, *, kind, lic, contract, source_period, gross, refund,
                   version, sale_line_ids, refund_ids, late) -> dict:
        net = gross - refund
        return {
            "kind": kind,
            "contract_id": contract.id,
            "counterparty": contract.counterparty,
            "license_id": lic.id,
            "asset_id": lic.asset_id,
            "source_period": source_period,
            "late": late,
            "gross_cents": gross,
            "refund_cents": refund,
            "net_cents": net,
            "rate": {
                "contract_version": version.version,
                "effective_from": version.effective_from.isoformat(),
                "splits": [dict(s) for s in version.splits],
            },
            "sale_line_ids": sale_line_ids,
            "refund_ids": refund_ids,
            "adjusts": None,
            "amendment_id": None,
            "shares": split_amount(net, version.splits),
        }

    def _confirmed_statements(self) -> list[dict]:
        return sorted(
            (s for s in self.store.statements.values() if s["status"] == "confirmed"),
            key=lambda s: s["period"])

    def _posted_retro_deltas(self) -> dict:
        """已确认账本中追溯调整的累计值：(授权, 源期间, 基础类型) -> 责任方 -> 分。"""
        posted: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for statement in self._confirmed_statements():
            for unit in statement["units"]:
                if unit["kind"] != UNIT_KIND_RETRO:
                    continue
                key = (unit["adjusts"]["license_id"],
                       unit["adjusts"]["source_period"],
                       unit["adjusts"]["base_kind"])
                for share in unit["shares"]:
                    posted[key][share["party"]] += share["amount_cents"]
        return posted

    def _retro_units(self, frozen: set) -> list[dict]:
        posted = self._posted_retro_deltas()
        confirmed = self._confirmed_statements()
        units = []
        for contract in self.store.contracts.values():
            if contract.id in frozen:
                continue
            pending = [a for a in self.store.amendments.values()
                       if a.contract_id == contract.id and a.applied_in_period is None]
            if not pending:
                continue
            start_month = min(period_of(a.effective_from) for a in pending)
            trigger = max(pending, key=lambda a: (a.effective_from, a.recorded_at))
            for statement in confirmed:
                for base in statement["units"]:
                    if base["contract_id"] != contract.id:
                        continue
                    if base["kind"] not in _BASE_UNIT_KINDS:
                        continue
                    if base["source_period"] < start_month:
                        continue
                    key = (base["license_id"], base["source_period"], base["kind"])
                    version = contract.version_for_period(base["source_period"])
                    target = {s["party"]: s["amount_cents"]
                              for s in split_amount(base["net_cents"], version.splits)}
                    current: dict[str, int] = defaultdict(int)
                    for share in base["shares"]:
                        current[share["party"]] += share["amount_cents"]
                    for party, cents in posted.get(key, {}).items():
                        current[party] += cents
                    delta = {p: target.get(p, 0) - current.get(p, 0)
                             for p in set(target) | set(current)}
                    delta = {p: c for p, c in delta.items() if c != 0}
                    if not delta:
                        continue
                    lic = self.store.licenses[base["license_id"]]
                    bp_of = {s["party"]: int(s["share_bp"]) for s in version.splits}
                    units.append({
                        "kind": UNIT_KIND_RETRO,
                        "contract_id": contract.id,
                        "counterparty": contract.counterparty,
                        "license_id": lic.id,
                        "asset_id": lic.asset_id,
                        "source_period": base["source_period"],
                        "late": False,
                        "gross_cents": 0,
                        "refund_cents": 0,
                        "net_cents": 0,
                        "rate": {
                            "contract_version": version.version,
                            "effective_from": version.effective_from.isoformat(),
                            "splits": [dict(s) for s in version.splits],
                        },
                        "sale_line_ids": [],
                        "refund_ids": [],
                        "adjusts": {"license_id": key[0], "source_period": key[1],
                                    "base_kind": key[2]},
                        "amendment_id": trigger.id,
                        "shares": [
                            {"party": p, "share_bp": bp_of.get(p, 0),
                             "exact_cents": float(c), "amount_cents": c,
                             "rounding_cents": 0}
                            for p, c in sorted(delta.items())
                        ],
                        "retro_basis": {
                            "base_net_cents": base["net_cents"],
                            "base_splits": base["rate"]["splits"],
                            "new_splits": [dict(s) for s in version.splits],
                            "previously_posted_cents": dict(posted.get(key, {})),
                            "target_amounts": target,
                        },
                    })
        return units

    def confirm_settlement(self, actor: str, role: str, period: str) -> dict:
        """确认关账：冻结账本，销售/退款标记已结算，追溯修订标记已入账。"""
        self._require_role(role, PRIVILEGED_ROLES)
        period = parse_period(period)
        statement = self.store.statements.get(period)
        if statement is None:
            raise DomainError("not_found", f"结算期 {period} 尚未生成草稿", 404)
        if statement["status"] == "confirmed":
            raise DomainError("period_confirmed", f"结算期 {period} 已确认关账", 409)
        generated_at = datetime.fromisoformat(statement["generated_at"])
        involved = {u["contract_id"] for u in statement["units"]}
        for amendment in self.store.amendments.values():
            if amendment.contract_id in involved and amendment.recorded_at > generated_at:
                raise DomainError(
                    "stale_statement",
                    "结算草稿生成后又有补充协议/比例修订，请重新 run 后再确认", 409)
        for unit in statement["units"]:
            for uid in unit["sale_line_ids"]:
                self.store.sales[uid].settled_in = period
            for rid in unit["refund_ids"]:
                self.store.refunds[rid].settled_in = period
        frozen = set(statement["frozen_contracts"])
        for amendment in self.store.amendments.values():
            if amendment.applied_in_period is None and amendment.contract_id not in frozen:
                amendment.applied_in_period = period
        statement["status"] = "confirmed"
        statement["confirmed_at"] = self.store.now().isoformat(timespec="seconds")
        statement["confirmed_by"] = actor
        self.store.log(actor=actor, role=role, action="settlement_confirm",
                       entity_type="statement", entity_id=period,
                       after={"totals": statement["totals"]})
        return self._statement_view(statement, role)

    def get_statement(self, role: str, period: str) -> dict:
        self._require_role(role, ROLES)
        period = parse_period(period)
        statement = self.store.statements.get(period)
        if statement is None:
            raise DomainError("not_found", f"结算期 {period} 不存在", 404)
        return self._statement_view(statement, role)

    def list_statements(self, role: str) -> list[dict]:
        self._require_role(role, ROLES)
        return [
            {"period": s["period"], "status": s["status"],
             "generated_at": s["generated_at"], "confirmed_at": s["confirmed_at"],
             "unit_count": len(s["units"])}
            for s in sorted(self.store.statements.values(), key=lambda s: s["period"])
        ]

    def _statement_view(self, statement: dict, role: str) -> dict:
        """结算视图：每一笔分成（lines）都带完整计算依据；敏感合同按职责过滤。"""
        units = [u for u in statement["units"] if self._unit_visible(u, role)]
        lines = []
        totals: dict[str, int] = defaultdict(int)
        for unit in units:
            for share in unit["shares"]:
                totals[share["party"]] += share["amount_cents"]
                lines.append({
                    "period": statement["period"],
                    "kind": unit["kind"],
                    "contract_id": unit["contract_id"],
                    "counterparty": unit["counterparty"],
                    "license_id": unit["license_id"],
                    "party": share["party"],
                    "amount_cents": share["amount_cents"],
                    "basis": {
                        "source_period": unit["source_period"],
                        "late": unit["late"],
                        "gross_cents": unit["gross_cents"],
                        "refund_cents": unit["refund_cents"],
                        "net_cents": unit["net_cents"],
                        "share_bp": share["share_bp"],
                        "exact_cents": share["exact_cents"],
                        "rounding_cents": share["rounding_cents"],
                        "rate": unit["rate"],
                        "sale_line_ids": unit["sale_line_ids"],
                        "refund_ids": unit["refund_ids"],
                        "adjusts": unit["adjusts"],
                        "amendment_id": unit["amendment_id"],
                        "retro_basis": unit.get("retro_basis"),
                    },
                })
        return {
            "period": statement["period"],
            "status": statement["status"],
            "generated_at": statement["generated_at"],
            "generated_by": statement["generated_by"],
            "confirmed_at": statement["confirmed_at"],
            "confirmed_by": statement["confirmed_by"],
            "units": units,
            "lines": lines,
            "totals": dict(sorted(totals.items())),
            "filtered": len(units) != len(statement["units"]),
        }

    def _unit_visible(self, unit: dict, role: str) -> bool:
        contract = self.store.contracts[unit["contract_id"]]
        return not contract.sensitive or role in PRIVILEGED_ROLES

    # ---------------- 版图 / 报表 / 审计 ----------------

    def landscape(self, role: str, on_date) -> dict:
        """还原指定日期当时有效的授权版图。"""
        self._require_role(role, ROLES)
        on = parse_date(on_date, "date")
        grants = []
        for lic in self.store.licenses.values():
            if lic.latest_status(on) != "已批准":
                continue
            if not (lic.start <= on <= lic.end):
                continue
            contract = self.store.contracts[lic.contract_id]
            if contract.latest_status(on) != "生效":
                continue
            if contract.sensitive and role not in PRIVILEGED_ROLES:
                continue
            asset = self.store.assets[lic.asset_id]
            grants.append({
                "license_id": lic.id,
                "contract_id": contract.id,
                "counterparty": contract.counterparty,
                "asset_id": asset.id,
                "asset_name": asset.name,
                "right_type": lic.right_type,
                "territory": list(lic.territory),
                "media": list(lic.media),
                "exclusive": lic.exclusive,
                "start": lic.start.isoformat(),
                "end": lic.end.isoformat(),
                "sensitive": contract.sensitive,
                "milestones": {
                    "total": len(lic.milestones),
                    "delivered": sum(1 for m in lic.milestones if m.delivered_at),
                },
            })
        grants.sort(key=lambda g: (g["asset_id"], g["license_id"]))
        return {"date": on.isoformat(), "grant_count": len(grants), "grants": grants}

    def revenue_report(self, role: str, year) -> dict:
        """全年衍生收入汇总：按已确认账本聚合各合同与责任方。"""
        self._require_role(role, ROLES)
        year_s = str(year)
        contracts: dict[str, dict] = {}
        party_totals: dict[str, int] = defaultdict(int)
        totals = {"gross_cents": 0, "refund_cents": 0, "net_cents": 0}
        for statement in self._confirmed_statements():
            if not statement["period"].startswith(year_s):
                continue
            for unit in statement["units"]:
                if not self._unit_visible(unit, role):
                    continue
                row = contracts.setdefault(unit["contract_id"], {
                    "contract_id": unit["contract_id"],
                    "counterparty": unit["counterparty"],
                    "gross_cents": 0, "refund_cents": 0, "net_cents": 0,
                    "parties": defaultdict(int),
                })
                row["gross_cents"] += unit["gross_cents"]
                row["refund_cents"] += unit["refund_cents"]
                row["net_cents"] += unit["net_cents"]
                totals["gross_cents"] += unit["gross_cents"]
                totals["refund_cents"] += unit["refund_cents"]
                totals["net_cents"] += unit["net_cents"]
                for share in unit["shares"]:
                    row["parties"][share["party"]] += share["amount_cents"]
                    party_totals[share["party"]] += share["amount_cents"]
        return {
            "year": int(year_s),
            "contracts": [
                {**row, "parties": dict(sorted(row["parties"].items()))}
                for row in sorted(contracts.values(), key=lambda r: r["contract_id"])
            ],
            "totals": {**totals, "parties": dict(sorted(party_totals.items()))},
        }

    def audit_trail(self, role: str, entity_type=None, entity_id=None) -> list[dict]:
        """审计流水：仅法务/管理职责可查。"""
        self._require_role(role, PRIVILEGED_ROLES)
        return [
            entry for entry in self.store.audit
            if (entity_type is None or entry["entity_type"] == entity_type)
            and (entity_id is None or entry["entity_id"] == entity_id)
        ]

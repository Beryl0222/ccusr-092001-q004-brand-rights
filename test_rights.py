"""赛事品牌权益台账的业务规则测试。"""

import copy
import unittest
from datetime import datetime, timedelta

from rights import DomainError, RightsService, Store
from rights.models import split_amount

LEGAL = "legal"
COMMERCIAL = "commercial"
ADMIN = "admin"


class Clock:
    def __init__(self, start=datetime(2026, 1, 5, 9, 0, 0)):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kwargs):
        self.t = self.t + timedelta(**kwargs)


def make_service():
    clock = Clock()
    return RightsService(Store(now=clock)), clock


def build_world(svc, *, sensitive=False, splits=None, exclusive=False):
    """素材 + 合同（生效）+ 一个可批准的授权申请。"""
    asset = svc.create_asset("法务", LEGAL, {
        "name": "队徽", "right_type": "标识", "owner": "赛事公司",
        "territories": ["CN", "JP"], "media": ["线上", "线下"],
    })
    contract = svc.create_contract("法务", LEGAL, {
        "counterparty": "文创商户A",
        "asset_ids": [asset["id"]],
        "right_types": ["标识"],
        "territory": ["CN", "JP"],
        "media": ["线上", "线下"],
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "splits": splits or [{"party": "赛事公司", "share_bp": 5000},
                             {"party": "文创商户A", "share_bp": 5000}],
        "sensitive": sensitive,
    })
    svc.change_contract_status("法务", LEGAL, contract["id"],
                               {"to": "会签", "effective_date": "2026-01-05"})
    svc.change_contract_status("法务", LEGAL, contract["id"],
                               {"to": "生效", "effective_date": "2026-01-05"})
    # 敏感合同对商业角色不可见，由法务代为申请
    applicant_role = COMMERCIAL if not sensitive else LEGAL
    lic = svc.apply_license("商务" if not sensitive else "法务", applicant_role, {
        "contract_id": contract["id"],
        "asset_id": asset["id"],
        "right_type": "标识",
        "territory": ["CN"],
        "media": ["线上"],
        "exclusive": exclusive,
        "start": "2026-01-10",
        "end": "2026-11-30",
        "milestones": [{"name": "素材包交付", "due_date": "2026-02-01"},
                       {"name": "衍生设计定稿", "due_date": "2026-05-01"}],
    })
    return asset, contract, lic


def approve(svc, lic_id, **extra):
    return svc.approve_license("法务", LEGAL, lic_id,
                               {"effective_date": "2026-01-05", **extra})


class ContractLifecycleTest(unittest.TestCase):
    def test_status_flow_and_invalid_transition(self):
        svc, _ = make_service()
        asset = svc.create_asset("法务", LEGAL, {
            "name": "主题曲", "right_type": "静态素材", "owner": "赛事公司",
            "territories": ["CN"], "media": ["线上"],
        })
        contract = svc.create_contract("法务", LEGAL, {
            "counterparty": "品牌方B", "asset_ids": [asset["id"]],
            "right_types": ["静态素材"], "territory": ["CN"], "media": ["线上"],
            "valid_from": "2026-01-01", "valid_to": "2026-06-30",
            "splits": [{"party": "赛事公司", "share_bp": 10000}],
        })
        # 草拟不能直接生效
        with self.assertRaises(DomainError) as ctx:
            svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "生效"})
        self.assertEqual(ctx.exception.code, "bad_transition")
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "会签"})
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "生效"})
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "争议冻结"})
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "生效"})
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "终止"})
        self.assertEqual(svc.get_contract(LEGAL, contract["id"])["status"], "终止")

    def test_splits_must_sum_to_10000(self):
        svc, _ = make_service()
        asset = svc.create_asset("法务", LEGAL, {
            "name": "队徽", "right_type": "标识", "owner": "赛事公司",
            "territories": ["CN"], "media": ["线上"],
        })
        with self.assertRaises(DomainError) as ctx:
            svc.create_contract("法务", LEGAL, {
                "counterparty": "品牌方B", "asset_ids": [asset["id"]],
                "right_types": ["标识"], "territory": ["CN"], "media": ["线上"],
                "valid_from": "2026-01-01", "valid_to": "2026-06-30",
                "splits": [{"party": "赛事公司", "share_bp": 6000},
                           {"party": "品牌方B", "share_bp": 3000}],
            })
        self.assertEqual(ctx.exception.code, "bad_splits")


class ApprovalGuardTest(unittest.TestCase):
    def test_approve_requires_active_contract(self):
        svc, _ = make_service()
        asset = svc.create_asset("法务", LEGAL, {
            "name": "队徽", "right_type": "标识", "owner": "赛事公司",
            "territories": ["CN"], "media": ["线上"],
        })
        contract = svc.create_contract("法务", LEGAL, {
            "counterparty": "商户", "asset_ids": [asset["id"]],
            "right_types": ["标识"], "territory": ["CN"], "media": ["线上"],
            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "splits": [{"party": "赛事公司", "share_bp": 10000}],
        })
        lic = svc.apply_license("商务", COMMERCIAL, {
            "contract_id": contract["id"], "asset_id": asset["id"],
            "right_type": "标识", "territory": ["CN"], "media": ["线上"],
            "start": "2026-02-01", "end": "2026-03-01",
        })
        with self.assertRaises(DomainError) as ctx:
            svc.approve_license("法务", LEGAL, lic["id"], {})
        self.assertEqual(ctx.exception.code, "contract_not_active")

    def test_approve_rejects_license_beyond_contract_validity(self):
        svc, _ = make_service()
        _, contract, _ = build_world(svc)
        lic = svc.apply_license("商务", COMMERCIAL, {
            "contract_id": contract["id"], "asset_id": contract["asset_ids"][0],
            "right_type": "标识", "territory": ["CN"], "media": ["线上"],
            "start": "2026-06-01", "end": "2027-02-01",  # 超出合同有效期 2026-12-31
        })
        with self.assertRaises(DomainError) as ctx:
            svc.approve_license("法务", LEGAL, lic["id"], {})
        self.assertEqual(ctx.exception.code, "license_out_of_validity")

    def test_exclusivity_conflict_blocks_overlapping_grant(self):
        svc, _ = make_service()
        _, contract, lic1 = build_world(svc, exclusive=True)
        approve(svc, lic1["id"])  # 独家授权已批准
        # 重叠期间 + 重叠地域/媒介 → 互斥
        lic2 = svc.apply_license("商务", COMMERCIAL, {
            "contract_id": contract["id"], "asset_id": lic1["asset_id"],
            "right_type": "标识", "territory": ["CN"], "media": ["线上"],
            "start": "2026-03-01", "end": "2026-08-01",
        })
        with self.assertRaises(DomainError) as ctx:
            svc.approve_license("法务", LEGAL, lic2["id"], {})
        self.assertEqual(ctx.exception.code, "exclusivity_conflict")
        # 地域不重叠 → 可以批准
        lic3 = svc.apply_license("商务", COMMERCIAL, {
            "contract_id": contract["id"], "asset_id": lic1["asset_id"],
            "right_type": "标识", "territory": ["JP"], "media": ["线上"],
            "start": "2026-03-01", "end": "2026-08-01",
        })
        self.assertEqual(approve(svc, lic3["id"])["status"], "已批准")

    def test_new_exclusive_blocked_by_existing_nonexclusive(self):
        svc, _ = make_service()
        _, contract, lic1 = build_world(svc)
        approve(svc, lic1["id"])  # 非独家已批准
        lic2 = svc.apply_license("商务", COMMERCIAL, {
            "contract_id": contract["id"], "asset_id": lic1["asset_id"],
            "right_type": "标识", "territory": ["CN"], "media": ["线上"],
            "exclusive": True, "start": "2026-02-01", "end": "2026-09-01",
        })
        with self.assertRaises(DomainError) as ctx:
            svc.approve_license("法务", LEGAL, lic2["id"], {})
        self.assertEqual(ctx.exception.code, "exclusivity_conflict")

    def test_nonexclusive_overlap_allowed(self):
        svc, _ = make_service()
        _, contract, lic1 = build_world(svc)
        approve(svc, lic1["id"])
        lic2 = svc.apply_license("商务", COMMERCIAL, {
            "contract_id": contract["id"], "asset_id": lic1["asset_id"],
            "right_type": "标识", "territory": ["CN"], "media": ["线上"],
            "start": "2026-02-01", "end": "2026-09-01",
        })
        self.assertEqual(approve(svc, lic2["id"])["status"], "已批准")


class SalesImportTest(unittest.TestCase):
    def test_import_is_idempotent(self):
        svc, _ = make_service()
        _, _, lic = build_world(svc)
        approve(svc, lic["id"])
        payload = {"batch_id": "B1", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2026-01-20", "amount_cents": 10000, "channel": "电商"},
            {"line_id": "2", "license_id": lic["id"],
             "sale_date": "2026-01-25", "amount_cents": 5000, "channel": "门店"},
        ]}
        first = svc.import_sales("商务", COMMERCIAL, payload)
        self.assertEqual((first["accepted"], first["duplicates"]), (2, 0))
        second = svc.import_sales("商务", COMMERCIAL, payload)
        self.assertEqual((second["accepted"], second["duplicates"]), (0, 2))
        self.assertEqual(len(svc.store.sales), 2)

    def test_rejects_sale_outside_license_window(self):
        svc, _ = make_service()
        _, _, lic = build_world(svc)
        approve(svc, lic["id"])
        result = svc.import_sales("商务", COMMERCIAL, {"batch_id": "B1", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2025-12-31", "amount_cents": 100},
        ]})
        self.assertEqual(result["rejected"], 1)


class RoundingTest(unittest.TestCase):
    def test_largest_remainder_sums_exactly(self):
        splits = [{"party": "甲", "share_bp": 3333},
                  {"party": "乙", "share_bp": 3333},
                  {"party": "丙", "share_bp": 3334}]
        shares = split_amount(10001, splits)
        self.assertEqual(sum(s["amount_cents"] for s in shares), 10001)
        # 丙的小数部分最大，补 1 分
        self.assertEqual({s["party"]: s["amount_cents"] for s in shares},
                         {"甲": 3333, "乙": 3333, "丙": 3335})

    def test_tie_break_is_deterministic(self):
        splits = [{"party": "B", "share_bp": 5000}, {"party": "A", "share_bp": 5000}]
        shares = split_amount(10001, splits)
        self.assertEqual({s["party"]: s["amount_cents"] for s in shares},
                         {"A": 5001, "B": 5000})

    def test_negative_amount_rounds_symmetrically(self):
        splits = [{"party": "A", "share_bp": 5000}, {"party": "B", "share_bp": 5000}]
        shares = split_amount(-10001, splits)
        self.assertEqual(sum(s["amount_cents"] for s in shares), -10001)
        self.assertEqual({s["party"]: s["amount_cents"] for s in shares},
                         {"A": -5001, "B": -5000})


class SettlementTest(unittest.TestCase):
    def _world_with_sales(self, svc):
        _, contract, lic = build_world(svc)
        approve(svc, lic["id"])
        svc.import_sales("商务", COMMERCIAL, {"batch_id": "B1", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2026-01-20", "amount_cents": 10000},
            {"line_id": "2", "license_id": lic["id"],
             "sale_date": "2026-01-25", "amount_cents": 5000},
        ]})
        return contract, lic

    def test_settlement_basis_per_royalty_line(self):
        svc, _ = make_service()
        contract, lic = self._world_with_sales(svc)
        statement = svc.run_settlement("商务", COMMERCIAL, "2026-01")
        self.assertEqual(statement["totals"], {"文创商户A": 7500, "赛事公司": 7500})
        line = next(l for l in statement["lines"] if l["party"] == "赛事公司")
        basis = line["basis"]
        self.assertEqual(basis["gross_cents"], 15000)
        self.assertEqual(basis["share_bp"], 5000)
        self.assertEqual(basis["exact_cents"], 7500.0)
        self.assertEqual(basis["sale_line_ids"], ["B1:1", "B1:2"])
        self.assertEqual(basis["rate"]["contract_version"], 1)
        self.assertFalse(basis["late"])

    def test_confirmed_period_cannot_rerun(self):
        svc, _ = make_service()
        self._world_with_sales(svc)
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        svc.confirm_settlement("法务", LEGAL, "2026-01")
        with self.assertRaises(DomainError) as ctx:
            svc.run_settlement("商务", COMMERCIAL, "2026-01")
        self.assertEqual(ctx.exception.code, "period_confirmed")

    def test_reimport_after_confirm_does_not_change_ledger(self):
        svc, _ = make_service()
        _, lic = self._world_with_sales(svc)
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        confirmed = svc.confirm_settlement("法务", LEGAL, "2026-01")
        snapshot = copy.deepcopy(confirmed)
        again = svc.import_sales("商务", COMMERCIAL, {"batch_id": "B1", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2026-01-20", "amount_cents": 10000},
            {"line_id": "2", "license_id": lic["id"],
             "sale_date": "2026-01-25", "amount_cents": 5000},
        ]})
        self.assertEqual(again["duplicates"], 2)
        self.assertEqual(svc.get_statement(LEGAL, "2026-01"), snapshot)

    def test_late_sales_flow_into_next_period(self):
        svc, _ = make_service()
        _, lic = self._world_with_sales(svc)
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        confirmed = svc.confirm_settlement("法务", LEGAL, "2026-01")
        # 迟到的一月销售在关账后才到达
        svc.import_sales("商务", COMMERCIAL, {"batch_id": "B2", "lines": [
            {"line_id": "9", "license_id": lic["id"],
             "sale_date": "2026-01-31", "amount_cents": 3000},
        ]})
        feb = svc.run_settlement("商务", COMMERCIAL, "2026-02")
        late_units = [u for u in feb["units"] if u["late"]]
        self.assertEqual(len(late_units), 1)
        self.assertEqual(late_units[0]["source_period"], "2026-01")
        self.assertEqual(late_units[0]["gross_cents"], 3000)
        # 已确认的一月账本不受影响
        self.assertEqual(svc.get_statement(LEGAL, "2026-01"), confirmed)

    def test_refund_offset_and_over_refund_rejected(self):
        svc, _ = make_service()
        _, lic = self._world_with_sales(svc)
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        svc.confirm_settlement("法务", LEGAL, "2026-01")
        svc.record_refund("商务", COMMERCIAL, {
            "refund_id": "R1", "batch_id": "B1", "line_id": "1",
            "amount_cents": 4000, "refund_date": "2026-02-05", "reason": "退货",
        })
        # 重复退款单号幂等
        dup = svc.record_refund("商务", COMMERCIAL, {
            "refund_id": "R1", "batch_id": "B1", "line_id": "1",
            "amount_cents": 4000, "refund_date": "2026-02-05",
        })
        self.assertEqual(dup["status"], "duplicate")
        # 累计退款超过原销售额被拒
        with self.assertRaises(DomainError) as ctx:
            svc.record_refund("商务", COMMERCIAL, {
                "refund_id": "R2", "batch_id": "B1", "line_id": "1",
                "amount_cents": 7000, "refund_date": "2026-02-06",
            })
        self.assertEqual(ctx.exception.code, "refund_exceeds_sale")
        feb = svc.run_settlement("商务", COMMERCIAL, "2026-02")
        refunds = [u for u in feb["units"] if u["kind"] == "退款冲销"]
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["net_cents"], -4000)
        self.assertEqual(sum(s["amount_cents"] for s in refunds[0]["shares"]), -4000)

    def test_retroactive_amendment_posts_delta_not_rewrite(self):
        svc, clock = make_service()
        contract, lic = self._world_with_sales(svc)
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        confirmed = svc.confirm_settlement("法务", LEGAL, "2026-01")
        self.assertEqual(confirmed["totals"], {"文创商户A": 7500, "赛事公司": 7500})
        clock.advance(days=1)
        # 补充协议：比例追溯至一月，赛事公司 70% / 商户 30%
        svc.amend_contract("法务", LEGAL, contract["id"], {
            "type": "补充协议",
            "effective_from": "2026-01-01",
            "changes": {"splits": [{"party": "赛事公司", "share_bp": 7000},
                                   {"party": "文创商户A", "share_bp": 3000}]},
            "approver": "法务总监",
            "reason": "补充约定一月起的分成比例",
        })
        feb = svc.run_settlement("商务", COMMERCIAL, "2026-02")
        retro = [u for u in feb["units"] if u["kind"] == "追溯调整"]
        self.assertEqual(len(retro), 1)
        delta = {s["party"]: s["amount_cents"] for s in retro[0]["shares"]}
        self.assertEqual(delta, {"赛事公司": 3000, "文创商户A": -3000})
        self.assertEqual(retro[0]["retro_basis"]["base_net_cents"], 15000)
        # 一月已确认账本保持原样
        self.assertEqual(svc.get_statement(LEGAL, "2026-01"), confirmed)
        # 关账后差额只入账一次
        svc.confirm_settlement("法务", LEGAL, "2026-02")
        mar = svc.run_settlement("商务", COMMERCIAL, "2026-03")
        self.assertEqual([u for u in mar["units"] if u["kind"] == "追溯调整"], [])

    def test_amendment_audit_keeps_before_after_and_approver(self):
        svc, _ = make_service()
        _, contract, _ = build_world(svc)
        svc.amend_contract("法务", LEGAL, contract["id"], {
            "type": "比例修订",
            "effective_from": "2026-04-01",
            "changes": {"splits": [{"party": "赛事公司", "share_bp": 6000},
                                   {"party": "文创商户A", "share_bp": 4000}]},
            "approver": "法务总监",
        })
        entries = svc.audit_trail(LEGAL, entity_type="contract",
                                  entity_id=contract["id"])
        amend = next(e for e in entries if e["action"].startswith("contract_amend"))
        self.assertEqual(amend["before"]["splits"][0]["share_bp"], 5000)
        self.assertEqual(amend["after"]["splits"][0]["share_bp"], 6000)
        self.assertEqual(amend["approver"], "法务总监")
        # 合同保留了全部条款版本
        self.assertEqual(len(svc.get_contract(LEGAL, contract["id"])["versions"]), 2)

    def test_stale_draft_must_be_regenerated_before_confirm(self):
        svc, clock = make_service()
        contract, _ = self._world_with_sales(svc)
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        clock.advance(hours=2)
        svc.amend_contract("法务", LEGAL, contract["id"], {
            "type": "比例修订", "effective_from": "2026-03-01",
            "changes": {"splits": [{"party": "赛事公司", "share_bp": 8000},
                                   {"party": "文创商户A", "share_bp": 2000}]},
        })
        with self.assertRaises(DomainError) as ctx:
            svc.confirm_settlement("法务", LEGAL, "2026-01")
        self.assertEqual(ctx.exception.code, "stale_statement")
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        svc.confirm_settlement("法务", LEGAL, "2026-01")

    def test_dispute_freeze_holds_lines_until_resolved(self):
        svc, _ = make_service()
        contract, lic = self._world_with_sales(svc)
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "争议冻结"})
        frozen_run = svc.run_settlement("商务", COMMERCIAL, "2026-01")
        self.assertEqual(frozen_run["units"], [])
        svc.change_contract_status("法务", LEGAL, contract["id"], {"to": "生效"})
        resumed = svc.run_settlement("商务", COMMERCIAL, "2026-01")
        self.assertEqual(resumed["totals"], {"文创商户A": 7500, "赛事公司": 7500})


class LandscapeTest(unittest.TestCase):
    def test_landscape_reconstructs_grants_at_date(self):
        svc, _ = make_service()
        _, contract, lic = build_world(svc)
        # 批准前
        self.assertEqual(svc.landscape(LEGAL, "2026-01-04")["grant_count"], 0)
        approve(svc, lic["id"])
        # 批准生效后、授权期间内
        view = svc.landscape(LEGAL, "2026-03-01")
        self.assertEqual(view["grant_count"], 1)
        self.assertEqual(view["grants"][0]["milestones"], {"total": 2, "delivered": 0})
        # 撤销后
        svc.revoke_license("法务", LEGAL, lic["id"], {"effective_date": "2026-04-01"})
        self.assertEqual(svc.landscape(LEGAL, "2026-05-01")["grant_count"], 0)
        # 历史日期仍可还原
        self.assertEqual(svc.landscape(LEGAL, "2026-03-01")["grant_count"], 1)

    def test_terminated_contract_removes_grants_from_landscape(self):
        svc, _ = make_service()
        _, contract, lic = build_world(svc)
        approve(svc, lic["id"])
        svc.change_contract_status("法务", LEGAL, contract["id"],
                                   {"to": "终止", "effective_date": "2026-06-01"})
        self.assertEqual(svc.landscape(LEGAL, "2026-05-31")["grant_count"], 1)
        self.assertEqual(svc.landscape(LEGAL, "2026-06-01")["grant_count"], 0)


class MilestoneTest(unittest.TestCase):
    def test_staged_delivery_is_tracked(self):
        svc, _ = make_service()
        _, _, lic = build_world(svc)
        approve(svc, lic["id"])
        view = svc.deliver_milestone("商务", COMMERCIAL, lic["id"], 1,
                                     {"delivered_date": "2026-02-01", "note": "已交付"})
        self.assertEqual(view["milestones"][0]["delivered_at"], "2026-02-01")
        self.assertFalse(view["milestones"][1]["overdue"])  # 5 月才到期，未逾期
        with self.assertRaises(DomainError):
            svc.deliver_milestone("商务", COMMERCIAL, lic["id"], 1, {})


class RbacTest(unittest.TestCase):
    def test_sensitive_contract_hidden_from_commercial(self):
        svc, _ = make_service()
        _, contract, _ = build_world(svc, sensitive=True)
        with self.assertRaises(DomainError) as ctx:
            svc.get_contract(COMMERCIAL, contract["id"])
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(svc.list_contracts(COMMERCIAL), [])
        self.assertEqual(svc.get_contract(LEGAL, contract["id"])["id"], contract["id"])

    def test_sensitive_lines_filtered_from_statement(self):
        svc, _ = make_service()
        _, _, lic = build_world(svc, sensitive=True)
        approve(svc, lic["id"])
        svc.import_sales("商务", COMMERCIAL, {"batch_id": "B1", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2026-01-20", "amount_cents": 10000},
        ]})
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        view = svc.get_statement(COMMERCIAL, "2026-01")
        self.assertTrue(view["filtered"])
        self.assertEqual(view["lines"], [])
        self.assertEqual(svc.get_statement(LEGAL, "2026-01")["totals"],
                         {"文创商户A": 5000, "赛事公司": 5000})

    def test_audit_and_writes_restricted_by_role(self):
        svc, _ = make_service()
        build_world(svc)
        with self.assertRaises(DomainError) as ctx:
            svc.audit_trail(COMMERCIAL)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(DomainError) as ctx:
            svc.amend_contract("商务", COMMERCIAL, "CT-0001", {
                "type": "比例修订", "effective_from": "2026-04-01",
                "changes": {"splits": [{"party": "赛事公司", "share_bp": 10000}]},
            })
        self.assertEqual(ctx.exception.status, 403)


class RevenueReportTest(unittest.TestCase):
    def test_annual_revenue_aggregates_confirmed_statements(self):
        svc, _ = make_service()
        _, lic = build_world(svc)[1:]
        approve(svc, lic["id"])
        svc.import_sales("商务", COMMERCIAL, {"batch_id": "B1", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2026-01-20", "amount_cents": 10000},
        ]})
        svc.import_sales("商务", COMMERCIAL, {"batch_id": "B2", "lines": [
            {"line_id": "1", "license_id": lic["id"],
             "sale_date": "2026-02-10", "amount_cents": 6000},
        ]})
        svc.run_settlement("商务", COMMERCIAL, "2026-01")
        svc.confirm_settlement("法务", LEGAL, "2026-01")
        svc.run_settlement("商务", COMMERCIAL, "2026-02")
        svc.confirm_settlement("法务", LEGAL, "2026-02")
        report = svc.revenue_report(LEGAL, 2026)
        self.assertEqual(report["totals"]["gross_cents"], 16000)
        self.assertEqual(report["totals"]["parties"],
                         {"文创商户A": 8000, "赛事公司": 8000})


if __name__ == "__main__":
    unittest.main()

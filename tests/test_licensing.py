"""授权审批：防止过期或互斥授权被批准。"""

import unittest

from brandledger.errors import ConflictError, DomainError, StateError
from tests.helpers import LedgerCase


class ApprovalTest(LedgerCase):
    def _submit(self, contract, grants):
        return self.core.submit_application(self.commercial, contract, grants)

    def test_approve_ok_creates_grants(self):
        cid = self.make_contract()
        aid = self._submit(cid, [self.grant(rights_type="静态素材", media=["社媒"])])
        result = self.core.approve_application(self.legal, aid, reason="例行审批")
        self.assertEqual(result["state"], "已批准")
        detail = self.core.get_contract(self.legal, cid)
        self.assertEqual(len(detail["grants"]), 1)
        self.assertEqual(detail["grants"][0]["rights_type"], "静态素材")

    def test_expired_window_rejected(self):
        cid = self.make_contract()
        aid = self._submit(cid, [self.grant(valid_from="2026-01-01", valid_to="2026-02-01")])
        with self.assertRaises(DomainError) as cm:
            self.core.approve_application(self.legal, aid)
        self.assertIn("过期", cm.exception.message)

    def test_window_outside_contract_term_rejected(self):
        cid = self.make_contract(valid_to="2026-06-30")
        aid = self._submit(cid, [self.grant(valid_to="2026-12-31")])
        with self.assertRaises(DomainError) as cm:
            self.core.approve_application(self.legal, aid)
        self.assertIn("合同截止", cm.exception.message)

    def test_draft_contract_cannot_approve(self):
        cid = self.make_contract(activate=False)
        aid = self._submit(cid, [self.grant()])
        with self.assertRaises(StateError):
            self.core.approve_application(self.legal, aid)

    def test_frozen_contract_cannot_approve(self):
        cid = self.make_contract()
        aid = self._submit(cid, [self.grant()])
        self.core.transition_contract(self.legal, cid, "争议冻结", reason="权属争议")
        with self.assertRaises(StateError):
            self.core.approve_application(self.legal, aid)

    def test_exclusive_conflict_rejected(self):
        c1 = self.make_contract(party=self.merchant)
        c2 = self.make_contract(party=self.brand)
        a1 = self._submit(c1, [self.grant(exclusive=True)])
        self.core.approve_application(self.legal, a1)
        # 同一权利类型 × 地域 × 媒介 × 窗口：非排他申请也撞上既有排他授权
        a2 = self._submit(c2, [self.grant()])
        with self.assertRaises(ConflictError) as cm:
            self.core.approve_application(self.legal, a2)
        self.assertEqual(cm.exception.details["conflicts"][0]["contract_id"], c1)

    def test_no_conflict_when_scope_disjoint(self):
        c1 = self.make_contract(party=self.merchant)
        c2 = self.make_contract(party=self.brand, valid_to="2027-12-31")
        self.core.approve_application(self.legal, self._submit(c1, [self.grant(exclusive=True)]))
        disjoint = [
            self.grant(rights_type="比赛片段"),                # 权利类型不同
            self.grant(territories=["US"]),                    # 地域不同
            self.grant(media=["线下零售"]),                    # 媒介不同
            self.grant(valid_from="2027-01-01", valid_to="2027-12-31"),  # 窗口不同
        ]
        for g in disjoint:
            aid = self._submit(c2, [g])
            self.core.approve_application(self.legal, aid)  # 不抛异常即通过

    def test_nonexclusive_overlap_allowed(self):
        c1 = self.make_contract(party=self.merchant)
        c2 = self.make_contract(party=self.brand)
        self.core.approve_application(self.legal, self._submit(c1, [self.grant()]))
        aid = self._submit(c2, [self.grant()])
        self.core.approve_application(self.legal, aid)  # 双方均非排他，可共存

    def test_termination_frees_scope(self):
        c1 = self.make_contract(party=self.merchant)
        c2 = self.make_contract(party=self.brand)
        self.core.approve_application(self.legal, self._submit(c1, [self.grant(exclusive=True)]))
        self.core.transition_contract(self.legal, c1, "终止", event_date="2026-06-30",
                                      reason="提前解约")
        # 终止日之后：授权已截断，不再构成冲突
        aid = self._submit(c2, [self.grant(valid_from="2026-07-01", valid_to="2026-12-31")])
        self.core.approve_application(self.legal, aid)
        # 终止日之前：仍然互斥
        aid2 = self._submit(c2, [self.grant(valid_from="2026-05-01", valid_to="2026-06-30")])
        with self.assertRaises(ConflictError):
            self.core.approve_application(self.legal, aid2)


if __name__ == "__main__":
    unittest.main()

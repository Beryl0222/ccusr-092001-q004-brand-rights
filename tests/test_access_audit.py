"""按职责的访问控制与审计留痕（金额/权利范围变更的前后版本与批准人）。"""

import json
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

from brandledger.api import Api, make_handler
from brandledger.errors import ForbiddenError
from tests.helpers import LedgerCase

TOK = {
    "admin": {"X-User-Token": "tok-admin"},
    "legal": {"X-User-Token": "tok-legal"},
    "commercial": {"X-User-Token": "tok-commercial"},
    "finance": {"X-User-Token": "tok-finance"},
}


class RbacTest(LedgerCase):
    def test_role_enforcement_in_core(self):
        with self.assertRaises(ForbiddenError):
            self.core.create_contract(
                self.commercial, self.merchant, "x", "2026-01-01",
                share_rule={"royalty_rate_bp": 1,
                            "splits": self.splits((self.licensor, 10000))})
        cid = self.make_contract()
        with self.assertRaises(ForbiddenError):
            self.core.transition_contract(self.finance, cid, "会签")
        with self.assertRaises(ForbiddenError):
            self.core.run_settlement(self.commercial, "2026-01")
        with self.assertRaises(ForbiddenError):
            self.core.import_sales(self.legal, "pos", [])

    def test_sensitive_contract_redacted_by_role(self):
        cid = self.make_contract(sensitive=True, grants=[self.grant()])
        detail = self.core.get_contract(self.commercial, cid)
        self.assertTrue(detail["redacted"])
        self.assertIsNone(detail["grants"])
        self.assertIsNone(detail["share_rules"])
        # 法务可见完整条款
        full = self.core.get_contract(self.legal, cid)
        self.assertFalse(full["redacted"])
        self.assertEqual(len(full["grants"]), 1)
        # 列表同样脱敏
        listed = {c["id"]: c for c in self.core.list_contracts(self.commercial)}
        self.assertTrue(listed[cid]["redacted"])
        # 版图还原中敏感合同只保留存在性
        land = self.core.landscape(self.commercial, "2026-03-15")
        entry = [c for c in land["contracts"] if c["contract_id"] == cid][0]
        self.assertTrue(entry["redacted"])
        self.assertIsNone(entry["grants"])


class AuditTest(LedgerCase):
    def test_amount_and_scope_changes_leave_trail(self):
        cid = self.make_contract(rate=1000)
        self.core.add_share_revision(self.legal, cid, "2026-04-01", 1500,
                                     self.splits((self.licensor, 6000), (self.merchant, 4000)),
                                     reason="费率上调")
        trail = self.core.list_audit(self.legal, entity_type="contract", entity_id=cid)
        actions = [t["action"] for t in trail]
        self.assertIn("新建合同", actions)
        self.assertIn("比例修订", actions)
        revision = [t for t in trail if t["action"] == "比例修订"][0]
        self.assertEqual(revision["before"]["share_rule"]["royalty_rate_bp"], 1000)
        self.assertEqual(revision["after"]["share_rule"]["royalty_rate_bp"], 1500)
        self.assertEqual(revision["actor"], "legal")

    def test_approval_records_approver(self):
        cid = self.make_contract()
        aid = self.core.submit_application(self.commercial, cid, [self.grant()])
        self.core.approve_application(self.legal, aid, reason="例行审批")
        trail = self.core.list_audit(self.legal, entity_type="application", entity_id=aid)
        approve = [t for t in trail if t["action"] == "批准授权申请"][0]
        self.assertEqual(approve["after"]["approver"], "legal")
        self.assertEqual(approve["before"]["state"], "已提交")

    def test_amendment_audit_contains_before_after_scope(self):
        cid = self.make_contract(grants=[self.grant()])
        self.core.add_amendment(self.legal, cid, "2026-04-01",
                                grants=[self.grant(rights_type="衍生设计",
                                                   valid_from="2026-04-01")],
                                reason="置换标的")
        trail = self.core.list_audit(self.legal, entity_type="contract", entity_id=cid)
        amend = [t for t in trail if t["action"] == "补充协议"][0]
        self.assertEqual(amend["before"]["grants"][0]["rights_type"], "标识")
        self.assertEqual(len(amend["after"]["new_grants"]), 1)
        self.assertEqual(len(amend["after"]["truncated_grants"]), 1)

    def test_audit_forbidden_for_commercial(self):
        with self.assertRaises(ForbiddenError):
            self.core.list_audit(self.commercial)


class ApiTest(LedgerCase):
    def setUp(self):
        super().setUp()
        self.api = Api(self.core)

    def test_health_is_public(self):
        status, payload = self.api.handle("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "event-brand-rights")

    def test_missing_token_is_401(self):
        status, _ = self.api.handle("GET", "/contracts")
        self.assertEqual(status, 401)

    def test_wrong_role_is_403(self):
        status, _ = self.api.handle("POST", "/contracts", TOK["commercial"], {})
        self.assertEqual(status, 403)
        status, _ = self.api.handle("GET", "/audit", TOK["commercial"])
        self.assertEqual(status, 403)

    def test_sensitive_contract_redacted_over_http(self):
        cid = self.make_contract(sensitive=True, grants=[self.grant()])
        status, payload = self.api.handle("GET", f"/contracts/{cid}", TOK["commercial"])
        self.assertEqual(status, 200)
        self.assertTrue(payload["redacted"])
        status, payload = self.api.handle("GET", f"/contracts/{cid}", TOK["legal"])
        self.assertFalse(payload["redacted"])

    def test_full_flow_over_api(self):
        status, payload = self.api.handle(
            "POST", "/contracts", TOK["legal"],
            {"party_id": self.merchant, "title": "年度授权", "valid_from": "2026-01-01",
             "share_rule": {"royalty_rate_bp": 1000,
                            "splits": [{"party_id": self.licensor, "bp": 6000},
                                       {"party_id": self.merchant, "bp": 4000}]}})
        self.assertEqual(status, 200)
        cid = payload["id"]
        for to in ("会签", "生效"):
            self.api.handle("POST", f"/contracts/{cid}/transition", TOK["legal"], {"to": to})
        status, payload = self.api.handle(
            "POST", "/sales/import", TOK["finance"],
            {"source": "pos", "lines": [{"line_ref": "r1", "contract_id": cid,
                                         "period": "2026-01", "kind": "销售回报",
                                         "amount_cents": 1_000_000}]})
        self.assertEqual(payload["inserted"], 1)
        self.api.handle("POST", "/settlements/2026-01/run", TOK["finance"])
        self.api.handle("POST", "/settlements/2026-01/confirm", TOK["finance"])
        status, payload = self.api.handle("GET", "/settlements/2026-01/explain", TOK["legal"])
        self.assertEqual(sum(l["amount_cents"] for l in payload["lines"]), 100_000)


class HttpSmokeTest(LedgerCase):
    def test_http_roundtrip(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(self.core)))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with urlopen(f"http://127.0.0.1:{port}/health") as resp:
            body = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(body, {"status": "ok", "service": "event-brand-rights"})
        req = Request(f"http://127.0.0.1:{port}/contracts")
        with self.assertRaises(Exception) as cm:
            urlopen(req)
        self.assertEqual(cm.exception.code, 401)


if __name__ == "__main__":
    unittest.main()

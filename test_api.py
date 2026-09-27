"""HTTP 接口层冒烟测试：健康检查、职责识别与 JSON 错误格式。"""

import http.client
import json
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer

from rights import RightsService, Store
from rights.api import make_handler
from service import health


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store = Store(now=lambda: datetime(2026, 3, 10, 9, 0, 0))
        cls.service = RightsService(store)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.service, health()))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def req(self, method, path, body=None, role=None, actor=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Content-Type": "application/json"}
        if role:
            headers["X-Role"] = role
        if actor:
            headers["X-Actor"] = actor
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_health_open_without_role(self):
        status, payload = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "event-brand-rights")

    def test_role_required(self):
        status, payload = self.req("GET", "/contracts")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_actor_required_for_writes(self):
        status, payload = self.req("POST", "/assets", {"name": "x"}, role="legal")
        self.assertEqual(status, 401)

    def test_create_and_read_flow_with_rbac(self):
        status, asset = self.req("POST", "/assets", {
            "name": "队徽", "right_type": "标识", "owner": "赛事公司",
            "territories": ["CN"], "media": ["线上"],
        }, role="legal", actor="legal-op")
        self.assertEqual(status, 201)
        status, contract = self.req("POST", "/contracts", {
            "counterparty": "商户", "asset_ids": [asset["id"]],
            "right_types": ["标识"], "territory": ["CN"], "media": ["线上"],
            "valid_from": "2026-01-01", "valid_to": "2026-12-31",
            "splits": [{"party": "赛事公司", "share_bp": 10000}],
            "sensitive": True,
        }, role="legal", actor="legal-op")
        self.assertEqual(status, 201)
        # 商业角色看不到敏感合同
        status, payload = self.req("GET", f"/contracts/{contract['id']}",
                                   role="commercial")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden")
        status, payload = self.req("GET", f"/contracts/{contract['id']}", role="legal")
        self.assertEqual(status, 200)
        # 商业角色不能建合同
        status, _ = self.req("POST", "/contracts", {}, role="commercial", actor="biz-op")
        self.assertEqual(status, 403)

    def test_unknown_route_404(self):
        status, payload = self.req("GET", "/nope", role="legal")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")


if __name__ == "__main__":
    unittest.main()

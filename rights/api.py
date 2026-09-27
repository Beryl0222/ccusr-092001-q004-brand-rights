"""HTTP 接口层：路由、JSON 编解码与基于请求头的职责识别。

所有接口（/health 除外）要求请求头：
- X-Role: legal | commercial | admin
- X-Actor: 操作人标识（写操作必填，会记入审计）
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

from .models import ROLES, DomainError
from .services import RightsService


def _need(query: dict, key: str) -> str:
    values = query.get(key)
    if not values:
        raise DomainError("missing_field", f"缺少查询参数：{key}")
    return values[0]


def _opt(query: dict, key: str):
    values = query.get(key)
    return values[0] if values else None


# (方法, 路径模式, 处理函数)；路径段以 ":" 开头为路径参数
# 处理函数签名: (service, 路径参数, URL查询, actor, role, body) -> (status, payload)
_ROUTES = [
    ("POST", ("assets",), lambda s, p, q, a, r, b: (201, s.create_asset(a, r, b))),
    ("GET", ("assets",), lambda s, p, q, a, r, b: (200, {"assets": s.list_assets(r)})),
    ("POST", ("contracts",), lambda s, p, q, a, r, b: (201, s.create_contract(a, r, b))),
    ("GET", ("contracts",), lambda s, p, q, a, r, b: (200, {"contracts": s.list_contracts(r)})),
    ("GET", ("contracts", ":cid"), lambda s, p, q, a, r, b: (200, s.get_contract(r, p["cid"]))),
    ("POST", ("contracts", ":cid", "status"),
     lambda s, p, q, a, r, b: (200, s.change_contract_status(a, r, p["cid"], b))),
    ("POST", ("contracts", ":cid", "amendments"),
     lambda s, p, q, a, r, b: (201, s.amend_contract(a, r, p["cid"], b))),
    ("POST", ("licenses",), lambda s, p, q, a, r, b: (201, s.apply_license(a, r, b))),
    ("GET", ("licenses", ":lid"), lambda s, p, q, a, r, b: (200, s.get_license(r, p["lid"]))),
    ("POST", ("licenses", ":lid", "approve"),
     lambda s, p, q, a, r, b: (200, s.approve_license(a, r, p["lid"], b))),
    ("POST", ("licenses", ":lid", "reject"),
     lambda s, p, q, a, r, b: (200, s.reject_license(a, r, p["lid"], b))),
    ("POST", ("licenses", ":lid", "revoke"),
     lambda s, p, q, a, r, b: (200, s.revoke_license(a, r, p["lid"], b))),
    ("POST", ("licenses", ":lid", "milestones", ":seq", "deliver"),
     lambda s, p, q, a, r, b: (200, s.deliver_milestone(a, r, p["lid"], int(p["seq"]), b))),
    ("POST", ("sales", "import"), lambda s, p, q, a, r, b: (200, s.import_sales(a, r, b))),
    ("POST", ("refunds",), lambda s, p, q, a, r, b: (201, s.record_refund(a, r, b))),
    ("POST", ("settlements", "run"),
     lambda s, p, q, a, r, b: (200, s.run_settlement(a, r, b.get("period", "")))),
    ("POST", ("settlements", ":period", "confirm"),
     lambda s, p, q, a, r, b: (200, s.confirm_settlement(a, r, p["period"]))),
    ("GET", ("settlements",),
     lambda s, p, q, a, r, b: (200, {"statements": s.list_statements(r)})),
    ("GET", ("settlements", ":period"),
     lambda s, p, q, a, r, b: (200, s.get_statement(r, p["period"]))),
    ("GET", ("landscape",), lambda s, p, q, a, r, b: (200, s.landscape(r, _need(q, "date")))),
    ("GET", ("reports", "revenue"),
     lambda s, p, q, a, r, b: (200, s.revenue_report(r, _need(q, "year")))),
    ("GET", ("audit",),
     lambda s, p, q, a, r, b: (200, {"audit": s.audit_trail(
         r, _opt(q, "entity_type"), _opt(q, "entity_id"))})),
]


def _match(pattern: tuple, segments: list[str]):
    if len(pattern) != len(segments):
        return None
    params = {}
    for want, got in zip(pattern, segments):
        if want.startswith(":"):
            params[want[1:]] = unquote(got)
        elif want != got:
            return None
    return params


def handle_request(service: RightsService, method: str, path: str, query: dict,
                   actor: str, role: str, body: dict):
    segments = [s for s in path.strip("/").split("/") if s]
    for route_method, pattern, handler in _ROUTES:
        if route_method != method:
            continue
        params = _match(pattern, segments)
        if params is not None:
            return handler(service, params, query, actor, role, body)
    raise DomainError("not_found", f"接口不存在：{method} {path}", 404)


def make_handler(service: RightsService, health_payload: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, health_payload)
                return
            role = self.headers.get("X-Role", "")
            actor = self.headers.get("X-Actor", "")
            if role not in ROLES:
                self._send(401, {"error": "unauthorized",
                                 "message": "缺少或未知职责（X-Role）"})
                return
            body = {}
            if method == "POST":
                if not actor:
                    self._send(401, {"error": "unauthorized",
                                     "message": "缺少操作人（X-Actor）"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._send(400, {"error": "bad_json", "message": "请求体不是合法 JSON"})
                        return
            try:
                status, payload = handle_request(
                    service, method, parsed.path, parse_qs(parsed.query),
                    actor, role, body)
            except DomainError as exc:
                status, payload = exc.status, {"error": exc.code, "message": exc.message}
            self._send(status, payload)

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    return Handler

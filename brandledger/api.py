"""HTTP 接口层：路由、令牌认证、按角色授权、统一错误格式。"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from . import health
from .auth import authenticate
from .core import (
    ROLE_ADMIN,
    ROLE_COMMERCIAL,
    ROLE_FINANCE,
    ROLE_LEGAL,
    ROLES,
    Core,
    Ctx,
)
from .errors import DomainError, ForbiddenError, UnauthorizedError

ALL = frozenset(ROLES)
LEGAL = frozenset({ROLE_LEGAL, ROLE_ADMIN})
FINANCE = frozenset({ROLE_FINANCE, ROLE_ADMIN})
LEDGER_READERS = frozenset({ROLE_LEGAL, ROLE_FINANCE, ROLE_ADMIN})


class Api:
    """把 HTTP 请求映射到领域核心；handle() 可直接用于测试。"""

    def __init__(self, core: Core):
        self.core = core
        self.routes = []
        self._build()

    def _add(self, method, pattern, roles, fn):
        self.routes.append((method, pattern.strip("/").split("/"), roles, fn))

    def _build(self):
        A = ALL
        self._add("GET", "/health", None, lambda c, b, q: health())
        # 合作方与素材
        self._add("POST", "/parties", A, lambda c, b, q: {"id": self.core.create_party(
            c, b.get("name"), b.get("kind"))})
        self._add("GET", "/parties", A, lambda c, b, q: self.core.list_parties(c))
        self._add("POST", "/assets", A, lambda c, b, q: {"id": self.core.create_asset(
            c, b.get("name"), b.get("rights_type"), b.get("owner_party_id"), b.get("notes", ""))})
        self._add("GET", "/assets", A, lambda c, b, q: self.core.list_assets(c))
        # 合同
        self._add("POST", "/contracts", LEGAL, lambda c, b, q: {"id": self.core.create_contract(
            c, b.get("party_id"), b.get("title"), b.get("valid_from"), b.get("valid_to"),
            bool(b.get("sensitive")), b.get("grants"), b.get("share_rule"))})
        self._add("GET", "/contracts", A, lambda c, b, q: self.core.list_contracts(c))
        self._add("GET", "/contracts/{id}", A,
                  lambda c, b, q, id: self.core.get_contract(c, id))
        self._add("POST", "/contracts/{id}/transition", LEGAL,
                  lambda c, b, q, id: self.core.transition_contract(
                      c, id, b.get("to"), b.get("event_date"), b.get("reason", "")))
        self._add("POST", "/contracts/{id}/amendments", LEGAL,
                  lambda c, b, q, id: self.core.add_amendment(
                      c, id, b.get("effective_from"), b.get("grants"),
                      b.get("share_rule"), b.get("reason", "")))
        self._add("POST", "/contracts/{id}/share-revisions", LEGAL,
                  lambda c, b, q, id: self.core.add_share_revision(
                      c, id, b.get("effective_from"), b.get("royalty_rate_bp"),
                      b.get("splits"), b.get("reason", "")))
        # 分阶段交付
        self._add("POST", "/contracts/{id}/milestones", A,
                  lambda c, b, q, id: {"id": self.core.add_milestone(
                      c, id, b.get("name"), b.get("due_date"))})
        self._add("GET", "/contracts/{id}/milestones", A,
                  lambda c, b, q, id: self.core.list_milestones(c, id))
        self._add("POST", "/milestones/{id}/deliver", A,
                  lambda c, b, q, id: self.core.deliver_milestone(c, id, b.get("evidence", "")))
        self._add("POST", "/milestones/{id}/accept", A,
                  lambda c, b, q, id: self.core.accept_milestone(c, id))
        # 授权申请
        self._add("POST", "/applications", A, lambda c, b, q: {"id": self.core.submit_application(
            c, b.get("contract_id"), b.get("grants"))})
        self._add("GET", "/applications", A,
                  lambda c, b, q: self.core.list_applications(c, _q1(q, "contract_id")))
        self._add("GET", "/applications/{id}", A,
                  lambda c, b, q, id: self.core.get_application(c, id))
        self._add("POST", "/applications/{id}/approve", LEGAL,
                  lambda c, b, q, id: self.core.approve_application(c, id, b.get("reason", "")))
        self._add("POST", "/applications/{id}/reject", LEGAL,
                  lambda c, b, q, id: self.core.reject_application(c, id, b.get("reason", "")))
        # 销售与结算
        self._add("POST", "/sales/import", A,
                  lambda c, b, q: self.core.import_sales(c, b.get("source"), b.get("lines")))
        self._add("GET", "/sales", A, lambda c, b, q: self.core.list_sales(
            c, _q1(q, "contract_id"), _q1(q, "period")))
        self._add("POST", "/settlements/{period}/run", FINANCE,
                  lambda c, b, q, period: self.core.run_settlement(c, period))
        self._add("POST", "/settlements/{period}/confirm", FINANCE,
                  lambda c, b, q, period: self.core.confirm_settlement(c, period))
        self._add("GET", "/settlements", A,
                  lambda c, b, q: self.core.list_settlements(c))
        self._add("GET", "/settlements/{period}", A,
                  lambda c, b, q, period: self.core.get_settlement(c, period))
        self._add("GET", "/settlements/{period}/explain", A,
                  lambda c, b, q, period: self.core.explain_settlement(c, period))
        # 版图、报表、审计
        self._add("GET", "/landscape", A, lambda c, b, q: self.core.landscape(
            c, _q1(q, "date"), _q1(q, "known_at")))
        self._add("GET", "/reports/annual", LEDGER_READERS,
                  lambda c, b, q: self.core.annual_report(c, _q1(q, "year")))
        self._add("GET", "/audit", LEDGER_READERS, lambda c, b, q: self.core.list_audit(
            c, _q1(q, "entity_type"), _q1(q, "entity_id")))

    def handle(self, method, raw_path, headers=None, body=None):
        """处理一次请求，返回 (status, payload)。供 HTTP 层与测试共用。"""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urlparse(raw_path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            match = self._match(method, path)
            if match is None:
                return 404, {"error": {"message": f"接口不存在：{method} {path}"}}
            roles, fn, params = match
            ctx = Ctx(actor="anonymous", role="anonymous")
            if roles is not None:
                user = authenticate(headers.get("x-user-token", ""))
                if user is None:
                    raise UnauthorizedError("缺少或无效的身份令牌（X-User-Token）")
                ctx = Ctx(actor=user["actor"], role=user["role"])
                if ctx.role not in roles:
                    raise ForbiddenError(f"角色 {ctx.role} 无权访问 {method} {path}")
            payload = fn(ctx, body or {}, query, **params)
            return 200, payload
        except DomainError as e:
            return e.status, {"error": {"message": e.message, "details": e.details}}

    def _match(self, method, path):
        segments = path.strip("/").split("/")
        for m, pattern, roles, fn in self.routes:
            if m != method or len(pattern) != len(segments):
                continue
            params = {}
            for p, a in zip(pattern, segments):
                if p.startswith("{") and p.endswith("}"):
                    params[p[1:-1]] = a
                elif p != a:
                    break
            else:
                return roles, fn, params
        return None


def _q1(query, key):
    """取查询参数的第一个值。"""
    values = query.get(key)
    return values[0] if values else None


def make_handler(api):
    """生成绑定指定 Api 实例的 BaseHTTPRequestHandler。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def _dispatch(self, method):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
                status, payload = 400, {"error": {"message": "请求体不是合法 JSON"}}
            if body is not None:
                status, payload = api.handle(
                    method, self.path, dict(self.headers.items()), body)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_args):
            return

    return Handler

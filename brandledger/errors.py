"""领域异常：携带 HTTP 状态码，供 API 层直接映射。"""

from __future__ import annotations


class DomainError(Exception):
    """业务规则校验失败。"""

    status = 400

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    status = 404


class ConflictError(DomainError):
    """与既有有效授权互斥等冲突。"""

    status = 409


class StateError(DomainError):
    """当前状态不允许该操作（如已确认账本被改写）。"""

    status = 409


class ForbiddenError(DomainError):
    status = 403


class UnauthorizedError(DomainError):
    status = 401

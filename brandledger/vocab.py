"""领域词汇：权利类型、合同版本（生命周期状态）、调整类型，来源于 domain.json。"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import DomainError

DOMAIN_PATH = Path(__file__).resolve().parent.parent / "domain.json"

REQUIRED_KEYS = ("权利类型", "合同版本", "调整类型")


class Vocab:
    """domain.json 的领域称谓，供各模块校验取值。"""

    def __init__(self, data):
        missing = [k for k in REQUIRED_KEYS if not data.get(k)]
        if missing:
            raise ValueError(f"domain.json 缺少必备词汇：{'、'.join(missing)}")
        self.project = data.get("项目", "")
        self.rights_types = tuple(data["权利类型"])
        self.contract_states = tuple(data["合同版本"])
        self.adjustment_kinds = tuple(data["调整类型"])

    def check_rights_type(self, value):
        if value not in self.rights_types:
            raise DomainError(f"未知权利类型：{value}（可选：{'、'.join(self.rights_types)}）")

    def check_contract_state(self, value):
        if value not in self.contract_states:
            raise DomainError(f"未知合同版本状态：{value}")

    def check_adjustment_kind(self, value):
        if value not in self.adjustment_kinds:
            raise DomainError(f"未知调整类型：{value}")


def load_vocab(path=None):
    p = Path(path) if path else DOMAIN_PATH
    return Vocab(json.loads(p.read_text(encoding="utf-8")))

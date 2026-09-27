"""测试公共基类：临时库、可注入时钟、常用身份与造数助手。"""

import os
import tempfile
import unittest
from datetime import datetime

from brandledger.core import Core, Ctx
from brandledger.store import Store


class LedgerCase(unittest.TestCase):
    """每个用例独立库文件；时钟固定在 2026-03-15，可推进。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [datetime(2026, 3, 15, 10, 0, 0)]
        self.store = Store(os.path.join(self.tmp.name, "ledger.db"))
        self.core = Core(self.store, clock=lambda: self.now[0])
        self.legal = Ctx("legal", "legal")
        self.finance = Ctx("finance", "finance")
        self.commercial = Ctx("commercial", "commercial")
        self.admin = Ctx("admin", "admin")
        self.licensor = self.core.create_party(self.admin, "赛事公司", "主办方")
        self.merchant = self.core.create_party(self.admin, "文创商户", "商户")
        self.brand = self.core.create_party(self.admin, "品牌方", "品牌")

    def set_now(self, value):
        self.now[0] = value

    def grant(self, **kw):
        base = {
            "rights_type": "标识",
            "territories": ["CN"],
            "media": ["电商"],
            "exclusive": False,
            "valid_from": "2026-01-01",
            "valid_to": "2026-12-31",
        }
        base.update(kw)
        return base

    def splits(self, *pairs):
        return [{"party_id": pid, "bp": bp} for pid, bp in pairs]

    def make_contract(self, party=None, grants=None, rate=1000, splits=None,
                      sensitive=False, valid_from="2026-01-01", valid_to="2026-12-31",
                      activate=True):
        """造合同；activate 时直接推进到「生效」（事件日默认为当天）。"""
        party = party or self.merchant
        splits = splits or self.splits((self.licensor, 6000), (party, 4000))
        cid = self.core.create_contract(
            self.legal, party, "授权合同", valid_from, valid_to,
            sensitive=sensitive, grants=grants or [],
            share_rule={"royalty_rate_bp": rate, "splits": splits},
        )
        if activate:
            self.core.transition_contract(self.legal, cid, "会签")
            self.core.transition_contract(self.legal, cid, "生效")
        return cid

    def import_sales(self, lines, source="pos"):
        return self.core.import_sales(self.finance, source, lines)

    def sale(self, ref, contract, period, amount, kind="销售回报", **extra):
        line = {"line_ref": ref, "contract_id": contract, "period": period,
                "kind": kind, "amount_cents": amount}
        line.update(extra)
        return line

    def settlement_lines(self, period):
        return self.core.explain_settlement(self.finance, period)["lines"]

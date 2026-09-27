"""销售明细导入：幂等、重复导入不改变已确认账本。"""

import unittest

from brandledger.errors import DomainError
from tests.helpers import LedgerCase


class ImportTest(LedgerCase):
    def _three_lines(self, cid, period="2026-01"):
        return [
            self.sale("r1", cid, period, 100_000),
            self.sale("r2", cid, period, 200_000),
            self.sale("r3", cid, period, -50_000, kind="退款冲销", ref_line_ref="r1"),
        ]

    def test_reimport_is_idempotent(self):
        cid = self.make_contract()
        first = self.import_sales(self._three_lines(cid))
        self.assertEqual((first["inserted"], len(first["duplicates"])), (3, 0))
        second = self.import_sales(self._three_lines(cid))
        self.assertEqual((second["inserted"], len(second["duplicates"])), (0, 3))
        self.assertEqual(len(self.core.list_sales(self.finance)), 3)

    def test_reimport_after_confirm_keeps_ledger(self):
        cid = self.make_contract(rate=1000)
        self.import_sales(self._three_lines(cid))
        self.core.run_settlement(self.finance, "2026-01")
        self.core.confirm_settlement(self.finance, "2026-01")
        ledger_before = self.settlement_lines("2026-01")
        sales_before = self.core.list_sales(self.finance)
        # 重复导入同一批明细
        again = self.import_sales(self._three_lines(cid))
        self.assertEqual(again["inserted"], 0)
        # 已确认账本与销售台账均无变化
        self.assertEqual(self.settlement_lines("2026-01"), ledger_before)
        self.assertEqual(self.core.list_sales(self.finance), sales_before)

    def test_same_ref_different_source_accepted(self):
        cid = self.make_contract()
        self.import_sales([self.sale("r1", cid, "2026-01", 100)], source="pos")
        result = self.import_sales([self.sale("r1", cid, "2026-01", 100)], source="mall")
        self.assertEqual(result["inserted"], 1)

    def test_line_validation(self):
        cid = self.make_contract()
        with self.assertRaises(DomainError):
            self.import_sales([self.sale("r1", cid, "2026-01", -100)])            # 销售为负
        with self.assertRaises(DomainError):
            self.import_sales([self.sale("r1", cid, "2026-01", 100, kind="退款冲销")])  # 退款为正
        with self.assertRaises(DomainError):
            self.import_sales([self.sale("r1", cid, "2026/01", 100)])             # 期间格式
        with self.assertRaises(DomainError):
            self.import_sales([self.sale("r1", cid, "2026-01", 10.5)])            # 非整数分

    def test_late_new_lines_never_touch_confirmed_period(self):
        cid = self.make_contract(rate=1000)
        self.import_sales([self.sale("r1", cid, "2026-01", 1_000_000)])
        self.core.run_settlement(self.finance, "2026-01")
        self.core.confirm_settlement(self.finance, "2026-01")
        ledger_before = self.settlement_lines("2026-01")
        # 迟到的「新」明细（非重复）：改道开放期，已确认账本纹丝不动
        self.import_sales([self.sale("r9", cid, "2026-01", 300_000)])
        self.assertEqual(self.settlement_lines("2026-01"), ledger_before)
        self.core.run_settlement(self.finance, "2026-03")
        lines = self.settlement_lines("2026-03")
        self.assertEqual(sum(l["amount_cents"] for l in lines), 30_000)
        self.assertEqual(lines[0]["calc"]["late_line_ids"], ["SL-000002"])


if __name__ == "__main__":
    unittest.main()

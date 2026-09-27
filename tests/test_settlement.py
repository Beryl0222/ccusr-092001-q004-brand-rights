"""结算：多方分成舍入、退款冲销、确认锁定、迟到数据、争议冻结。"""

import unittest

from brandledger.errors import StateError
from brandledger.money import mul_bp, split_amount
from tests.helpers import LedgerCase


class MoneyTest(unittest.TestCase):
    def test_mul_bp_half_up(self):
        self.assertEqual(mul_bp(1_000_000, 1000), 100_000)
        self.assertEqual(mul_bp(999, 3333), 333)      # 332.9667 → 333
        self.assertEqual(mul_bp(-999, 3333), -333)    # 负数对称
        self.assertEqual(mul_bp(0, 5000), 0)

    def test_split_amount_largest_remainder(self):
        shares = [("a", 3333), ("b", 3333), ("c", 3334)]
        self.assertEqual(split_amount(100, shares), [("a", 33), ("b", 33), ("c", 34)])
        result = split_amount(100_001, shares)
        self.assertEqual(sum(v for _, v in result), 100_001)

    def test_split_amount_negative_and_zero(self):
        shares = [("a", 3333), ("b", 3333), ("c", 3334)]
        result = split_amount(-101, shares)
        self.assertEqual(sum(v for _, v in result), -101)
        self.assertEqual(split_amount(0, shares), [("a", 0), ("b", 0), ("c", 0)])

    def test_split_requires_full_basis(self):
        with self.assertRaises(ValueError):
            split_amount(100, [("a", 5000)])


class SettlementTest(LedgerCase):
    def test_basic_settlement_and_explain(self):
        cid = self.make_contract(rate=1000)  # 费率 10%，赛事公司 60% / 商户 40%
        self.import_sales([self.sale("r1", cid, "2026-01", 1_000_000)])
        result = self.core.run_settlement(self.finance, "2026-01")
        self.assertEqual(result["state"], "待确认")
        lines = self.settlement_lines("2026-01")
        amounts = {l["party_id"]: l["amount_cents"] for l in lines}
        self.assertEqual(amounts, {self.licensor: 60_000, self.merchant: 40_000})
        # 每一笔分成的计算依据
        calc = lines[0]["calc"]
        self.assertEqual(calc["net_cents"], 1_000_000)
        self.assertEqual(calc["royalty_rate_bp"], 1000)
        self.assertEqual(calc["royalty_cents"], 100_000)
        self.assertEqual(calc["sales_line_ids"], ["SL-000001"])
        self.assertEqual(calc["rounding"], "最大余数法")

    def test_refund_offsets_sales(self):
        cid = self.make_contract(rate=1000)
        self.import_sales([
            self.sale("r1", cid, "2026-01", 1_000_000),
            self.sale("r2", cid, "2026-01", -200_000, kind="退款冲销", ref_line_ref="r1"),
        ])
        self.core.run_settlement(self.finance, "2026-01")
        lines = self.settlement_lines("2026-01")
        calc = lines[0]["calc"]
        self.assertEqual(calc["gross_cents"], 1_000_000)
        self.assertEqual(calc["refund_cents"], -200_000)
        self.assertEqual(calc["net_cents"], 800_000)
        self.assertEqual(calc["royalty_cents"], 80_000)
        total = sum(l["amount_cents"] for l in lines)
        self.assertEqual(total, 80_000)

    def test_multi_party_rounding_sums_exactly(self):
        cid = self.make_contract(
            rate=1000,
            splits=self.splits((self.licensor, 3333), (self.merchant, 3333), (self.brand, 3334)),
        )
        self.import_sales([self.sale("r1", cid, "2026-01", 1_000_010)])  # 分成 100001 分
        self.core.run_settlement(self.finance, "2026-01")
        lines = self.settlement_lines("2026-01")
        self.assertEqual(len(lines), 3)
        self.assertEqual(sum(l["amount_cents"] for l in lines), 100_001)

    def test_confirm_locks_and_late_data_reroutes(self):
        cid = self.make_contract(rate=1000)
        self.import_sales([self.sale("r1", cid, "2026-01", 1_000_000)])
        self.core.run_settlement(self.finance, "2026-01")
        self.core.confirm_settlement(self.finance, "2026-01")
        # 已确认账本禁止重算
        with self.assertRaises(StateError):
            self.core.run_settlement(self.finance, "2026-01")
        before = self.settlement_lines("2026-01")
        # 迟到销售：所属期已确认 → 改道当前开放期（今天 2026-03-15 → 2026-03）
        self.import_sales([self.sale("r2", cid, "2026-01", 500_000)])
        sales = self.core.list_sales(self.finance, contract_id=cid)
        late = [s for s in sales if s["line_ref"] == "r2"][0]
        self.assertEqual(late["period"], "2026-01")
        self.assertEqual(late["routed_period"], "2026-03")
        # 已确认账本未被改变
        self.assertEqual(self.settlement_lines("2026-01"), before)
        # 迟到数据进入 2026-03 并标注来源
        self.core.run_settlement(self.finance, "2026-03")
        lines = self.settlement_lines("2026-03")
        self.assertEqual(lines[0]["calc"]["late_line_ids"], ["SL-000002"])
        self.assertEqual(sum(l["amount_cents"] for l in lines), 50_000)

    def test_frozen_contract_skipped_until_unfrozen(self):
        cid = self.make_contract(rate=1000)
        self.import_sales([self.sale("r1", cid, "2026-01", 1_000_000)])
        self.core.transition_contract(self.legal, cid, "争议冻结", reason="权属争议")
        self.core.run_settlement(self.finance, "2026-01")
        self.assertEqual(self.settlement_lines("2026-01"), [])
        # 明细仍未结算，解冻后由后续结算期吸收
        self.core.transition_contract(self.legal, cid, "生效")
        self.core.run_settlement(self.finance, "2026-02")
        lines = self.settlement_lines("2026-02")
        self.assertEqual(sum(l["amount_cents"] for l in lines), 100_000)

    def test_rerun_before_confirm_is_idempotent(self):
        cid = self.make_contract(rate=1000)
        self.import_sales([self.sale("r1", cid, "2026-01", 1_000_000)])
        self.core.run_settlement(self.finance, "2026-01")
        first = self.settlement_lines("2026-01")
        self.core.run_settlement(self.finance, "2026-01")  # 未确认前可重算
        second = self.settlement_lines("2026-01")
        self.assertEqual([l["amount_cents"] for l in first],
                         [l["amount_cents"] for l in second])


if __name__ == "__main__":
    unittest.main()

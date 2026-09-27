"""追溯生效（比例修订 / 补充协议）与授权版图的时间还原。"""

import unittest
from datetime import datetime

from tests.helpers import LedgerCase


class RetroAdjustTest(LedgerCase):
    def _confirmed_period(self, cid, period="2026-01", amount=1_000_000, rate=1000):
        self.import_sales([self.sale("r1", cid, period, amount)])
        self.core.run_settlement(self.finance, period)
        self.core.confirm_settlement(self.finance, period)

    def test_retro_rate_revision_posts_delta(self):
        cid = self.make_contract(rate=1000)  # 10%，赛事公司 60% / 商户 40%
        self._confirmed_period(cid)          # 已确认：100000 分（60000/40000）
        before = self.settlement_lines("2026-01")
        # 费率追溯上调至 15%，自 2026-01-01 生效
        result = self.core.add_share_revision(
            self.legal, cid, "2026-01-01", 1500,
            self.splits((self.licensor, 6000), (self.merchant, 4000)),
            reason="补充谈判上调费率")
        self.assertEqual(len(result["adjustment_entries"]), 1)
        # 已确认账本不变，差额进入当前开放期（2026-03）
        self.assertEqual(self.settlement_lines("2026-01"), before)
        self.core.run_settlement(self.finance, "2026-03")
        lines = [l for l in self.settlement_lines("2026-03") if l["kind"] == "比例修订"]
        amounts = {l["party_id"]: l["amount_cents"] for l in lines}
        self.assertEqual(amounts, {self.licensor: 30_000, self.merchant: 20_000})
        # 计算依据可解释：原始期间、净销售额、新旧规则、差额
        calc = lines[0]["calc"]
        self.assertEqual(calc["origin_period"], "2026-01")
        self.assertEqual(calc["net_cents"], 1_000_000)
        self.assertEqual(calc["new_rule"]["royalty_rate_bp"], 1500)
        self.assertEqual(lines[0]["origin_period"], "2026-01")

    def test_chained_revisions_converge(self):
        cid = self.make_contract(rate=1000)
        self._confirmed_period(cid)
        self.core.add_share_revision(self.legal, cid, "2026-01-01", 1500,
                                     self.splits((self.licensor, 6000), (self.merchant, 4000)))
        # 第二笔修订在第一笔的调整入账前到达：差额须基于「已确认 + 在途调整」计算
        self.core.add_share_revision(self.legal, cid, "2026-01-01", 2000,
                                     self.splits((self.licensor, 6000), (self.merchant, 4000)))
        self.core.run_settlement(self.finance, "2026-03")
        lines = [l for l in self.settlement_lines("2026-03") if l["kind"] == "比例修订"]
        totals = {}
        for l in lines:
            totals[l["party_id"]] = totals.get(l["party_id"], 0) + l["amount_cents"]
        # 10% → 20%：累计补差 = 100000（60000/40000）
        self.assertEqual(totals, {self.licensor: 60_000, self.merchant: 40_000})

    def test_retro_split_change_is_zero_sum(self):
        cid = self.make_contract(rate=1000)
        self._confirmed_period(cid)
        # 补充协议：比例改为五五开，追溯至期初
        result = self.core.add_amendment(
            self.legal, cid, "2026-01-01",
            share_rule={"royalty_rate_bp": 1000,
                        "splits": self.splits((self.licensor, 5000), (self.merchant, 5000))},
            reason="重新分配比例")
        self.assertEqual(len(result["adjustment_entries"]), 1)
        self.core.run_settlement(self.finance, "2026-03")
        lines = [l for l in self.settlement_lines("2026-03") if l["kind"] == "补充协议"]
        amounts = {l["party_id"]: l["amount_cents"] for l in lines}
        self.assertEqual(amounts, {self.licensor: -10_000, self.merchant: 10_000})
        self.assertEqual(sum(amounts.values()), 0)

    def test_revision_not_effective_for_earlier_period(self):
        cid = self.make_contract(rate=1000)
        self._confirmed_period(cid, period="2026-01")
        # 修订自 2026-02 生效，不影响 2026-01
        result = self.core.add_share_revision(
            self.legal, cid, "2026-02-01", 1500,
            self.splits((self.licensor, 6000), (self.merchant, 4000)))
        self.assertEqual(result["adjustment_entries"], [])


class LandscapeTest(LedgerCase):
    def _active_contract(self, grants, **kw):
        # 把时钟拨回 1 月建合同并推进到生效，使状态事件按业务时间排列
        self.set_now(datetime(2026, 1, 5, 9, 0, 0))
        cid = self.core.create_contract(
            self.legal, self.merchant, "授权合同", "2026-01-01", "2026-12-31",
            grants=grants,
            share_rule={"royalty_rate_bp": 1000,
                        "splits": self.splits((self.licensor, 6000), (self.merchant, 4000))},
            **kw)
        self.core.transition_contract(self.legal, cid, "会签")
        self.core.transition_contract(self.legal, cid, "生效")
        self.set_now(datetime(2026, 3, 15, 10, 0, 0))
        return cid

    def _grants_at(self, on_date, known_at=None):
        land = self.core.landscape(self.legal, on_date, known_at)
        return land["contracts"]

    def test_landscape_respects_state_events(self):
        cid = self._active_contract([self.grant()])
        self.core.transition_contract(self.legal, cid, "终止", event_date="2026-06-30")
        self.assertEqual(len(self._grants_at("2026-06-29")), 1)
        self.assertEqual(self._grants_at("2026-06-30"), [])  # 终止当日不再有效

    def test_retroactive_amendment_rewrites_scope(self):
        cid = self._active_contract([self.grant(rights_type="标识")])
        self.core.add_amendment(
            self.legal, cid, "2026-02-01",
            grants=[self.grant(rights_type="比赛片段", valid_from="2026-02-01",
                               valid_to="2026-12-31")],
            reason="置换授权标的")
        # 追溯生效：2 月 1 日起版图即为新范围，1 月仍是旧范围
        jan = self._grants_at("2026-01-15")
        self.assertEqual([g["rights_type"] for g in jan[0]["grants"]], ["标识"])
        feb = self._grants_at("2026-02-01")
        self.assertEqual([g["rights_type"] for g in feb[0]["grants"]], ["比赛片段"])

    def test_landscape_as_known_at(self):
        cid = self._active_contract([self.grant(rights_type="标识")])
        known_before = self.core._now_iso()
        self.set_now(datetime(2026, 3, 16, 9, 0, 0))  # 第二天才录入补充协议
        self.core.add_amendment(
            self.legal, cid, "2026-02-01",
            grants=[self.grant(rights_type="衍生设计", valid_from="2026-02-01",
                               valid_to="2026-12-31")])
        # 以录入前的知情时刻还原：2 月 1 日仍是旧范围
        then = self._grants_at("2026-02-01", known_at=known_before)
        self.assertEqual([g["rights_type"] for g in then[0]["grants"]], ["标识"])
        # 以当前认知还原：已是追溯后的新范围
        now = self._grants_at("2026-02-01")
        self.assertEqual([g["rights_type"] for g in now[0]["grants"]], ["衍生设计"])


if __name__ == "__main__":
    unittest.main()

"""金额与比例工具。

约定：
- 金额一律为整数「分」（cents），退款为负数。
- 费率与分成比例一律为万分比整数（bp，10000 = 100%）。
- 多方分摊使用最大余数法，保证各方之和严格等于总额，且结果确定（与输入顺序一致）。
"""

from __future__ import annotations

BP_BASE = 10_000


def mul_bp(amount_cents, bp):
    """金额乘以万分比，四舍五入到分；负数按绝对值计算后还原符号（对称确定）。"""
    if amount_cents == 0 or bp == 0:
        return 0
    sign = -1 if amount_cents < 0 else 1
    q, r = divmod(abs(amount_cents) * bp, BP_BASE)
    if r * 2 >= BP_BASE:
        q += 1
    return sign * q


def split_amount(total_cents, shares):
    """按万分比把总额分摊给多方（最大余数法）。

    shares: [(key, bp), ...]，bp 合计必须等于 10000。
    返回 [(key, cents), ...]，顺序与输入一致；各方之和严格等于 total_cents。
    余数按小数部分从大到小分配，并列时按输入顺序，保证结果确定。
    """
    if not shares:
        if total_cents != 0:
            raise ValueError("无分成方但金额非零")
        return []
    total_bp = sum(bp for _, bp in shares)
    if total_bp != BP_BASE:
        raise ValueError(f"分成比例合计须为 10000（当前 {total_bp}）")
    sign = -1 if total_cents < 0 else 1
    rest = abs(total_cents)
    floors = []
    fracs = []
    for i, (_key, bp) in enumerate(shares):
        q, r = divmod(rest * bp, BP_BASE)
        floors.append(q)
        fracs.append((r, i))
    remain = rest - sum(floors)
    for _r, i in sorted(fracs, key=lambda t: (-t[0], t[1]))[:remain]:
        floors[i] += 1
    return [(shares[i][0], sign * floors[i]) for i in range(len(shares))]

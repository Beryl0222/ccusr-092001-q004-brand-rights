"""赛事品牌全年权益：授权台账与分成结算。

包内模块：
- vocab：domain.json 领域词汇（权利类型 / 合同版本 / 调整类型）
- money：整数分金额、万分比费率、最大余数法分摊
- store：SQLite 存储（双时间轴授权、不可变已确认账本、审计日志）
- core：领域规则（审批拦截、追溯调整、结算、版图还原）
- api：HTTP 接口与按角色的访问控制
"""

SERVICE_ID = "event-brand-rights"


def health():
    """返回稳定的服务身份。"""
    return {"status": "ok", "service": SERVICE_ID}

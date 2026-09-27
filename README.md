# 赛事品牌全年权益

赛事结束后，品牌、文创商户与内容制作方仍在各渠道使用队徽、片段与主题素材。本系统为法务与商业团队提供统一后台，把**素材权属、地域与媒介限制、授权申请、分阶段交付、销售回报与分成调整**串成一条长期台账，并保证：

- 过期或互斥的授权**无法被批准**；
- 补充协议、比例修订可**追溯生效**，已确认账本不被改写，差额自动进入后续开放结算期；
- 迟到销售数据、退款冲销、多方分成舍入都有确定的处理规则；
- 敏感合同**按职责可见**，任何金额或权利范围的变化都留下前后版本与批准人；
- 可在**指定日期还原当时有效的授权版图**，可解释**某结算期每一笔分成的计算依据**，**重复导入销售明细不会改变已确认账本**。

## 运行

```bash
python3 service.py --check              # 校验领域词汇并初始化存储
python3 service.py --port 8000          # 启动服务（默认库文件 ledger.db，--db 可改）
python3 -m unittest -v                  # 运行全部测试
```

`domain.json` 给出领域称谓：权利类型（标识/静态素材/比赛片段/衍生设计）、合同版本（草拟/会签/生效/终止/争议冻结）、调整类型（销售回报/退款冲销/比例修订/补充协议）。`/health` 是启动后的巡检入口。

## 结构

```
brandledger/
  vocab.py   领域词汇加载与校验（domain.json）
  money.py   整数分金额、万分比费率、最大余数法分摊
  store.py   SQLite 存储（双时间轴授权、不可变已确认账本、审计日志）
  core.py    领域规则：审批拦截、追溯调整、结算、版图还原、审计
  api.py     HTTP 路由、令牌认证、按角色授权
  auth.py    演示身份（生产应替换为真实 SSO/IAM）
service.py   入口：--check / --serve
tests/       领域规则与验收测试
```

## 角色与身份

请求头携带 `X-User-Token`（演示令牌见 `brandledger/auth.py`）：

| 角色 | 职责 |
| --- | --- |
| legal（法务） | 合同全生命周期、授权审批、补充协议与比例修订、查看敏感合同 |
| commercial（商业） | 合作方与素材、提交申请、交付里程碑、导入销售明细 |
| finance（财务） | 运行/确认结算、导入销售明细、查看计算依据与年度报表 |
| admin | 全部权限 |

**敏感合同**（`sensitive`）的条款与权利范围仅 legal/admin 可见；其他角色在合同详情、列表与授权版图中只能看到脱敏后的存在性信息。所有金额或权利范围的变化（新建合同、状态流转、批准、比例修订、补充协议、结算确认……）都写入 `audit_log`：前后版本 JSON、操作人、角色、时间、事由，可通过 `GET /audit` 查询。

## 核心规则

**授权审批**（`POST /applications/{id}/approve`）逐项校验：合同须处于「生效」；授权窗口未过期且在合同期限内；与既有有效授权无互斥冲突——同一权利类型、地域与媒介相交、时间窗口相交，且任一侧为排他即冲突（`"*"` 为全域通配）。合同终止会自动截断其全部授权，此后该范围可被他人申请。

**结算**（`POST /settlements/{period}/run` → `/confirm`）：金额一律整数分，费率与分成比例为万分比。每期分成 = 净销售额（销售 − 退款）× 费率，再按各方万分比以**最大余数法**分摊，保证各方之和严格等于总额。运行可重复（覆盖未确认结果）；确认后账本锁定。争议冻结中的合同本期跳过，明细留待解冻后的结算期吸收。

**迟到与退款**：销售明细按 `(source, line_ref)` 幂等去重，重复导入逐行跳过、不产生任何变化。明细所属结算期已确认时自动改道当前开放期（`routed_period`），已确认账本纹丝不动；计算依据中以 `late_line_ids` 标注迟到明细。退款以负数「退款冲销」行导入，与销售同口径轧差。

**追溯生效**：比例修订 / 补充协议可指定早于今日的 `effective_from`。新规则按「期末有效」适用于结算期；受影响的已确认期间会生成差额调整条目（基于已确认金额 + 在途调整计算，链式修订收敛），进入当前开放结算期，原始期间账本不被改写。补充协议提供新授权范围时，旧范围自生效日起截断。

**版图还原**：`GET /landscape?date=YYYY-MM-DD[&known_at=...]` 还原指定日期有效的授权版图（合同状态、授权范围、分成规则）。授权与状态均为双时间轴（业务生效日 + 系统记录时刻），追加 `known_at` 可回答「以当时已知的信息，那天是什么样」。

**计算依据**：`GET /settlements/{period}/explain` 给出每一笔分成的完整依据——来源销售行、毛额/退款/净额、费率、分成总额、各方比例与舍入方式；调整行附原始期间、新旧规则与差额推导。

## API 一览

```
GET    /health
POST   /parties                     GET  /parties
POST   /assets                      GET  /assets
POST   /contracts                   GET  /contracts            GET /contracts/{id}
POST   /contracts/{id}/transition        {to, event_date?, reason?}
POST   /contracts/{id}/amendments        {effective_from, grants?, share_rule?, reason?}
POST   /contracts/{id}/share-revisions   {effective_from, royalty_rate_bp, splits, reason?}
POST   /contracts/{id}/milestones   GET  /contracts/{id}/milestones
POST   /milestones/{id}/deliver          /milestones/{id}/accept
POST   /applications                GET  /applications         GET /applications/{id}
POST   /applications/{id}/approve        /applications/{id}/reject
POST   /sales/import                GET  /sales?contract_id=&period=
POST   /settlements/{period}/run         /settlements/{period}/confirm
GET    /settlements                 GET  /settlements/{period} GET /settlements/{period}/explain
GET    /landscape?date=&known_at=
GET    /reports/annual?year=
GET    /audit?entity_type=&entity_id=
```

错误统一为 `{"error": {"message", "details"}}`：400 参数/规则、401 未认证、403 越权、404 不存在、409 状态冲突或互斥授权。

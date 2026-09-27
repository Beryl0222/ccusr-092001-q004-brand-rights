# 赛事品牌全年权益

赛事公司与法务、商业团队共用的权益台账后台：把素材权属、地域与媒介限制、
授权申请、分阶段交付、销售回报、退款冲销与分成调整串成一条长期台账。
`domain.json` 给出权利类型、合同版本与账务调整分类，`rights/` 是领域核心。

## 运行

```bash
python3 service.py --check        # 校验 domain.json 词汇与模型一致
python3 -m unittest -v            # 全部业务规则与接口测试
python3 service.py --port 8000    # 启动后台，/health 为巡检入口
```

接口（除 `/health`）要求请求头 `X-Role: legal|commercial|admin`，
写操作另需 `X-Actor`（ASCII 标识，记入审计）。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST/GET | `/assets` | 素材登记（权属、地域、媒介限制） |
| POST/GET | `/contracts`、`/contracts/{id}` | 合同登记与查询；敏感合同仅 legal/admin 可见 |
| POST | `/contracts/{id}/status` | 状态流转：草拟→会签→生效→（争议冻结↔生效）→终止 |
| POST | `/contracts/{id}/amendments` | 补充协议/比例修订，可追溯生效，留前后值与批准人 |
| POST/GET | `/licenses`、`/licenses/{id}` | 授权申请；批准时拦截过期与互斥（独家冲突）授权 |
| POST | `/licenses/{id}/approve|reject|revoke` | 审批与撤销，状态按生效日记录 |
| POST | `/licenses/{id}/milestones/{seq}/deliver` | 分阶段交付登记 |
| POST | `/sales/import` | 销售明细批量导入，`batch_id:line_id` 幂等 |
| POST | `/refunds` | 退款冲销，`refund_id` 幂等，累计不超过原销售额 |
| POST | `/settlements/run`、`/settlements/{period}/confirm` | 生成草稿、确认关账 |
| GET | `/settlements/{period}` | 每一笔分成的计算依据（销售额、比例版本、舍入） |
| GET | `/landscape?date=YYYY-MM-DD` | 还原指定日期当时有效的授权版图 |
| GET | `/reports/revenue?year=YYYY` | 全年衍生收入汇总（按已确认账本） |
| GET | `/audit` | 审计流水（仅 legal/admin） |

## 关键设计

- **金额与比例**：金额一律为整数分；分成比例为基点（万分之一），合计必须
  为 10000。多方分成用最大余数法舍入，任一结算单元各方之和精确等于净额，
  并列时按责任方名称排序保证结果确定。
- **条款版本化**：补充协议/比例修订追加合同条款版本而不改写旧版本。
  分成比例按结算月生效，权利范围按日生效；金额与范围的每次变更都留下
  前后值、操作人与批准人（`/audit`）。
- **审批防护**：授权批准时校验合同处于「生效」、授权期间未超出合同有效期
  （防过期授权）、范围未超出现行条款与素材权属限制；与已批准授权在期间/
  地域/媒介上重叠且任一方为独家时判定互斥，拒绝批准。
- **账本不可变**：结算期确认（关账）后账本冻结，不可重跑。迟到销售、
  退款冲销、追溯修订一律在后续结算期以调整单元入账：迟到销售标记
  `late`，退款按原销售期间以负数入账，追溯修订按新旧比例重算已确认
  单元的差额（`追溯调整`），已确认账本本身不被改写。
- **幂等**：销售以 `batch_id:line_id`、退款以 `refund_id` 去重，重复导入
  不会改变已确认账本；草稿在确认前可安全重算，确认时若草稿之后又有
  补充协议则要求重新 run。
- **争议冻结**：冻结期间合同的销售与追溯调整暂缓结算，解冻后随下一
  结算期入账。
- **时间还原**：合同与授权的状态按生效日记录（同刻后写胜出），
  `/landscape?date=` 还原当日有效的授权、范围与交付进度。

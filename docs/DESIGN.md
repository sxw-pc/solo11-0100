# 仓门扫描对账台设计

预约单以箱号为主键，声明订单、托盘、数量、月台、窗口、净重及容差。输入扫描流为 JSONL，每条有稳定事件身份、UTC 时间、箱号、动作和来源字段。文件哈希冻结来源；事件身份重复且内容不同进入冲突记录。

数据流：预约配置 → 来源文件 → 事件规范化 → 事件时间排序 → 装卸状态机与拆并箱谱系 → 数量、称重、月台核对 → 差异结算 → JSON/Markdown 交接报告。

合法装卸顺序是 `gate → unload → check → exit`。称重与拆并箱事件不推进此状态。错误码 `illegal_transition` 阻断交接；缺少阶段为 `missing_stage`。拆箱 `children` 与并箱 `parents` 的数量之和须等于事件声明数量，只有守恒的关系写入 `lineage`。数量差异区分 `shortage` 与 `overage`，缺重为 `missing_weight`，越容差为 `weight_mismatch`。月台或时间窗口不符是 `wrong_dock_or_window`。未知箱号保留 `unknown_box` 问题。

决策为 `accepted`、`pending` 或 `blocked`。阻断问题不会因其他正常扫描被覆盖；缺少证据不会得到 accepted。每次重算只更新决策、业务问题与谱系，来源和原始事件不可变。配置文件哈希冻结在数据库；修改预约单须使用新工作区。当前只处理单批次本地扫描，不与仓储系统或承运人联网。

## 逆向退货处置（版本化、可重放）

`returns.register` 把一票已 `accepted` 的来源箱、其拆并箱谱系、RMA、退包与检查原文整体冻结进 `return_case`（含 fixture 的 SHA-256）；来源箱之后若转为 `pending`/`blocked`，重放时读取实时 `decision` 而不是信任冻结快照。

逐序列号的资格判断只依据可核证证据，不信任退包文字：

- 序列号必须能沿 `event` 中真实 `split`/`merge` 的 `units` 追到唯一子箱；无归属是 `lineage_unit_untraced`，被多个子箱重复归属是 `duplicate_lineage_attribution`，退包声明子箱与谱系不符是 `wrong_child_box`。
- 序列号必须出现在恰好一张授权 RMA：无授权 `unit_not_in_authorized_rma`，多张 RMA 重复认领 `unit_claimed_by_multiple_rmas`；退包晚于该 RMA 承诺期限（边界含等号）是 `rma_deadline_passed`。
- 同号重复扫描（含跨退包）只保留首扫为实物证据，重复计 `duplicate_scan`，不产生第二份库存或退款。
- `sealed` 且 `seal=intact` 可直接入库；封口破损按 opened 处理；opened 只有检查给出正向授权修复（正的 `repair_cost`）且在真实工位完成才可再售，否则 `opened_requires_authorized_repair` 隔离；封口与修复字段自相矛盾是 `inspection_conflict`。
- 硬隔离原因全部进 `return_issue` 并写在 `unit_trace.reasons`，实物状态为 `quarantined`，既不入库也不退款。

RMA 是成组退回：组内任一单位未退回（`rma_unit_not_returned`）或被隔离，整张 RMA 不构成完整履约，部分已修不等于完整退款。修复资源是冻结数量的真实工位（`repair_station`，每个 `repair_order` 占用一个工位并带证据路径），两种正当处置合同：

1. `oldest_complete_rma_first`：按 `waiting_days` 从久到近，工位够就完成整张 RMA。
2. `maximum_complete_customers`：在工位约束下最大化可完整履约的不同客户数，确定性平局规则（客户数→RMA 数→少用工位→RMA 编号）。

工位不足的 opened 单位留 `awaiting_repair` 并记 `repair_capacity_exceeded`，不得再售。

每次 `returns.process` 产生一个不可变 `return_run`，输入摘要（case 摘要、全部 amendment 哈希、实时 decision/lineage/拆箱事件、规则版本）相同且合同相同的重放直接复用，不写新行。库存义务落在 `inventory_ledger`（release/quarantine/hold_repair），财务义务落在 `refund_ledger`（refund/reversal，含 `settled_after` 与 `prior_id` 链）。新增退包或检查纠错经 `returns.add_amendment` 冻结后重放：已出具退款绝不覆盖，只追加补差或冲回，未受影响的 RMA 不产生新台账行；版本链按合同分别延续，不同合同互不串链。报告导出全部逆向表、逐版本摘要和每笔证据定位（`return_case:`/`amendment:`/`lineage:`/`event:`/`decision:`/`return_run:STATION:`）。

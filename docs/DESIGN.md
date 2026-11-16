# 仓门扫描对账台设计

预约单以箱号为主键，声明订单、托盘、数量、月台、窗口、净重及容差。输入扫描流为 JSONL，每条有稳定事件身份、UTC 时间、箱号、动作和来源字段。文件哈希冻结来源；事件身份重复且内容不同进入冲突记录。

数据流：预约配置 → 来源文件 → 事件规范化 → 事件时间排序 → 装卸状态机与拆并箱谱系 → 数量、称重、月台核对 → 差异结算 → JSON/Markdown 交接报告。

合法装卸顺序是 `gate → unload → check → exit`。称重与拆并箱事件不推进此状态。错误码 `illegal_transition` 阻断交接；缺少阶段为 `missing_stage`。拆箱 `children` 与并箱 `parents` 的数量之和须等于事件声明数量，只有守恒的关系写入 `lineage`。数量差异区分 `shortage` 与 `overage`，缺重为 `missing_weight`，越容差为 `weight_mismatch`。月台或时间窗口不符是 `wrong_dock_or_window`。未知箱号保留 `unknown_box` 问题。

决策为 `accepted`、`pending` 或 `blocked`。阻断问题不会因其他正常扫描被覆盖；缺少证据不会得到 accepted。每次重算只更新决策、业务问题与谱系，来源和原始事件不可变。配置文件哈希冻结在数据库；修改预约单须使用新工作区。当前只处理单批次本地扫描，不与仓储系统或承运人联网。

## 计费重量结算

拆箱后再次并箱的任意深度谱系按谱系图（非普通树递归）结算承运计费重量。每个最终叶箱保留一行账单：实测毛重、沿链父箱皮重按件数比例分摊到本叶的估算分摊额、可结算净重（毛重减分摊皮重）、舍入后计费重量，以及原始称重事件与来源行号，账单行可反查叶箱、父箱皮重和来源行。

毛重在每个 split/merge 节点必须守恒：拆箱子箱毛重之和等于父箱毛重，并箱结果毛重等于各父箱毛重之和；未单独称重的中转箱按其直接拆箱子箱解析。叶箱可结算净重之和必须等于根箱毛重减去链上全部皮重。缺重（`weight_not_conserved`）、负净重（`negative_net_weight`）、循环谱系（`lineage_cycle`）、同一箱进入两个谱系事件或同一叶箱被重复引用（`duplicate_lineage_reference`）、重复称重（`duplicate_weighing`）、称重引用的设备校准版本缺失或在称重时刻已失效（`calibration_expired`）都会阻断本订单账单，且不影响无关订单结算。

合同允许两种同效舍入口径，必须由冻结的订单配置显式选择 `rounding_mode`，缺失或无法识别时按 `missing_rounding_config` 阻断：`per_leaf` 为逐叶箱按 `billing_unit_kg` 向上舍入后相加；`shipment` 为先汇总整票可结算净重再舍入一次。同一三层 split/merge fixture（`fixtures/billing_*`）在两种口径下产生不同的应付计费重量与承运商账单。结算结果持久化在 `settlement`（票头）与 `settlement_line`（叶箱行）表，随每次重算重建。

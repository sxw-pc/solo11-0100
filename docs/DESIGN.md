# 仓门扫描对账台设计

预约单以箱号为主键，声明订单、托盘、数量、月台、窗口、净重及容差。输入扫描流为 JSONL，每条有稳定事件身份、UTC 时间、箱号、动作和来源字段。文件哈希冻结来源；事件身份重复且内容不同进入冲突记录。

数据流：预约配置 → 来源文件 → 事件规范化 → 事件时间排序 → 装卸状态机与拆并箱谱系 → 数量、称重、月台核对 → 差异结算 → JSON/Markdown 交接报告。

合法装卸顺序是 `gate → unload → check → exit`。称重与拆并箱事件不推进此状态。错误码 `illegal_transition` 阻断交接；缺少阶段为 `missing_stage`。拆箱 `children` 与并箱 `parents` 的数量之和须等于事件声明数量，只有守恒的关系写入 `lineage`。数量差异区分 `shortage` 与 `overage`，缺重为 `missing_weight`，越容差为 `weight_mismatch`。月台或时间窗口不符是 `wrong_dock_or_window`。未知箱号保留 `unknown_box` 问题。

决策为 `accepted`、`pending` 或 `blocked`。阻断问题不会因其他正常扫描被覆盖；缺少证据不会得到 accepted。每次重算只更新决策、业务问题与谱系，来源和原始事件不可变。配置文件哈希冻结在数据库；修改预约单须使用新工作区。当前只处理单批次本地扫描，不与仓储系统或承运人联网。

## 待领托盘放行

寄存托盘的状态链为 `unclaimed_case`（冻结 fixture 与 ingest 身份）→ `unclaimed_custody`（保管链）→ `unclaimed_authorization`（绑定版本的核验授权）→ `pallet_custody`（物理持有人与状态）→ `unclaimed_release`（唯一一次实体交付）。每个案件在 `unclaimed_release.case_id` 上唯一，重复交货会触发约束或 `authorization_already_consumed`。

保管链只接受两种扩展：双签的 `handoff` 与现任保管人的 `seal_check`。任何运行时步骤都递增 `pallet_custody.version`，授权记录写入时的版本与保管链指纹；`deliver` 在同一即时事务内比对版本、指纹、封条、现行保管人与授权状态，全部一致才原子消费。这样核验后换班、事务中断或并发双领取都不会产生第二次交付。

领取合同互斥：记名合同核对 `registered_consignor` 与 `manifest_pallet`；不记名合同核对提交摘要与寄存摘要，且同一摘要不能跨案复用。报告只落摘要与核对结果。`decision=accepted` 是卸货结论，与领取授权无关。

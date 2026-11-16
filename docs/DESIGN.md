# 仓门扫描对账台设计

预约单以箱号为主键，声明订单、托盘、数量、月台、窗口、净重及容差。输入扫描流为 JSONL，每条有稳定事件身份、UTC 时间、箱号、动作和来源字段。文件哈希冻结来源；事件身份重复且内容不同进入冲突记录。

数据流：预约配置 → 来源文件 → 事件规范化 → 事件时间排序 → 装卸状态机与拆并箱谱系 → 数量、称重、月台核对 → 差异结算 → JSON/Markdown 交接报告。

合法装卸顺序是 `gate → unload → check → exit`。称重与拆并箱事件不推进此状态。错误码 `illegal_transition` 阻断交接；缺少阶段为 `missing_stage`。拆箱 `children` 与并箱 `parents` 的数量之和须等于事件声明数量，只有守恒的关系写入 `lineage`。数量差异区分 `shortage` 与 `overage`，缺重为 `missing_weight`，越容差为 `weight_mismatch`。月台或时间窗口不符是 `wrong_dock_or_window`。未知箱号保留 `unknown_box` 问题。

决策为 `accepted`、`pending` 或 `blocked`。阻断问题不会因其他正常扫描被覆盖；缺少证据不会得到 accepted。每次重算只更新决策、业务问题与谱系，来源和原始事件不可变。配置文件哈希冻结在数据库；修改预约单须使用新工作区。当前只处理单批次本地扫描，不与仓储系统或承运人联网。

## 危险类别与隔离舱契约（规则版本 2）

预约配置新增可审计契约：`config_version`、`safety_policy`、`hazard_classes`（每类声明 `incompatible_with`）、`segregation_bays`（月台、认证区间 `certified_from`/`certified_until`、获准类别 `classes`），每箱声明 `hazard_class`，可选 `bay`。冻结配置时校验契约自洽：策略必须是 `dock_window_exclusive` 或 `certified_bays_parallel`，互斥引用与舱位获准类别必须已定义，认证区间不得倒置。契约随配置哈希一并冻结；`decision` 记录配置版本，重算只读冻结配置，配置变化须新建工作区，已完成批次的判定不被改写。

类别沿拆并谱系继承：拆箱子箱继承父箱类别；并箱保留全部来源类别，不得只按合并后箱号判定；传播经不动点沿中继链（拆后再拆、并后再并）传导，类别与来源写入 `class_origin` 供审计。并箱混合不兼容类别时，每个来源货物须声明获准隔离舱：舱位已定义、月台一致、事件时刻落在认证区间内（含端点）、类别获准、不兼容来源不同舱，否则记 `unsegregated_merge` 或 `bay_not_certified`。

同月台预约窗口重叠且类别不兼容时按本批 `safety_policy` 判定同时作业范围：`dock_window_exclusive` 整个月台窗口互斥，两张单据都记 `segregation_conflict` 并阻断；`certified_bays_parallel` 仅当双方箱单声明的隔离舱不同、认证完整覆盖重叠窗口且获准各自全部类别时允许并行放行，决策中记录 `parallel_cleared` 证据链。未知类别（`unknown_hazard_class`）、缺少类别（`missing_hazard_class`）、并箱来源无类别（`missing_class_origin`）一律阻断，不误报安全。报告列出冲突类别、重叠窗口、隔离舱配置版本与影响到的源箱/子箱。

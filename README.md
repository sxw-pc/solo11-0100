# 仓门扫描对账台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m dockscan demo` 自动初始化临时工作区、导入 fixture、计算决策、核验待领托盘并按记名发货方合同完成一次柜台放行，最后打印报告路径。`python -m dockscan --help` 查看手动导入、复核、核验、授权、放行及报告命令。`python -m pytest -q` 运行自动化测试。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写；同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

## 待领托盘：核验与柜台放行

`unclaimed-register` 冻结案件时把 fixture、`ingest` 中该托盘的原始事件与箱单身份、保管链和托盘状态一起写入。随后按“核验 → 一次性授权 → 原子放行”推进：

- 保管链必须连续：首个 `store` 步骤由接收人签名，`handoff` 必须由交出的现任持有人与接收人双签，`seal_check` 由现任保管人复核；每一步都须指向同一托盘与同一原封条。缺签名、保管人不明、封条更换或时间倒序即冻结实物。
- 每个案件只能选一种排他合同：`registered_consignor_release` 核对登记发货方与 `manifest_pallet`；`bearer_receipt_release` 核对领取者提交的实体凭证摘要与寄存摘要。两种合同同时声明且未显式选定时为 `contract_ambiguous`，柜台拒绝交货。
- `unclaimed-verify` / `verify_claimants` 只核对、不移动托盘；`unclaimed-authorize` 生成绑定当前托盘版本与保管链指纹的授权；`unclaimed-deliver` 在同一 `BEGIN IMMEDIATE` 事务内消费授权与托盘状态，成功后托盘 `released`、持有人变为领取人，并记录交付人、领取人、合同、凭据摘要、封条与来源事件。
- 授权不是交货：核验后换班（`unclaimed-handoff`）会递增版本并使旧授权失效；失败或中断的事务回滚后授权仍可安全重试；成功后不可再次放行。
- 报告只保留凭据摘要与核对结果，不输出可复用凭据原文；`unclaimed` 段落直接读取放行表，因此实际领取人与交接证据只能来自真实交付。

原收货 `decision=accepted` 只是卸货结论，不构成领取授权。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

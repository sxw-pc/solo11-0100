# 仓门扫描对账台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m dockscan demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m dockscan --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 逆向退货处置

`python -m dockscan returns-demo` 用 `fixtures/return-*.json` 完整演示：冻结 accepted 来源箱与拆箱谱系 → `return-register` 冻结 RMA/退包/检查原文 → 两种处置合同分别 `return-process` 重放 → 报告导出可再售序列号、隔离原因、逐 RMA 退款义务和证据路径。生产步骤对应子命令 `return-register`、`return-amend`（冻结新增退包或检查纠错）、`return-process --contract oldest_complete_rma_first|maximum_complete_customers`、`report`。

逐序列号沿原始收货事件和拆箱子箱追权；同号重复扫描只计一次；退包子箱不符、序列号不在授权 RMA、谱系重复归属、超期或来源单转 pending/blocked 一律隔离并留核证原因。opened 件占用真实修复工位且获授权修复后才可再售；成组 RMA 全部合格才退款。新退包或纠错只追加补差/冲回，从不覆盖已出具退款，未受影响 RMA 不产生新台账行。逆向表见 `return_run`、`unit_trace`、`repair_station`、`repair_order`、`inventory_ledger`、`refund_ledger`、`return_issue`、`return_amendment`。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

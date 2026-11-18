# 仓门扫描对账台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m dockscan demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m dockscan privacy-demo` 运行保密费率发布的端到端场景（含允许/拒绝决定、受众隔离与恢复）。`python -m dockscan --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 保密发布与不可推断审查

含 `freight_rate_cents` 的称重行是保密数据。`register-case` 把逐箱原始费率行、来源哈希以及此前已对外发布的聚合（`prior_releases`）冻结为 `privacy_case`。`release` 命令按事前授权合同生成可审计发布物：

- `internal_auditor_exact_access`（受众 `internal_auditor`）：允许逐箱费率与完整来源路径，但报告绑定来源版本/哈希并写访问记录。
- `carrier_consortium_non_inference`（受众 `carrier_consortium`）：只允许聚合，且该受众历史可见的全部方程联立后，不得唯一确定任何单箱费率。

审查使用精确有理数行空间判据（RREF 中出现单位行等价于某箱变量被唯一解出），覆盖两组相减、多组联立、完全相同组重报等情形，而非检查组内人数或删字段。拒绝记录只含箱号与方程成员，不含任何被保护数值。发布身份由来源版本、来源哈希、受众、合同、成员集合与金额的规范化哈希确定，不可覆写。审查→占用身份（`proposed`）→原子落盘→翻 `committed` 由跨线程/跨进程发布锁与 SQLite `BEGIN IMMEDIATE` 串行化；`recover` 依据账本冻结的字节修复或中止在途发布，杜绝“账本称已发布但文件缺失”或“文件已外发但账本未发布”的状态。来源被修订时，事件/来源哈希校验失败，所有审查以 `source_state_conflict` 失败关闭。普通对外报告（`report`）自动从事件载荷中移除保密字段。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

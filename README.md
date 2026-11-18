# 仓门扫描对账台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m dockscan demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m dockscan --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 仓门暂存区温控（有限班次冷却优化）

`python -m dockscan climate-demo` 冻结 D1/D2 仓门事件与本地传感器，针对两份授权目标发布全局精确最优冷却计划并输出 JSON/Markdown 报告。手动流程：

- `climate-register --db DB --case-id ID --fixture case.json`：冻结仓门事件、传感器原始字节/SHA 与合同输入。
- `climate-plan --db DB --case-id ID --objective {min_energy,min_max_dock_exposure}`：校验并发布设备命令、逐时段温度/暴露、电费与最优性证据；校验失败或无可行计划时整份待核，不发布部分命令。
- `climate-feedback --db DB --case-id ID --objective O --dock D --at 边界时间戳 --temp-c T`：登记迟到真实读数；已执行命令不可变，只重算剩余时段并产生新版本，已发生超限报告为不可追回。
- `climate-show` 查看最新计划版本（含偏差、来源摘要、DP 枚举证据），`report` 命令的输出同时包含收货决策与温控命令/风险。

温度动力学为 `终点 = 起点 + 环境增温 − 实际冷却量`；暴露为逐时段终点超上限正温差之和（缺测不当零）；共享冷却机每时段最多冷却一个仓门。动态规划枚举全部可行序列并按目标字典序求精确最优，贪心逐时段决策不被采用。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

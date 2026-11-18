# 仓门扫描对账台

纯本地证据采集与业务决策工具。输入为虚构 fixture，运行中不访问网络。Python 3.11+ 标准库为运行依赖，pytest 为测试依赖。

## 快速开始

`python -m pip install . --no-build-isolation --no-deps` 在本地安装已预装构建工具的环境中构建此项目；运行期无第三方依赖。

`python -m dockscan demo` 自动初始化临时工作区、导入 fixture、计算决策并打印报告路径。`python -m dockscan --help` 查看手动导入、复核及报告命令。`python -m pytest -q` 运行自动化测试。

## 原件归档

`python -m dockscan archive --db <db> --blocks <dir> --representation record_chunks`
把 `source.raw` 迁移为内容寻址块仓 + 有序 manifest，逐字节验证后切换读取指针并回收
BLOB；`fixed_64` 与 `record_chunks` 两种无损表示、GC 的引用证明、在线并发与故障恢复
见 `docs/ARCHIVE.md`。

## 数据流和约束

配置文件的 SHA-256 冻结到 `meta`；`source` 存每个输入文件的哈希；`event` 存逐行规范化记录和事件身份；`issue` 保存解析或业务问题；`decision` 保存规则版本、业务结论和引用的事件 ID。导入采用单个 SQLite 事务；同文件哈希重入不重复写，同事件身份但不同内容进入冲突队列。业务规则读取事件时间而非文件顺序；未知对象或缺证据不能自动视为正常。决策重算保持源事件不可变。

错误码见 `issue.code`，报告同时输出 JSON 和 Markdown。`fixtures` 中是确定性的全虚构数据。首版只支持单机、单批；配置一旦冻结，变化需新建工作区。细分业务边界与未来方向由项目生成提示词约束，首版没有预置未来功能。

来源原件默认仍以 `source.raw` BLOB 保存，归档迁移是显式操作；迁移不改变 `source.id`、
路径、`sha256` 或 `event/evidence` 引用，报告中的来源摘要保持一致。

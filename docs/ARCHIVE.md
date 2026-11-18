# 原始来源文件的内容寻址归档

旧版把每份输入文件的完整字节存进 `source.raw` BLOB，`source.sha256` 是整文件
哈希。两份文件只要整体哈希不同就各占一份完整 BLOB——即使 B 只是比 A 多一条记录。
本模块把原始字节改为**内容寻址块仓 + 每来源有序 manifest**，在不破坏来源身份、
事件外键与报告证据的前提下消除重复存储。

## 不可改写块仓

```
<store_root>/blocks/<sha256[:2]>/<sha256>   只含原始字节块
<store_root>/tmp/<sha>.<pid>.<uuid>.part    写入暂存（崩溃后由 GC 清理）
```

* 块文件名就是内容 SHA-256；写入先落 `tmp/`、`fsync`，再以硬链接（或原子
  `rename`）就位。任何时刻都不会出现被截断的内容寻址块。
* 块不可改写、不原地修改；同一内容只存一次，物理去重由文件系统保证。
* `source` 新增 `length`、`version`、`created_generation`、`storage` 列；
  旧库在 `connect()` 时原地重建该表（保留行 id），因此 `event.source_id`、
  `evidence`、`decision` 引用与报告中的 `source.id` 完全不变。

## 两种无损表示

`fixtures/archive-case.json` 冻结了 `fixed_64` 与 `record_chunks`。
两种表示对同一份 A/B 都恢复**各自不同的原始字节和 SHA**。

| | `fixed_64` | `record_chunks` |
|---|---|---|
| 切分 | 从偏移 0 起每 64 字节 | 每条原始 JSONL 记录 + 实际分隔字节（`\n` 或 `\r\n`） |
| BOM / 末尾无换行 | 全部在块内，无 side-channel | `prefix`（BOM）与 `suffix`（末尾未终止记录）单独保存 |
| A/B 共享块 | ≤ 2（开头插入 1 条记录令后续边界整体错位） | 25（B 多出的首条记录独占 1 块） |
| 唯一块数（A+B） | 84 | 26 |
| 块仓字节（A+B） | 5288 | 2761 |

manifest 是 SQLite 中的不可变行（`source_manifest` + `manifest_block`）；读取时
只按 manifest 顺序拼接原始块与 prefix/suffix，**从不反序列化 JSONL 再输出**。
因此 CRLF、BOM、非 ASCII 字节、重复的同内容行、无末尾换行都逐字节保留，并重得
旧 `source.sha256`。任何块长度或哈希不符都会以 `block_corrupt` 失败，而不是给出
“看起来一样”的文本。

## 世代与在线迁移

* `archive_generation` 记录 `planned → published → retired`（或 `abandoned`）。
* `archive_state.archive_generation` 是唯一读取指针；发布使用比较交换：
  只有全部 manifest 就绪且每个来源都能逐字节复原（长度 + SHA + 与旧 BLOB 对比）
  才切换，`expected_pointer` 不匹配则 `generation_pointer_conflict`，重规划重试。
* 读者 `open_reader()` 在第一次读取前把世代 id 写入 `archive_reader` 并在整个
  读取期间钉住它；切换指针不影响已打开的读者。`read_source()` 自动短暂钉住。
* 迁移期间旧 BLOB 与新 manifest 并存；`verify_generation()` 通过后才
  `release_blobs()` 清空已发布世代的 `source.raw`（可 `VACUUM` 归还磁盘）。
* 新导入（`ingest.import_file`）在归档已启用时立即为新来源建块与 manifest，
  以一个新世代承载旧 manifest 引用并 CAS 发布；旧世代对钉住读者仍然可用。
* 第二表示可以在 BLOB 已清空后直接从已发布 manifest 复原原始字节再切块。

## 垃圾回收

`gc()` 在**持有写锁的同一事务**内读取受保护世代集合，然后删除不在集合内的块：

* 受保护 = `published` / `planned` 世代，**以及所有未关闭读者钉住的世代**（包括
  已 retired 的世代）；
* 保护判定走 `generation_manifest`，因此新世代按引用沿用旧 manifest 时，那些块
  仍被视为可达；
* 没有“等几秒”的经验性宽限：读者行就是引用证明。进程崩溃遗留的读者行由
  `close_dead_readers()` 通过 PID 存活检查显式关闭（可选 `grace_seconds` 只是
  在引用证明之外额外保护陈旧块，不能替代引用）。

磁盘数字来自真实文件：块文件大小、SQLite 主文件 / WAL、以及尚未归还的空闲页
（`db_free_bytes`）。`blob_bytes` 是数据库中逻辑 BLOB 载荷，**不重复计入**
`physical_bytes`；`logical_block_refs` 只是引用数，绝不当作已节省磁盘。

## 故障点与恢复

`migrate(..., hook=...)` 会在以下位置抛错注入故障，重启后均幂等继续：

| 故障点 | 状态 | 重启行为 |
|---|---|---|
| `block_writing` | 只写了半块到 `tmp/*.part` | 无内容寻址块产生；重写并清掉 `.part` |
| `block_staged` | 完整临时块尚未就位 | 重写；`tmp` 由 GC 清理 |
| `manifest_written` / `manifests_written` | 块已全写、manifest 未发布 | 事务回滚，重新构建 manifest |
| `published` | 指针已切换、BLOB 未清 | 校验后继续清理 BLOB，两者并存 |
| `blobs_cleared` | BLOB 已清、进程中断 | 从 manifest 校验/读取并继续 |
| `blobs_released` | 收尾统计前中断 | 指针与 manifest 完整，重复执行即幂等 |

恢复前置条件始终成立：要么旧 BLOB 完整，要么存在已发布且可校验的世代；恢复流程
要么复用已存在的 manifest，要么新建世代重建全部块（已丢弃的陈旧 manifest 不会被
误发布）。

## CLI

```
python -m dockscan archive --db ledger.db --blocks /path/blocks --representation record_chunks
python -m dockscan verify     --db ledger.db --blocks /path/blocks [--generation N]
python -m dockscan source     --db ledger.db --blocks /path/blocks --source-id 1 --out a.jsonl
python -m dockscan storage    --db ledger.db --blocks /path/blocks
python -m dockscan gc         --db ledger.db --blocks /path/blocks [--dry-run]
python -m dockscan register   --db ledger.db --case archive-01 fixtures/archive-case.json
python -m dockscan case-check --db ledger.db --case archive-01 --blocks /path/blocks
```

`report` 的 `source` 投影仍只有 `id/path/sha256`；归档存在时额外附带
`archive.generation`、逐源复原结果、真实物理占用与最近一次 BLOB 回收前后数字。
`archive_case` 的冻结记录不受迁移影响，`case-check` 用它们逐源证明字节、路径、
长度与 SHA 仍一致。

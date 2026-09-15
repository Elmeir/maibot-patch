# maibot-patch

[MaiBot](https://github.com/MaiM-with-u/MaiBot) 一键补丁工具——自包含单文件 Python 脚本，无需安装任何依赖。

针对 MaiBot **1.2.4 / 1.2.5** 源码核查后整理的修复集，附带配套的上游 issue 整理文档 [`maibot-issues.md`](maibot-issues.md)（对齐官方 issue 模板字段，可逐字段复制提交）。

## 补丁组

| 组名 | 问题 | 修复 |
|---|---|---|
| `2010` | A_memorix 向量通道降级时 `vector_store/graph_store.save()` 抛 NoneType、写回游标冻结、摘要洪水 | `save()` 判空 + 构造适配器时清空类级缓存（热重建后自检发真实请求，免重启自愈） |
| `tools` | `query_person_profile` / `query_memory` 工具描述职责不清，planner 乱用 | 工具描述分工：画像按名字查人无需日期；记忆 time/hybrid 必须给时间范围 |
| `order` | Maisaka 内存聊天历史乱序——bot 回复"越过"生成期间到达的消息（LLM 上下文因果错位） | 新增 `insert_chat_history_message_sorted()` 按 timestamp 二分有序插入，覆盖全部 3 处入历史路径 |
| `trigger` | 回复后紧邻消息的触发被静默丢弃（"A→planner→B→reply→回复A→B 丢失"） | `turn_scheduler.py` 两个触发门的 wait 决策统一安排延迟重查 `_defer_message_turn_check`（间隔优先平均外部消息间隔，趋于触发收敛） |

## 用法

```bash
python maibot_patch_all.py --detect                 # 定位 MaiBot 目录并打印
python maibot_patch_all.py                          # 默认 dry-run，预览全部 diff
python maibot_patch_all.py --apply                  # 一键打全部补丁
python maibot_patch_all.py --apply --only trigger   # 只打指定组（2010 / tools / order / trigger）
python maibot_patch_all.py --revert                 # 从最近备份一键还原
```

目录定位顺序：`--repo` 参数 > 环境变量 `MAIBOT_ROOT` > 常见安装路径 > 当前目录及父级 > 盘符浅扫。

## 安全机制

- **默认 dry-run**：只打印 diff，加 `--apply` 才写文件
- **写前自动备份**：`.bak-<时间戳>`
- **写后语法自检**：`py_compile` 报错自动回滚该文件
- **幂等**：已打过的补丁自动跳过，可重复执行
- **可还原**：`--revert` 从最近备份还原所有文件

## 适用版本

MaiBot 1.2.4 / 1.2.5（按 tag 逐一验证锚点行）。其他版本请先 dry-run 检查 diff 是否匹配。

## 相关仓库

- 上游：[MaiM-with-u/MaiBot](https://github.com/MaiM-with-u/MaiBot)
- 配套插件：`maibot-plugin-gif-storyboard`、`maibot-plugin-knowledge-base`

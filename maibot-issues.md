# MaiBot 上游 Issue 整理（对齐官方模板字段）

> 来源：`maibot-plugin-gif-storyboard` / `maibot-plugin-knowledge-base` 插件实测 + `maibot_patch_all.py` 补丁源码核查
> 核查版本：**MaiBot 1.2.4 / 1.2.5**（1.2.5 已按 tag 逐一验证）｜整理日期：2026-09-15
> 排序：严重程度降序。每节标注建议使用的官方模板（**Bug** = `bug_report.yml`，**Feature** = `feature_request.yml`），字段名与模板一致，可逐字段复制提交。
> 提交时需自行填写模板必填项：勾选检查项、分支（main/dev）、系统环境、Python 版本。

---

## Issue 1（高）｜模板：Bug｜标题：回复后紧邻消息的触发被静默丢弃——新消息不进 Planner 也不进历史

**组件**：`src/maisaka/turn_scheduler.py` ｜ **已验证补丁**：`maibot_patch_all.py` 组 `[trigger]`

### 遇到的问题

A 发言触发 planner → planner 处理期间 B 发言 → planner 调 reply → 回复 A 发出 → **B 的触发被丢弃**：B 不触发新 Planner 轮次、无延迟重查，滞留 `message_cache`。群里没人再说话则 **B 永远不被 Planner 看到**（不进 Planner 也不进历史）。@ / 提及消息不受影响（强制触发）。

### 报错信息

无异常栈。日志特征：调度判定出现 `判定=等待更多消息`（reply_necessity）或 `判定=等待更多消息`（frequency wait）后，该消息无任何后续处理记录。

### 如何重现此问题？

1. 群聊触发 planner（A 发言，可含被回复的语境）；
2. planner 处理期间（或 replyer 生成回复期间）B 发一条普通闲聊消息（非 @ / 非提及）；
3. 等待回复 A 发出；
4. 观察：B 未触发新 Planner 轮次；回复频率调度日志中 B 的那次判定为"等待更多消息"后无下文。

### 可能造成问题的原因

`turn_scheduler.py::schedule_message_turn` 的两个触发门在 **"wait" 决策下均无重查兜底**：

1. **reply_necessity 模式**：`should_trigger_by_reply_necessity` 为 False 直接 `return`。评分含**"近期已回复"存在感惩罚**（`reply_necessity.py::_calculate_recent_presence_penalty`，5min 窗口内 bot 发言占比）——回复 A 后紧邻的 B 几乎必然 wait，wait 即丢弃；
2. **frequency 模式**（默认）：`FrequencyThresholdGate.evaluate` 在平均外部消息间隔不可用时（`_get_recent_average_external_message_interval` 返回 None，如冷启动）返回 `wait`，同样丢弃。

放大因素：打断只在 planner 流式阶段生效（`PlannerInterruptController` flag 仅绑定 `chat_loop_step`），工具执行 / replyer 阶段一律 `"idle"`；默认 `planner_interrupt_max_consecutive_count = 0` 时流式阶段也不打断。对比：`delay` 决策有 `_defer_message_turn_check` 兜底、`_idle_backoff.should_delay` 也 defer——**唯独 "wait" 裸丢**。

### 补充信息

**建议修复**：两个门的 "wait" 决策统一安排延迟重查，间隔优先用平均外部消息间隔（到点后 necessity 的 idle 因子与 frequency 的空窗补偿都会加分，重查趋于触发而收敛），无样本回退 5 秒：

```python
# turn_scheduler.py
def _get_recheck_delay(self) -> float:
    average_interval = self._runtime._get_recent_average_external_message_interval()
    return average_interval if average_interval and average_interval > 0 else 5.0

# necessity 分支 else / frequency 分支末尾：
runtime._defer_message_turn_check(self._get_recheck_delay())
```

幂等性：消息被消费后 `pending_count = 0` 直接返回，不空转；`_defer_message_turn_check` 单任务取消语义，不堆积。

**已验证补丁**：`maibot_patch_all.py` 组 `[trigger]`（`--apply --only trigger`），1.2.5 上 dry-run + apply + py_compile 通过，支持 `--revert`。

---

## Issue 2（高）｜模板：Feature｜标题：planner 与 replyer 之间增加工具结果交接机制——两级模型下"查到了但答不出"

**组件**：`src/maisaka`（reasoning_engine / chat loop）｜ **状态**：插件侧已用官方通道绕过

### 期望的功能描述

planner 调用工具后，工具结果（或其摘要）应能进入 replyer 的回复生成上下文——至少覆盖**当前 logical turn** 的工具结果。现状 replyer 有意过滤全部 `ToolResultMessage`（`maisaka_generator_base.py::_is_replyer_filtered_history_message`，防工具噪音污染回复风格），导致"planner 查到资料 → replyer 组织回复"的链路断开：检索 / 查询 / 外部数据类插件实质失效（工具白调、token 白花），用户侧表现为"机器人明明查了却答非所问"，且日志无异常。

可选实现：
- **方案 A（交接机制）**：replyer 构建请求时保留当前 turn 的 `ToolResultMessage`（或摘要），只裁剪历史轮次的工具消息；
- **方案 B（args 通道）**：`reply_tool_args` 增加自由文本槽位并渲染进 replyer prompt（现状只认 `attach_pic/attach_at/attach_emoji/reply_reference/reply_style` 等固定键）。

### 补充信息

- 工具结果其实**已入** `_chat_history`（`reasoning_engine._append_tool_execution_result` ~L1669），reply 工具也把整份历史交给 replyer（`builtin_tool/reply.py:354`）——缺口仅在 replyer 的显式过滤一步；
- 定性：设计边界而非实现 bug（过滤有意为之），故按 Feature 提交；
- 插件侧已用官方通道绕过：knowledge-base 在工具成功返回文本末尾追加系统提示，引导 planner 把关键要点写入 `reply_reference` 参数（原文渲染为回复参考块，`_build_reply_reference_lines`）或依赖 `reply_reason`（渲染为「当前思考」块）。局限：依赖 planner LLM 遵从指引；备选为 `maisaka.replyer.before_model_request` 钩子按会话缓存注入（需 LRU + TTL，参照 prompt-injector）。

---

## Issue 3（中）｜模板：Bug｜标题：Maisaka 内存聊天历史时间序乱序——bot 回复"越过"生成期间到达的消息

**组件**：`src/maisaka`（runtime / reasoning_engine / builtin_tool/context）｜ **已验证补丁**：`maibot_patch_all.py` 组 `[order]`

### 遇到的问题

```
真实时间：  A(t=0) → B(t=5) → C(t=8) → bot回复(t=10)
历史顺序：  A(t=0) → bot回复 → B → C
```

LLM 时间感知失真（因果错位）、行为学习器 / 回复效果追踪读到错序序列、`_build_time_user_message` 的时间提示与消息排列互相矛盾。DB `mai_messages` 时间戳为发送时刻，**落库顺序正确**——乱序只在内存 `_chat_history`（LLM 上下文与学习器的数据源）。生成耗时越长越明显。

### 报错信息

无（行为异常，无异常栈）。

### 如何重现此问题？

1. 群聊中 A 发言触发 planner（planner + replyer 两级 LLM，耗时数秒到数十秒）；
2. 期间其他群友 B、C 陆续发言；
3. bot 回复发出后查看 `_chat_history` / LLM 上下文：回复排在 B、C 之前。

### 可能造成问题的原因

两条入历史路径都是**处理序 append**，处理时刻不同：

1. **入站消息**：`register_message` 只进 `message_cache`；下一轮 turn / 轮间才经 `_collect_pending_messages` → `_ingest_messages` → `_insert_chat_history_message`（reasoning_engine ~L1343）append；
2. **bot 回复**：reply / send_image / send_emoji → `send_service` → `runtime.append_sent_message_to_chat_history`（~L603）**发送瞬间** append。

### 补充信息

**建议修复**：入历史改按 `message.timestamp` **二分有序插入**（时间戳相同保持先来后到，缺失/不可比较回退 append），覆盖全部 3 处：

| 文件 | 位置 | 路径 |
|---|---|---|
| `src/maisaka/runtime.py` | `append_sent_message_to_chat_history`（~L603） | bot 发送消息（reply/send_image/send_emoji 共用） |
| `src/maisaka/reasoning_engine.py` | `_insert_chat_history_message`（~L1343） | 入站消息收集合并 |
| `src/maisaka/builtin_tool/context.py` | L457 / L491 / L531 | CLI 回复、旧运行时兜底、表情包 |

副作用：`_drop_head_context_messages` 裁掉的是最旧消息，语义更正确；`response.raw_messages` 创建时间即最新，天然有序。

**已验证补丁**：`maibot_patch_all.py` 组 `[order]`，1.2.4 与 1.2.5 均验证通过，支持 `--revert`。

---

## Issue 4（低-中）｜模板：Bug｜标题：未声明 capabilities 的调用被拒后日志仅 debug 级——插件功能静默失效

**组件**：`AuthorizationManager`、插件运行时

### 遇到的问题

宿主只对 `manifest.capabilities` **声明过的能力**签发令牌；未声明时该插件所有该能力调用被拒（`E_CAPABILITY_DENIED` / "未注册能力令牌"），但拒绝日志仅 debug 级——插件作者在默认日志级别下无任何线索，表现为"功能静默失效"。

### 报错信息

调用侧仅返回失败 / `E_CAPABILITY_DENIED`；宿主日志无 warning 级提示（拒绝原因在 debug 级）。

### 如何重现此问题？

1. 插件 `_manifest.json` 的 `capabilities` 不声明 `database.query`；
2. 插件代码调用 `ctx.db` 查询；
3. 默认日志级别下运行：全部调用静默失败，日志无可见拒绝原因。

### 可能造成问题的原因

能力拒绝的日志级别只有 debug，且无修复指引。

### 补充信息

建议修复：拒绝日志提升到 `warning` 并附指引（"请在 `_manifest.json` 的 `capabilities` 中声明后重载"）；或同一插件对同一未声明能力的拒绝做一次性显式告警（去重防刷屏）。实测案例：gif-storyboard v1.0.1 未声明 `database.query`，描述搬运功能无声失效。

---

## Issue 5（低）｜模板：Feature｜标题：识别链对图片格式做归一化——GIF 支持不应因模型 Provider 而异

**组件**：`ImageManager.get_image_description`

### 期望的功能描述

视觉识别对图片格式的支持在不同 Provider 间对齐：OpenAI 兼容后端支持 `gif`；Gemini / 插件 Provider 不支持并直接抛"不受支持的图片格式"异常。同一份用户配置在不同后端下行为完全不同（正常识别 vs 每次失败）。

### 补充信息

建议识别链入口做格式归一化：不支持的 Provider 自动降级为首帧 PNG/JPEG（Pillow 抽帧）；至少在文档与配置校验中明确差异。（gif-storyboard 的"GIF→JPEG 合成图"替换模式实际已绕开，但原生链路的 GIF 静态图仍受影响。）

---

## Issue 6（低）｜模板：Feature｜标题：视觉占位刷新器支持"描述升级回填"——按 image_hash 同步数据库最新描述

**组件**：`chat_history_refresher`、`ImageManager`

### 期望的功能描述

planner 轮前的 `chat_history_refresher` 目前**只回填空占位**组件（content 为空或等于 `"[图片，识别中.....]"` / `"[表情包]"`）。一旦组件文本被首次描述固化（如 GIF 只取首帧的描述），即使数据库该 `image_hash` 的 `description` 后续更新（`update_image_description` 只写 `description/last_used_time/vlm_processed`），刷新器也不再回填——"先固化、后覆盖描述"的方案必然卡死（两轮实测），插件被迫用"多帧描述作为第一份描述"的绕行架构。

建议增加可选的"描述升级回填"：按 `image_hash` 比对 DB 最新 `description` 与固化文本，不一致则更新（开关控制，默认关闭省 token）。

### 补充信息

无。

---

## Issue 7（discussion）｜模板：Feature｜标题：钩子间组件序列化往返保留 `x_` / `plugin_` 前缀自定义字段

**组件**：插件运行时序列化层

### 期望的功能描述

`chat.receive.before_process` 与 `after_process` 钩子之间 message 以 dict 传递（进前反序列化、返回后重建），组件对象上插件附加的自定义字段（如 `comp._my_kind = ...`）被**静默丢弃**——插件无法跨钩子传状态。建议序列化层保留 `x_` / `plugin_` 前缀的额外字段作为官方跨钩子通道；至少在插件开发文档中明确该边界（当前"文档没写"容易被假设成可用）。

### 补充信息

gif-storyboard 实测：被迫改用插件内部登记表（按"组件索引 + hash"键控），可行但增加复杂度。

---

## 附：已确认无需上报的行为（避免误报）

| 行为 | 结论 |
|---|---|
| 宿主表情链路对 GIF 表情**原生抽帧拼接分析**（生成 ≤5 个逗号分隔情绪标签存 EMOJI.description） | 正常能力。插件旁路反而双倍解析 + 标签格式覆写（gif-storyboard v1.1.4 已移除自身旁路） |
| EMOJI 记录的 `description` 兼作情绪标签来源（逗号分隔格式） | 设计事实。插件侧约定：只写 IMAGE 类型记录，禁止覆写 EMOJI 的 description |
| `no_file_flag` 被多处按文件系统真相自动修正（落库/清理/ensure_image_saved） | 设计如此。语义由文件系统决定，插件不可挪用 |
| planner 轮次模型：上一轮结束前新消息只进缓冲（受 `planner_interrupt_max_consecutive_count` 打断限制） | 正常机制。乱序问题见 Issue 3（修排序即可）；触发丢失见 Issue 1（补重查兜底即可），无需改轮次模型 |

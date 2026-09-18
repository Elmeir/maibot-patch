#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaiBot 一键补丁（自包含，无需其它文件）
========================================
一次应用全部补丁：

  [A] issue #2010
      A1. src/A_memorix/core/utils/summary_importer.py
          给 vector_store / graph_store.save() 加判空 → 向量通道降级时不再抛 NoneType、
          写回游标不再冻结、不再产生近似摘要洪水。
      A2. src/A_memorix/core/embedding/api_adapter.py
          构造适配器时清空类级缓存 → 内核热重建后自检发真实请求 → 指纹 observed → 向量通道免重启自愈。

  [B] 工具描述分工（方案 B）
      B1. src/maisaka/builtin_tool/query_person_profile.py —— 专管「人物画像/档案」，按名字查人、无需日期。
      B2. src/maisaka/builtin_tool/query_memory.py          —— 管「记忆片段/事实/关系/经历」，
                                                               time/hybrid 必须给时间范围，要画像请用 B1。

  [C] 聊天记录时间顺序修复（LLM 上下文层乱序）
      现象：planner/replyer 生成耗时期间到达的群友消息，在聊天记录里排在 bot 回复之后——
      回复被"提前"到回复对象后面，时间序失真（DB mai_messages 时间戳是发送时刻，正确；
      乱序发生在 Maisaka 内存历史 _chat_history）。
      根因（1.2.4 源码核查）：
        - 入站消息先进 message_cache，下一轮/轮间合并才经 _ingest_messages 追加进 _chat_history；
        - bot 回复（reply/send_image/send_emoji → send_service → runtime.
          append_sent_message_to_chat_history）在发送瞬间直接 append 进 _chat_history；
        - 纯处理序 append ⇒ 回复越过时间上更早、但尚未被收集的入站消息。
      方案：新增 insert_chat_history_message_sorted()（按 message.timestamp 二分有序插入，
      时间戳相同保持先来后到；缺失/不可比较时回退尾部追加，永不抛错），并让全部入历史
      路径改走它：
      C1. src/maisaka/runtime.py                      —— 新增方法 + append_sent_message_to_chat_history 改调
      C2. src/maisaka/reasoning_engine.py             —— _insert_chat_history_message（入站消息入口）改调
      C3. src/maisaka/builtin_tool/context.py         —— CLI 回复 / 旧运行时兜底 / 表情包 3 处 append 改调
      副作用评估：_drop_head_context_messages 从头部裁剪 → 裁掉的是最旧消息，语义更正确；
      学习器/效果追踪按时间序读取，不受影响。

  [D] planner 误回复自己消息 —— 不做成补丁
      is_self_message="true" 属性在 prompts/*/maisaka_chat*.prompt 中零解释，导致 planner
      把 bot 自己的回复当他人发言来回应（详见 maibot-issues.md Issue 8）。但提示词是
      数据文件：WebUI「提示词」页 / data/custom_prompts/ 可直接管理覆盖，不需要补丁；
      需要添加的文本见 maibot-issues.md Issue 8 补充信息。

  [target] 出现新消息后 planner 重复回复旧消息——末尾提醒缺少回复目标指引
      现象：A 消息 → bot 回复 A（带引用）→ B 消息到达 → planner 再次回复 A（而非 B
      或不回复）。reply 工具虽检测"重复目标"并把提醒传给回复器，但不拦截发送；且
      planner 侧提示词没有任何"对同一条消息只回复一次/优先回应新消息"的约束。
      根因（1.2.4/1.2.5 源码核查）：planner 请求的新消息与历史混排、无分界；每轮
      末尾的一次性 user 提醒（PLANNER_FINAL_USER_REMINDER_TEMPLATE，
      chat_loop_service.py:86）只有一句"输出对{bot_name}发言的分析"，完全没有回复
      目标指引。
      方案：改写该常量——末尾提醒显式声明"越靠后越新；重点针对最新的、尚未回复
      过的用户消息决定动作；更早的历史仅供理解背景，不要回应；自己的消息不是别人
      的发言"。位置紧邻模型生成点、注意力权重最高，单点常量替换。
      注：与 [D] 的 WebUI 提示词文本互补不重复（那边讲消息格式语义，这边讲本轮
      决策对象）；也与 Issue 3 的 [order] 排序修复正交。

  [trigger] 回复后紧邻消息的触发被静默丢弃（"A→planner→B→reply→回复A→B 丢失"）
      现象：A 发言触发 planner → planner 处理期间 B 发言 → planner 调 reply → 回复 A →
      B 的消息触发被丢弃，B 滞留 message_cache 不进 Planner（直到下一条消息才可能被
      顺带处理；群里没人再说话则 B 永远不被处理）。
      根因（1.2.5 源码核查）：src/maisaka/turn_scheduler.py 的两个触发门在"wait"决策下
      都没有任何重查兜底：
        - reply_necessity 模式：should_trigger_by_reply_necessity 为 False 时直接 return。
          评分含"近期已回复"存在感惩罚（reply_necessity.py
          _calculate_recent_presence_penalty，5min 窗口）——回复 A 后紧邻的 B 几乎必然
          wait；wait 后不安排重查，B 的触发即被丢弃。
        - frequency 模式：FrequencyThresholdGate.evaluate 在平均外部消息间隔不可用时
          （runtime._get_recent_average_external_message_interval 返回 None，如冷启动）
          返回 wait，同样直接结束，无延迟重查（delay 决策有 _defer_message_turn_check
          兜底，wait 没有）。
      另：打断请求在 planner 流式阶段之外一律 "idle"（PlannerInterruptController.request），
      默认 planner_interrupt_max_consecutive_count=0 时流式阶段也不打断——B 只能靠
      调度器入队，进一步放大上述缺口。
      方案：两个门的 wait 决策统一安排 _defer_message_turn_check(重查间隔)：
        T1. turn_scheduler.py —— necessity wait → defer（新增 _get_recheck_delay）
        T2. turn_scheduler.py —— frequency wait → defer
        T3. turn_scheduler.py —— 新增 _get_recheck_delay()：优先平均外部消息间隔
            （到点后 reply_necessity 的 idle 因子与 frequency 的空窗补偿都会加分，
            重查趋于触发而收敛），冷启动无样本时回退 5 秒。
      幂等性：重查时 pending 消息一旦被消费（_last_processed_index 推进）pending_count=0
      直接返回，不会空转；_defer_message_turn_check 自带单任务取消语义，不会堆积。

  [wait] wait 时长无上限钳制（模型可自选任意等待秒数）
      现象：wait 工具的 seconds 参数由 LLM 自选，宿主只钳下限 0（wait.py
      wait_seconds = max(0, wait_seconds)），模型传 300 秒就真等 300 秒——
      期间新消息不打断等待，造成长时间沉默与空转。
      方案：wait.py 钳制 wait_seconds ≤ 60（工具结果文本会如实报告钳后的值，
      模型能感知）；工具描述同步注明上限，引导模型一开始就选合理时长。
      注：连续 wait 次数上限已有配置（chat.reply_timing.max_consecutive_wait_count），
      本补丁只管"每次等多久"。

  [reload] file_watcher 源码变更从全量重启改为定向重载
      现象：插件树里任何一个 .py 变化都会重启全部插件运行时（shutdown 所有
      Supervisor + 依赖同步 + 冷启动重新 import 全部插件）。重启成本随插件数
      与加载期数据线性增长，装得越多越慢。
      根因（1.2.5 源码核查）：_handle_plugin_source_changes
      （src/plugin_runtime/integration.py）把源码变更一律走 _restart_supervisors
      全量重启；而宿主本就具备单插件热重载通路（runner 侧 plugin.reload_batch
      RPC，宿主侧 reload_plugins_globally，supervisor.reload_plugins），仅源码
      变更路径没有接上。
      方案：把变更文件路径映射到受影响插件 ID（复用 _match_plugin_id_for_
      supervisor，跨两个 Supervisor 查找去重）：
        - 映射不到任何已注册插件（如新装插件目录）→ 保持原全量重启；
        - 映射得到 → 依赖同步照跑（保证新声明的依赖先装好/被阻止），然后
          reload_plugins_globally(受影响插件) 定向重载；
        - 定向重载失败（如全部被依赖流水线阻止）→ 回退全量重启，保底不劣化。
      插件侧无需任何修改：重载链路 = on_unload → purge 模块 → 重新 import →
      on_load → 组件重注册，runner 已处理 sys.modules 清理。
      v2 增强（2026-09-19，部署实测暴露的中间态问题）：
        - 现象：整目录替换/大目录复制进行中时，600ms 防抖窗口仍可能提前触发，
          读到空 _manifest.json / 半截 .py → 定向重载失败 → 回退全量重启
          （日志链：Manifest 校验失败 → 插件目录已不存在(已恢复旧版本) →
          回退全量重启）；
        - 增强一：插件源码 watcher 防抖 600 → 1500ms（中途批次更易合并到稳定态）；
        - 增强二：变更批次就绪预检——_manifest.json 须可解析、.py 须可编译；
          不就绪则延迟 1.5s 复查一次，仍不就绪跳过本批（等后续文件事件），
          不让半截文件进入重载/全量路径（跳过时打印 warning）。

  [summary] 引用回复导致长期记忆张冠李戴（A 引用 B 说话 → 事实归到 B）
      现象：A 引用 B 的回复并发言，长期记忆/人物画像里经常被记成「B XXX」——
      说话人归属错乱。
      根因（1.2.5 源码核查）：引用回复在纯文本里渲染为行内前缀
      `[回复了B的消息: B的原话]`（src/chat/message_receive/message.py:441/452），
      拼进行后为 `[时间] A说：[回复了B的消息: B的原话] A说的XXX`。总结提示词
      （A_memorix summary_importer.py SUMMARY_PROMPT_TEMPLATE）虽有「引用文本
      不入事实」「严格绑定发言者」规则，但没有任何一处解释这个前缀的语义，
      模型看到行内 B 的名字与原话便把内容归到 B 头上。
      方案：在 SUMMARY_PROMPT_TEMPLATE 的事实筛选规则中补充三条引用归属规则：
        - 每条消息发言者只认「说：」前面的名字；内容开头的
          [回复了X的消息: 原话] 是发言者引用的他人原话，不是发言者或 X 的新发言；
        - 引用场景示例：只有引用者明确复述确认，被引用原话才能记为被引用者的
          事实，且不得张冠李戴到引用者身上；
        - 引用者明确转述确认时才可记录，来源为引用者转述。
      注：渲染格式不动——reply_necessity.py 等处依赖 `[回复了...]` 前缀做正则
      剥离，改渲染格式牵连面大；只补提示词语义。宿主中期记忆模板
      prompts/zh-CN/mid_term_memory_summary.prompt 是数据文件，可在 WebUI
      自行加同类规则（不需补丁）。

  [episode] Episode 分段输出语言不受控（英文提示词无语言约束）
      现象：Episode 的 title/summary/keywords 经常输出英文或中英混杂。
      根因（1.2.5 源码核查）：episode_segmentation_service.py 的分段提示词是
      纯英文硬编码，且没有像 factual/narrative 策略那样带 build_language_guard
      语言保持约束，输出语言全凭模型发挥。
      方案：在分段提示词 Rules 末尾补第 5 条——title/summary/participants/
      keywords 用输入段落的主导语言书写（中文聊天写中文），不做翻译、不混杂；
      JSON schema 与其余逻辑不动。

安全
----
* 默认 **dry-run**（只打印 diff），加 --apply 才写文件；
* 每个文件写前自动备份 `.bak-<时间戳>`；
* 写后自动 `py_compile` 语法自检，**若语法报错自动回滚该文件**；
* 幂等：已打过则跳过；
* **自动刷新**：补丁常量更新后再 --apply，锚点被旧版补丁占用时自动从备份
  重放最新补丁（打印 [刷新]），无需先 --revert；同文件多组补丁整体重建，
  刷新不新增备份（备份链保留的原始副本继续作为还原基准）；
* 可还原：`--revert` 从最近备份还原所有文件。

定位
----
--repo > 环境变量 MAIBOT_ROOT > 常见路径(含 C:\\Users\\Administrator\\Desktop\\MaiBot) >
当前目录及父级 > 盘符浅扫。--detect 只定位并打印。

用法
----
  python maibot_patch_all.py --detect                 # 确认找到哪个目录
  python maibot_patch_all.py                          # 预览全部 diff
  python maibot_patch_all.py --apply                  # 一键打全部补丁
  python maibot_patch_all.py --apply --only tools     # 只打 B 组（可选 2010 / order）
  python maibot_patch_all.py --revert                 # 一键还原
"""

from __future__ import annotations

import argparse
import difflib
import glob
import os
import py_compile
import shutil
import sys
import tempfile
import time

# ============================================================ 新文本
PROFILE_DESC = (
    'description="查询某个人的长期人物画像/档案（身份设定、稳定了解、相处偏好、近期互动等）。'
    '需要了解「某人是谁 / 关于某人的信息 / 某人的画像」时用本工具：'
    '按名字、昵称或关键词提供 person_name 即可，不需要任何日期或时间范围。",'
)
PROFILE_PERSON_ID_DESC = '"description": "内部人物ID；仅在已知时填，通常留空。",'
PROFILE_PERSON_NAME_DESC = '"description": "要查询的人的名字、昵称或关键词；按名字查人时填这个（通常填它）。",'
PROFILE_LIMIT_DESC = '"description": "返回的画像证据条数上限。",'

MEMORY_DESC = (
    'description="检索长期记忆（事实、关系、事件/经历等片段）。'
    '查绰号、别名、关系、事件等持久事实用 search 模式（无需时间）；'
    'time/hybrid 模式必须提供 time_start/time_end。'
    '每次查询只聚焦一个主题或一个关键词；有多个关键词时拆成多次调用，不要拼在同一个 query 里。'
    '若要查某个人的画像/档案，请用 query_person_profile。",'
)
MEMORY_MODE_DESC = (
    '"description": "检索模式。'
    'search=按关键词/事实检索（查绰号、别名、关系、事件等持久事实时首选，无需时间）；'
    'time=按时间段检索（必须提供 time_start 或 time_end）；'
    'episode=按经历检索；aggregate=整体印象汇总；'
    'hybrid=混合检索（必须提供 time_start 或 time_end，不适合查绰号/别名等无时间维度的事实）。'
    '不确定时用 search。查人物画像/档案请改用 query_person_profile。",'
)
MEMORY_PERSON_NAME_DESC = (
    '"description": "人物名；用于对本工具的记忆结果做定向过滤'
    '（查人物画像请用 query_person_profile）。",'
)
MEMORY_QUERY_DESC = (
    '"description": "单个主题、关键词或模糊主题短语（如「做菜」「出差讨论」），'
    '用自然语言即可，不必是精确关键词；不要把多个主题拼在一起。",'
)

SUM_NEW = [
    "# [patch #2010] 向量通道降级时 vector_store/graph_store 可能为 None，判空避免 AttributeError",
    "if self.vector_store is not None:",
    "    self.vector_store.save()",
    "if self.graph_store is not None:",
    "    self.graph_store.save()",
]
ADA_NEW = [
    "",
    "# [patch #2010] 内核热重建会复用旧的【类级(进程级)】缓存：新实例自检 encode 命中旧缓存、",
    "# 无真实 API 请求 → _last_success_model_name 永远为空 → 指纹永远 configured、非 observed",
    "# → 向量通道永久降级（issue #2010）。每次构造适配器时清空类级缓存，确保自检发出真实请求。",
    "type(self)._GLOBAL_DIMENSION_CACHE.clear()",
    "type(self)._GLOBAL_TEXT_EMBEDDING_CACHE.clear()",
]

# ============================================================ [C] 聊天记录时间顺序
M = os.path.join("src", "maisaka")

# C1 新增方法（插在 _schedule_sent_image_recognition 之前；基准缩进由引擎按锚点行注入）
SORTED_INSERT_HELPERS = [
    "def insert_chat_history_message_sorted(self, message) -> None:",
    '    """按消息时间戳有序插入 Maisaka 聊天历史（时间戳相同保持先来后到）。',
    "",
    "    背景：入站消息在下一轮开始时才从缓存收集进历史（message_cache →",
    "    _collect_pending_messages → _ingest_messages），而 bot 回复在发送瞬间",
    "    就写入历史——纯处理序 append 会让回复排在时间上更早的消息之前",
    "    （planner/replyer 生成耗时越长越明显）。改为按 timestamp 二分插入；",
    "    时间戳缺失或不可比较时退回尾部追加，保证永不抛错。",
    '    """',
    "    try:",
    "        from bisect import bisect_right",
    "",
    "        index = bisect_right(self._chat_history, message.timestamp, key=lambda item: item.timestamp)",
    "    except (AttributeError, TypeError):",
    "        self._chat_history.append(message)",
    "        return",
    "    self._chat_history.insert(index, message)",
    "",
    "",
    "def _schedule_sent_image_recognition(self, message: SessionMessage) -> None:",
    '    """为已发送并同步进历史的图片消息调度后台识图。"""',
]

# C2：入站消息入口改为有序插入
INGEST_OLD = [
    "def _insert_chat_history_message(self, message: LLMContextMessage) -> int:",
    '"""将消息按处理顺序追加到聊天历史末尾。"""',
    "self._runtime._chat_history.append(message)",
    "return len(self._runtime._chat_history) - 1",
]
INGEST_NEW = [
    "def _insert_chat_history_message(self, message: LLMContextMessage) -> int:",
    '    """按消息时间戳有序插入聊天历史（避免回复越过时间上更早的入站消息）。"""',
    "    self._runtime.insert_chat_history_message_sorted(message)",
    "    return len(self._runtime._chat_history) - 1",
]

# ============================================================ [D] planner 误回复自己消息 —— 不做成补丁
# 提示词是数据文件：WebUI「提示词」页 / data/custom_prompts/ 可直接管理覆盖，
# 需要添加的文本见 maibot-issues.md Issue 8 补充信息。

B = os.path.join("src", "maisaka", "builtin_tool")
A = os.path.join("src", "A_memorix")

# ============================================================ [trigger] 触发门 wait 兜底
TS = os.path.join("src", "maisaka")

# T1：necessity 模式 wait → 安排延迟重查
TRIGGER_NECESSITY_OLD = [
    "if is_reply_necessity_trigger_enabled():",
    "if self.should_trigger_by_reply_necessity(",
    "pending_messages=runtime.message_cache[runtime._last_processed_index :],",
    "trigger_threshold=trigger_threshold,",
    "formatted_frequency=formatted_frequency,",
    "pending_count=pending_count,",
    "):",
    "runtime._enqueue_message_turn()",
    "return",
]
TRIGGER_NECESSITY_NEW = [
    "if is_reply_necessity_trigger_enabled():",
    "    if self.should_trigger_by_reply_necessity(",
    "        pending_messages=runtime.message_cache[runtime._last_processed_index :],",
    "        trigger_threshold=trigger_threshold,",
    "        formatted_frequency=formatted_frequency,",
    "        pending_count=pending_count,",
    "    ):",
    "        runtime._enqueue_message_turn()",
    "    else:",
    "        # [patch trigger] wait 决策兜底：安排延迟重查，避免触发被静默丢弃。",
    "        # 必要性评分含\"近期已回复\"存在感惩罚（5min 窗口）——回复后紧邻的",
    "        # 新消息几乎必然 wait；无重查时触发被丢弃，消息滞留缓存直到",
    "        # 下一条消息到来才可能被顺带处理（群里没人再说话则永远滞留）",
    "        runtime._defer_message_turn_check(self._get_recheck_delay())",
    "    return",
]

# T2：frequency 模式 wait（平均间隔不可用等场景）→ 安排延迟重查
TRIGGER_FREQUENCY_OLD = [
    "if frequency_result.should_trigger:",
    "runtime._enqueue_message_turn()",
    "return",
    "",
    "if frequency_result.decision == \"delay\" and frequency_result.delay_seconds is not None:",
    "runtime._defer_message_turn_check(frequency_result.delay_seconds)",
]
TRIGGER_FREQUENCY_NEW = [
    "if frequency_result.should_trigger:",
    "    runtime._enqueue_message_turn()",
    "    return",
    "",
    "if frequency_result.decision == \"delay\" and frequency_result.delay_seconds is not None:",
    "    runtime._defer_message_turn_check(frequency_result.delay_seconds)",
    "    return",
    "",
    "# [patch trigger] wait 决策兜底（平均外部消息间隔不可用等场景）：安排延迟重查，",
    "# 避免消息触发被静默丢弃后滞留缓存",
    "runtime._defer_message_turn_check(self._get_recheck_delay())",
]

# T3：新增 _get_recheck_delay（插在 should_trigger_by_reply_necessity 定义之前）
TRIGGER_RECHECK_DELAY_ANCHOR = ["def should_trigger_by_reply_necessity("]
TRIGGER_RECHECK_DELAY_NEW = [
    "def _get_recheck_delay(self) -> float:",
    "    \"\"\"wait 兜底的重查间隔：优先平均外部消息间隔（到点后空闲因子加分，趋于触发）。\"\"\"",
    "    average_interval = self._runtime._get_recent_average_external_message_interval()",
    "    if average_interval and average_interval > 0:",
    "        return average_interval",
    "    return 5.0",
    "",
    "",
    "def should_trigger_by_reply_necessity(",
]

# ============================================================ [target] 末尾提醒补回复目标指引
TARGET_OLD = [
    "PLANNER_FINAL_USER_REMINDER_TEMPLATE = (",
    '"你需要输出对{bot_name}发言的分析，视情况输出文本内容的分析，思考是否进行工具调用"',
    ")",
]
TARGET_NEW = [
    "PLANNER_FINAL_USER_REMINDER_TEMPLATE = (",
    '    "上下文中越靠后的消息越新。请重点针对最新的、{bot_name} 尚未回复过的用户消息决定下一步动作；"',
    '    "更早的历史消息仅供理解背景，不要回应它们；{bot_name} 自己发送的消息（is_self_message=\\"true\\"）不是别人的发言。"',
    '    "之后，你需要输出对{bot_name}发言的分析，视情况输出文本内容的分析，思考是否进行工具调用"',
    ")",
]

# ============================================================ [wait] wait 时长上限钳制
# 注意：引擎按"匹配行缩进 + new 行原文"叠加缩进（_find 按 strip 匹配），
# new 行不要带绝对缩进，需要层级时只写相对缩进。
WAIT_SECONDS_OLD = [
    "wait_seconds = max(0, wait_seconds)",
]
WAIT_SECONDS_NEW = [
    "# 硬上限：模型传入的等待时长最大不超过 60 秒，防止长 wait 造成长时间沉默",
    "wait_seconds = max(0, min(wait_seconds, 60))",
]
WAIT_DESC_OLD = [
    '"description": "等待秒数。",',
]
WAIT_DESC_NEW = [
    '"description": "等待秒数，建议 30~60，最长不超过 60。",',
]

# ============================================================ [reload] file_watcher 定向重载
# 注意：引擎按"匹配行缩进 + new 行原文"叠加缩进（_find 按 strip 匹配），
# new 行不要带绝对缩进，需要层级时只写相对缩进（首行 0 → 落在函数体 8 空格）。

# reload v2-a：插件源码 watcher 防抖窗口 600 → 1500ms（整目录替换/大目录复制的
# 中途批次更容易被合并到稳定态之后，从源头减少「半截文件被当成变更」的概率）
RELOAD_DEBOUNCE_OLD = [
    "paths=watch_paths,",
    "debounce_ms=600,",
    "callback_timeout_s=15.0,",
]
RELOAD_DEBOUNCE_NEW = [
    "paths=watch_paths,",
    "debounce_ms=1500,  # [reload] 600→1500ms：目录替换/大目录复制的中途批次更易合并到稳定态",
    "callback_timeout_s=15.0,",
]

RELOAD_OLD = [
    "dependency_sync_state = await self._sync_plugin_dependencies(plugin_dirs)",
    'restart_reason = "file_watcher"',
    "if dependency_sync_state.environment_changed:",
    'restart_reason = "file_watcher_dependency_install"',
    "elif dependency_sync_state.blocked_changed_plugin_ids:",
    'restart_reason = "file_watcher_blocklist_changed"',
    "",
    "restarted = await self._restart_supervisors(restart_reason)",
    "if not restarted:",
    'logger.warning(f"插件源码变更后重启 Supervisor 失败: {restart_reason}")',
]
# reload v2-b：变更批次就绪预检 + 定向重载（预检不就绪时跳过本批，等后续事件）
RELOAD_NEW = [
    "# [reload] 就绪预检：目录整体替换/大目录复制进行中时，watcher 仍可能提前",
    "# 触发并读到空的 _manifest.json / 半截的 .py——直接重载会失败并回退全量",
    "# 重启。先做批次内容完整性检查；不就绪则延迟 1.5s 复查一次，仍不就绪",
    "# 跳过本批（等后续文件事件），避免「校验失败→重载失败→全量重启」链路。",
    "def _reload_sources_ready() -> bool:",
    "    import json as _preflight_json",
    "    for changed_path in relevant_source_changes:",
    "        try:",
    "            if not changed_path.is_file():",
    "                continue  # 删除类变更不做内容检查",
    '            if changed_path.name == "_manifest.json":',
    '                with changed_path.open("r", encoding="utf-8") as manifest_file:',
    "                    _preflight_json.load(manifest_file)",
    '            elif changed_path.suffix == ".py":',
    '                compile(changed_path.read_text(encoding="utf-8"), str(changed_path), "exec")',
    "        except Exception:",
    "            return False",
    "    return True",
    "",
    "if not _reload_sources_ready():",
    "    await asyncio.sleep(1.5)",
    "if not _reload_sources_ready():",
    "    logger.warning(",
    '        "插件源码变更批次尚未写入完整（可能仍在替换/复制），跳过本批等待后续事件: "',
    '        + ", ".join(str(changed_path) for changed_path in relevant_source_changes[:5])',
    "    )",
    "    return",
    "",
    "affected_plugin_ids: list[str] = []",
    "for supervisor in self.supervisors:",
    "    for change_path in relevant_source_changes:",
    "        matched_plugin_id = self._match_plugin_id_for_supervisor(supervisor, change_path)",
    "        if matched_plugin_id and matched_plugin_id not in affected_plugin_ids:",
    "            affected_plugin_ids.append(matched_plugin_id)",
    "dependency_sync_state = await self._sync_plugin_dependencies(plugin_dirs)",
    "if not affected_plugin_ids:",
    '    restart_reason = "file_watcher"',
    "    if dependency_sync_state.environment_changed:",
    '        restart_reason = "file_watcher_dependency_install"',
    "    elif dependency_sync_state.blocked_changed_plugin_ids:",
    '        restart_reason = "file_watcher_blocklist_changed"',
    "    restarted = await self._restart_supervisors(restart_reason)",
    "    if not restarted:",
    '        logger.warning(f"插件源码变更后重启 Supervisor 失败: {restart_reason}")',
    "    return",
    "",
    'reloaded = await self.reload_plugins_globally(affected_plugin_ids, reason="file_watcher")',
    "if not reloaded:",
    '    logger.warning(f"插件源码变更后定向重载失败，回退全量重启: {affected_plugin_ids}")',
    '    restarted = await self._restart_supervisors("file_watcher_fallback")',
    "    if not restarted:",
    '        logger.warning("插件源码变更后回退全量重启失败")',
]

# ============================================================ [summary] 引用回复归属
# 注意：引擎按"匹配行缩进 + new 行原文"叠加缩进（_find 按 strip 匹配）。
# SUMMARY_PROMPT_TEMPLATE 是模块级字符串常量，内容行无缩进（base=空），
# new 行按原文写入即可。
SUMMARY_QUOTE_OLD = [
    "- 相似昵称或多人多线程时，必须严格绑定发言者与事实；不要把 A 的地点、行程、偏好、健康状况合并到 B 身上。",
]
SUMMARY_QUOTE_NEW = [
    "- 相似昵称或多人多线程时，必须严格绑定发言者与事实；不要把 A 的地点、行程、偏好、健康状况合并到 B 身上。",
    "- 每条消息的发言者只认「说：」前面的名字；消息内容开头的「[回复了X的消息: 原话]」是发言者引用的他人原话，属于引用文本：它既不是发言者本人说的话，也不是 X 的新发言。",
    "- 引用场景归属示例：「A说：[回复了B的消息: 我明天去北京] 好啊一起」——只能记录 A 回应了同行/计划；除非 A 明确复述确认，否则不要把「B 明天去北京」记为 B 的事实，更不要把被引用内容里的信息张冠李戴到引用者 A 身上。",
    "- 只有当引用者明确转述并确认被引用内容时（如「B 明天去北京，我也去」），才可把「B 明天去北京」记录为 B 的事实；来源按引用者转述处理，且引用者本人的事实只包括其自己陈述的部分。",
]

# ============================================================ [episode] Episode 分段输出语言
# 注意：episode_segmentation_service.py 的分段提示词是纯英文硬编码，且不像
# factual/narrative 策略那样带 build_language_guard 语言约束——中文聊天切出的
# Episode 标题/摘要可能出英文或中英混杂。这里只在 Rules 末尾补一条语言规则，
# JSON schema 与其余逻辑不动。源码行为括号续行（缩进 12），引擎按匹配行缩进叠加。
EPISODE_LANG_OLD = [
    '"4) if uncertain, still provide best effort confidence values.\\n"',
]
EPISODE_LANG_NEW = [
    '"4) if uncertain, still provide best effort confidence values.\\n"',
    '"5) Write title, summary, participants and keywords in the dominant language of the input paragraphs (for Chinese chat, write Chinese); never translate and never mix languages.\\n"',
]

# ============================================================ 补丁清单（按文件分组）
GROUPS = {
    "2010": {
        os.path.join(A, "core", "utils", "summary_importer.py"): [
            {"kind": "replace_lines", "old": ["self.vector_store.save()", "self.graph_store.save()"], "new": SUM_NEW},
        ],
        os.path.join(A, "core", "embedding", "api_adapter.py"): [
            {"kind": "insert_after", "anchor": 'self._last_success_provider_name = ""', "new": ADA_NEW},
        ],
    },
    "tools": {
        os.path.join(B, "query_person_profile.py"): [
            {"kind": "replace_after", "anchor": 'name="query_person_profile",', "prefix": "description=", "new": PROFILE_DESC},
            {"kind": "replace_after", "anchor": '"person_id": {', "prefix": '"description":', "new": PROFILE_PERSON_ID_DESC},
            {"kind": "replace_after", "anchor": '"person_name": {', "prefix": '"description":', "new": PROFILE_PERSON_NAME_DESC},
            {"kind": "replace_after", "anchor": '"limit": {', "prefix": '"description":', "new": PROFILE_LIMIT_DESC},
        ],
        os.path.join(B, "query_memory.py"): [
            {"kind": "replace_after", "anchor": 'name="query_memory",', "prefix": "description=", "new": MEMORY_DESC},
            {"kind": "replace_after", "anchor": '"query": {', "prefix": '"description":', "new": MEMORY_QUERY_DESC},
            {"kind": "replace_after", "anchor": '"mode": {', "prefix": '"description":', "new": MEMORY_MODE_DESC},
            {"kind": "replace_after", "anchor": '"person_name": {', "prefix": '"description":', "new": MEMORY_PERSON_NAME_DESC},
        ],
    },
    "order": {
        # C1：bot 发送消息入历史的主路径（reply/send_image/send_emoji 都经这里）
        os.path.join(M, "runtime.py"): [
            {
                "kind": "replace_lines",
                "old": ["self._chat_history.append(history_message)"],
                "new": ["self.insert_chat_history_message_sorted(history_message)"],
            },
            {
                "kind": "replace_lines",
                "old": [
                    "def _schedule_sent_image_recognition(self, message: SessionMessage) -> None:",
                    '"""为已发送并同步进历史的图片消息调度后台识图。"""',
                ],
                "new": SORTED_INSERT_HELPERS,
            },
        ],
        # C2：入站消息（下一轮收集合并时）入历史
        os.path.join(M, "reasoning_engine.py"): [
            {"kind": "replace_lines", "old": INGEST_OLD, "new": INGEST_NEW},
        ],
        # C3：CLI 回复 / 旧运行时兜底 / 表情包 —— 三处相同 append 全部替换
        os.path.join(M, "builtin_tool", "context.py"): [
            {
                "kind": "replace_lines",
                "all": True,
                "old": ["self.runtime._chat_history.append(history_message)"],
                "new": ["self.runtime.insert_chat_history_message_sorted(history_message)"],
            },
        ],
    },
    "trigger": {
        # 回复后紧邻消息的触发被静默丢弃：两个触发门的 wait 决策补延迟重查
        os.path.join(TS, "turn_scheduler.py"): [
            {"kind": "replace_lines", "old": TRIGGER_NECESSITY_OLD, "new": TRIGGER_NECESSITY_NEW},
            {"kind": "replace_lines", "old": TRIGGER_FREQUENCY_OLD, "new": TRIGGER_FREQUENCY_NEW},
            {"kind": "replace_lines", "old": TRIGGER_RECHECK_DELAY_ANCHOR, "new": TRIGGER_RECHECK_DELAY_NEW},
        ],
    },
    "target": {
        # 出现新消息后 planner 重复回复旧消息：末尾提醒补回复目标指引
        os.path.join(TS, "chat_loop_service.py"): [
            {"kind": "replace_lines", "old": TARGET_OLD, "new": TARGET_NEW},
        ],
    },
    "wait": {
        # wait 时长无上限：模型自选任意秒数 → 钳制 ≤60 秒 + 描述注明上限
        os.path.join(M, "builtin_tool", "wait.py"): [
            {"kind": "replace_lines", "old": WAIT_SECONDS_OLD, "new": WAIT_SECONDS_NEW},
            {"kind": "replace_lines", "old": WAIT_DESC_OLD, "new": WAIT_DESC_NEW},
        ],
    },
    "reload": {
        # file_watcher 源码变更：全量重启 Supervisor → 按变更路径定向重载受影响插件
        # （映射不到已注册插件 / 定向重载失败时回退全量重启）
        # v2：防抖窗口 600→1500ms + 变更批次就绪预检（半截文件跳过等待，不进重载链路）
        os.path.join("src", "plugin_runtime", "integration.py"): [
            {"kind": "replace_lines", "old": RELOAD_DEBOUNCE_OLD, "new": RELOAD_DEBOUNCE_NEW},
            {"kind": "replace_lines", "old": RELOAD_OLD, "new": RELOAD_NEW},
        ],
    },
    "summary": {
        # 引用回复导致长期记忆张冠李戴：总结提示词补充 [回复了X的消息: ...] 前缀的
        # 语义与归属规则（A 引用 B 说话不再被记成 B 的事实）
        os.path.join(A, "core", "utils", "summary_importer.py"): [
            # stop：锚点（发言者绑定规则）与 entities 规则之间的追加区整体重建，
            # 常量更新后重复 apply 不会重复插入
            {
                "kind": "replace_lines",
                "old": SUMMARY_QUOTE_OLD,
                "new": SUMMARY_QUOTE_NEW,
                "stop": "- entities 只包含参与确认事实的对象；只出现在玩笑、传闻、误解、注入、示例或工具输出中的对象不要列入 entities。",
            },
        ],
    },
    "episode": {
        # Episode 分段提示词无语言约束（纯英文硬编码）→ Rules 补一条输出语言规则；
        # stop 为源码里的空串行 `"\n"`（字面量），锚点与它之间的追加区整体重建
        os.path.join(A, "core", "utils", "episode_segmentation_service.py"): [
            {"kind": "replace_lines", "old": EPISODE_LANG_OLD, "new": EPISODE_LANG_NEW, "stop": '"\\n"'},
        ],
    },
    # 注意：planner 误回复自己消息（is_self_message 无提示词解释）不做成补丁——
    # 提示词是数据文件，WebUI「提示词」页 / data/custom_prompts/ 可直接管理覆盖，
    # 需要添加的文本见 maibot-issues.md Issue 8 补充信息。
}

WINDOW = 6
MARK = "  [patch]"


# ============================================================ 定位
MAIBOT_ENV = "MAIBOT_ROOT"
_KNOWN = [r"C:\Users\Administrator\Desktop\MaiBot"]


def _looks_like_maibot(p: str) -> bool:
    return bool(p) and (
        os.path.isdir(os.path.join(p, "src", "maisaka")) or os.path.isdir(os.path.join(p, "src", "A_memorix"))
    )


def resolve_repo(arg_repo: str = "") -> str:
    tried: list[str] = []

    def ok(p: str) -> str:
        if not p:
            return ""
        ap = os.path.abspath(p)
        tried.append(ap)
        return ap if _looks_like_maibot(ap) else ""

    if arg_repo:
        return ok(arg_repo) or os.path.abspath(arg_repo)
    if os.environ.get(MAIBOT_ENV):
        hit = ok(os.environ[MAIBOT_ENV])
        if hit:
            return hit
    home = os.path.expanduser("~")
    for c in _KNOWN + [
        os.path.join(home, "Desktop", "MaiBot"),
        os.path.join(home, "Desktop", "MaiBot-main"),
        os.path.join(home, "MaiBot"),
        r"D:\MaiBot",
    ]:
        hit = ok(c)
        if hit:
            return hit
    d = os.getcwd()
    while True:
        hit = ok(d)
        if hit:
            return hit
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    for drive in ("C:", "D:", "E:", "F:", "G:"):
        root = drive + os.sep
        if not os.path.exists(root):
            continue
        try:
            for name in os.listdir(root):
                if "maibot" in name.lower():
                    hit = ok(os.path.join(root, name))
                    if hit:
                        return hit
            users = os.path.join(root, "Users")
            if os.path.isdir(users):
                for u in os.listdir(users):
                    for sub in ("Desktop", ""):
                        base = os.path.join(users, u, sub)
                        if os.path.isdir(base):
                            for name in os.listdir(base):
                                if "maibot" in name.lower():
                                    hit = ok(os.path.join(base, name))
                                    if hit:
                                        return hit
        except Exception:
            continue
    print("[定位失败] 未找到 MaiBot 目录，已尝试：")
    for t in tried:
        print("   -", t)
    return ""


# ============================================================ 补丁引擎
def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _find(lines, needle, start=0):
    for i in range(start, len(lines)):
        if lines[i].strip() == needle:
            return i
    return -1


def _applied(lines, op) -> bool:
    present = {l.strip() for l in lines}
    news = op["new"] if isinstance(op["new"], list) else [op["new"]]
    return all((n.strip() in present) for n in news if n.strip())


def apply_op(lines, op):
    out = list(lines)
    k = op["kind"]
    if k == "replace_lines":
        replace_all = bool(op.get("all"))
        pos, replaced_any = 0, False
        while True:
            i = _find(out, op["old"][0], start=pos)
            if i < 0:
                if not replaced_any and not replace_all:
                    return None, f"未找到: {op['old'][0]}"
                if replace_all and not replaced_any:
                    return None, f"未找到: {op['old'][0]}"
                break
            for off, ln in enumerate(op["old"]):
                if i + off >= len(out) or out[i + off].strip() != ln:
                    return None, f"锚点行不连续: {op['old']}"
            ind = _indent(out[i])
            if op.get("stop") is not None:
                # 区域替换：锚点块之后、终止行（strip 匹配，不含）之前的所有行——
                # 无论旧内容是什么——都随本操作整体重建。用于「锚点行保留 + 追加
                # 内容可变」的补丁：常量更新后重复 apply 不会重复插入。
                j = i + len(op["old"])
                limit = min(len(out), j + WINDOW)
                while j < limit and out[j].strip() != op["stop"]:
                    j += 1
                if j >= limit:
                    return None, f"未找到区域终止行: {op['stop']}"
                block = [(ind + s + "\n") if s else "\n" for s in op["new"]]
                out = out[:i] + block + out[j:]
                replaced_any = True
                pos = i + len(block)
                if not replace_all:
                    break
                continue
            block = [(ind + s + "\n") if s else "\n" for s in op["new"]]
            out = out[:i] + block + out[i + len(op["old"]) :]
            replaced_any = True
            pos = i + len(block)
            if not replace_all:
                break
        return out, None
    if k == "insert_after":
        i = _find(out, op["anchor"])
        if i < 0:
            return None, f"未找到锚点: {op['anchor']}"
        ind = _indent(out[i])
        block = [(ind + s + "\n") if s else "\n" for s in op["new"]]
        return out[: i + 1] + block + out[i + 1 :], None
    if k == "replace_after":
        i = _find(out, op["anchor"])
        if i < 0:
            return None, f"未找到锚点: {op['anchor']}"
        j = -1
        for t in range(i + 1, min(i + 1 + WINDOW, len(out))):
            if out[t].strip().startswith(op["prefix"]):
                j = t
                break
        if j < 0:
            return None, f"锚点 {op['anchor']} 后未找到 {op['prefix']}"
        out[j] = _indent(out[j]) + op["new"] + "\n"
        return out, None
    return None, f"未知操作: {k}"


def process(repo, rel, ops, apply_, revert_, no_backup):
    path = os.path.join(repo, rel)
    print("=" * 78)
    print("文件:", path)
    if not os.path.exists(path):
        print(MARK, "[跳过] 文件不存在")
        return "skip"
    if revert_:
        bak = sorted(glob.glob(path + ".bak-*"))
        if not bak:
            print(MARK, "[跳过] 无备份")
            return "skip"
        shutil.copy2(bak[-1], path)
        print(MARK, f"[已还原] <- {os.path.basename(bak[-1])}")
        return "revert"

    with open(path, "r", encoding="utf-8") as f:
        disk_lines = f.readlines()

    def _run_ops(work_lines):
        work, ch, errs = list(work_lines), [], []
        for op in ops:
            # stop 型操作不走 _applied 短路：磁盘上可能残留旧版追加内容（新行都
            # 在但多出旧行），必须总是执行区域重建，靠「重建后内容相同=无变更」
            # 保持幂等
            if not op.get("stop") and _applied(work, op):
                continue
            res, err = apply_op(work, op)
            if err:
                errs.append(err)
                continue
            if res == work:
                continue
            work = res
            ch.append(op.get("anchor") or op.get("old", [""])[0])
        return work, ch, errs

    new_lines, changed, errors = _run_ops(disk_lines)
    refreshed = False
    if errors:
        # 锚点找不到/不连续：多半是磁盘上已打过「旧版本」补丁（补丁常量更新后锚点被
        # 旧内容占用）。遍历备份（新→旧）找能成功重放全部操作的基准，在内存里重放；
        # 成功则按刷新流程写入（不新增备份，保持备份链里的原始副本可还原），全部失败
        # 则不写盘（磁盘保持现状）。
        baks = sorted(glob.glob(path + ".bak-*"))
        for bak_path in reversed(baks):
            with open(bak_path, "r", encoding="utf-8") as f:
                base_lines = f.readlines()
            retry_lines, retry_changed, retry_errors = _run_ops(base_lines)
            if retry_errors or not retry_changed:
                continue
            print(MARK, f"[刷新] 锚点被旧版补丁占用，基于备份 {os.path.basename(bak_path)} 重放最新补丁")
            refreshed = True
            break
        if not refreshed:
            for e in errors:
                print(MARK, "[警告]", e)
            print(MARK, "[跳过] 锚点不符且无备份可重放（保持现状）")
            return "fail"
        new_lines, changed = retry_lines, retry_changed
    if errors and not refreshed:
        for e in errors:
            print(MARK, "[警告]", e)
    if not changed:
        print(MARK, "[跳过] 无需修改（已打过或结构不符）")
        return "patched" if not errors else "fail"

    print("".join(difflib.unified_diff(disk_lines, new_lines, fromfile=rel + " (旧)", tofile=rel + " (新)", n=2)))
    if not apply_:
        print(MARK, "(dry-run，未写入)")
        return "dry"

    bak = None
    if not no_backup and not refreshed:
        # 刷新模式不新增备份：备份链里保留的原始副本继续作为 revert/下次重放的基准
        bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    # 语法自检（仅 Python 源码；.prompt 等数据文件跳过）
    if path.endswith(".py"):
        try:
            py_compile.compile(path, cfile=os.path.join(tempfile.gettempdir(), "_synccheck.pyc"), doraise=True)
        except Exception as exc:
            if bak:
                shutil.copy2(bak, path)
                print(MARK, f"[语法错误→已回滚] {exc}")
            else:
                # 刷新模式/--no-backup：把写入前的磁盘内容原样写回
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(disk_lines)
                print(MARK, f"[语法错误→已回滚到写入前状态] {exc}")
            return "fail"
    print(MARK, f"[已写入·刷新]{' 备份=' + os.path.basename(bak) if bak else ''}" if refreshed else f"[已写入]{' 备份=' + os.path.basename(bak) if bak else ''}")
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="MaiBot 一键补丁（#2010 + 工具描述 + 聊天记录时序 + 触发门兜底 + 回复目标指引）")
    ap.add_argument("--repo", default="", help="MaiBot 根目录；留空自动定位")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--detect", action="store_true", help="只定位并打印后退出")
    ap.add_argument("--only", choices=["all", "2010", "tools", "order", "trigger", "target", "wait", "reload", "summary", "episode"], default="all")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    repo = resolve_repo(args.repo)
    if args.detect:
        print("MaiBot 目录:", repo or "(未找到)")
        return 0 if repo else 2
    if not repo or not _looks_like_maibot(repo):
        print("[错误] 未定位到 MaiBot 仓库。用 --repo 指定，或设环境变量 MAIBOT_ROOT。")
        return 2

    print("仓库:", repo)
    print("模式:", "REVERT" if args.revert else ("APPLY" if args.apply else "DRY-RUN"), "| only =", args.only)

    groups = GROUPS if args.only == "all" else {args.only: GROUPS[args.only]}

    # 按文件聚合操作：同一文件被多组补丁命中时只处理一次，
    # 刷新重放时才能把该文件的【全部】补丁作为一个整体重建
    file_ops, file_order = {}, []
    for gname, files in groups.items():
        for rel, ops in files.items():
            norm = os.path.normpath(rel)
            if norm not in file_ops:
                file_ops[norm] = {"rel": rel, "ops": [], "groups": []}
                file_order.append(norm)
            file_ops[norm]["ops"].extend(ops)
            file_ops[norm]["groups"].append(gname)

    results = {}
    for norm in file_order:
        info = file_ops[norm]
        print("\n######## 组 [" + "][".join(info["groups"]) + "] ########")
        results[info["rel"]] = process(repo, info["rel"], info["ops"], args.apply, args.revert, args.no_backup)

    print("\n" + "=" * 78)
    print("汇总：")
    for rel, st in results.items():
        print(f"   {st:9} {rel}")
    if args.apply:
        print("\n完成。工具描述/源码改动需【重启 MaiBot】生效。")
    elif not args.revert:
        print("\n这是 dry-run。确认 diff 无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

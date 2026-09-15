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

安全
----
* 默认 **dry-run**（只打印 diff），加 --apply 才写文件；
* 每个文件写前自动备份 `.bak-<时间戳>`；
* 写后自动 `py_compile` 语法自检，**若语法报错自动回滚该文件**；
* 幂等：已打过则跳过；
* --revert 从最近备份还原所有文件。

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
        lines = f.readlines()
    new_lines, changed, errors = list(lines), [], []
    for op in ops:
        if _applied(new_lines, op):
            continue
        res, err = apply_op(new_lines, op)
        if err:
            errors.append(err)
            continue
        new_lines = res
        changed.append(op.get("anchor") or op.get("old", [""])[0])
    if errors:
        for e in errors:
            print(MARK, "[警告]", e)
    if not changed:
        print(MARK, "[跳过] 无需修改（已打过或结构不符）")
        return "patched" if not errors else "fail"

    print("".join(difflib.unified_diff(lines, new_lines, fromfile=rel + " (旧)", tofile=rel + " (新)", n=2)))
    if not apply_:
        print(MARK, "(dry-run，未写入)")
        return "dry"

    bak = None
    if not no_backup:
        bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    # 语法自检
    try:
        py_compile.compile(path, cfile=os.path.join(tempfile.gettempdir(), "_synccheck.pyc"), doraise=True)
    except Exception as exc:
        if bak:
            shutil.copy2(bak, path)
            print(MARK, f"[语法错误→已回滚] {exc}")
        else:
            print(MARK, f"[语法错误] {exc}（未备份，未回滚）")
        return "fail"
    print(MARK, f"[已写入]{' 备份=' + os.path.basename(bak) if bak else ''}")
    return "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="MaiBot 一键补丁（#2010 + 工具描述 + 聊天记录时序 + 触发门兜底）")
    ap.add_argument("--repo", default="", help="MaiBot 根目录；留空自动定位")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--detect", action="store_true", help="只定位并打印后退出")
    ap.add_argument("--only", choices=["all", "2010", "tools", "order", "trigger"], default="all")
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
    results = {}
    for gname, files in groups.items():
        print(f"\n######## 组 [{gname}] ########")
        for rel, ops in files.items():
            results[rel] = process(repo, rel, ops, args.apply, args.revert, args.no_backup)

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

"""TaskUnderstanding — L1 rules + L2 LLM for structured turn interpretation.

Phase 2: output is a TaskIntent saved to DB; agent.py execution is NOT changed.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from pydantic import ValidationError

from ..compatibility.intent.adapter import (
    build_elements,
    compile_draft,
    parse_draft,
    parse_elements,
)
from .intent_compat import to_task_intent
from .intent_models import IntentSpec
from .intent_preprocessor import build_intent_context_hint
from .intent_llm_trace import IntentLLMTrace
from .intent_validation_trace import IntentValidationTrace
from .intent_validator import IntentValidator
from .models import EntityHint, TaskIntent

logger = logging.getLogger(__name__)

# ── L1 keyword sets ──────────────────────────────────────────────────

_CREATE_WORDS = (
    "写一篇", "写个", "写一", "创建一篇", "创作一篇", "生成一篇",
    "发一篇", "发布一篇", "创建", "新帖子", "新建",
    "运营", "策划", "来一篇", "做一篇", "搞个",
)
_REVISE_WORDS = ("修改", "改成", "改得", "改一下", "润色", "重写",
                  "调整", "更新", "替换", "补充", "追加", "加入", "加上",
                  "完善", "打磨", "增强", "优化", "提升", "改进", "充实",
                  "修正", "丰富", "精简", "整理")
_SCHEDULE_WORDS = ("定时", "安排", "后发布", "发布任务", "几点发", "什么时候发")
_CANCEL_WORDS = ("取消", "撤销")
_SEARCH_WORDS = ("搜索", "查找", "检索", "找一下", "看看有没有", "帮我找")
_ANALYZE_WORDS = ("分析", "总结", "归纳", "梳理")
_QUERY_WORDS = ("列出", "查看", "看一下", "怎么样了", "状态", "有哪些")

# ── L2 triggers ──────────────────────────────────────────────────────

_AMBIGUOUS_VERBS = {
    "优化", "提升", "改进", "整理", "重构", "润色", "丰富", "精简",
    "完善", "打磨", "增强", "充实", "修正",
}
_COMPOSITE_MARKERS = {"然后", "之后", "同时", "并且", "再", "接着", "完了", "最后"}
_CROSS_REF_PATTERNS = [
    re.compile(r"把.{0,20}(?:结果|分析|搜索|找到).{0,20}(?:加|放|合并|用)"),
    re.compile(r"(?:参考|借鉴|基于|依据).{0,10}(?:刚才|上面|之前|上次)"),
]

# ── LLM prompt (compact, ~150 tokens) ────────────────────────────────

_L2_SYSTEM = """Analyze the user message. Return JSON.

RELATION: NEW_TASK | CONTINUE_TASK | MODIFY_TASK | QUERY_TASK | CANCEL_TASK | DIRECT
CATEGORY: CREATE_CONTENT | IMPROVE_CONTENT | ANALYZE_COMMUNITY | PUBLISH_CONTENT | MANAGE_SCHEDULE | INTERACT | QUERY_INFO | COMPOSITE

RULES:
- "参考/借鉴+优化/改进/提升" → IMPROVE_CONTENT, MODIFY_TASK (target existing draft)
- "搜索+分析+生成" → COMPOSITE or CREATE_CONTENT
- "刚才/上次/之前" → match existing task
- "取消" → CANCEL_TASK
- Simple greeting/question → DIRECT, QUERY_INFO

Output valid JSON only:
{"relation":"...","goal":"...","goal_category":"...","target_task_hint":"..."}"""


# ── Phase 6.8.1: enhanced L2 prompt (IntentSpec structured output) ──────

_L2_SYSTEM_V2 = """You are an intent understanding module for a community operations assistant.

Analyze the user message and output a structured JSON object.

## Role
You are a semantic extraction module, not a conversational assistant.
Your only task is to map the user's natural language into the IntentSpec below.
Do not answer the user. Do not explain your reasoning. Do not create an
execution plan, steps, dependencies, ordering, tools, or planner instructions.

## Internal extraction procedure
Before emitting JSON, reason internally through these four passes:
1. Extract every action explicitly requested by the user.
2. Extract every conditional branch and its then/else behavior.
3. Extract every constraint, including approval and time requirements.
4. Map the extracted meaning into the IntentSpec schema without dropping
   actions, conditions, or constraints.
Only emit the final JSON object.

## Output Schema (strict)
{
  "mode": "SIMPLE" | "COMPOSITE" | "CONDITIONAL",
  "goal": "one-sentence summary of what the user wants",
  "actions": [
    {"action": "CREATE"|"UPDATE"|"DELETE"|"QUERY"|"SEARCH"|"ANALYZE"|"PUBLISH"|"UPDATE_OR_CREATE",
     "resource": "CONTENT"|"DRAFT"|"SCHEDULE"|"POST"|"TASK"|null}
  ],
  "conditions": [
    {"type": "IF_EXISTS"|"IF_NOT_EXISTS",
     "resource": "DRAFT"|"SCHEDULE"|"CONTENT"|null,
     "then_action": "UPDATE"|"PUBLISH"|null,
     "else_action": "CREATE"|null}
  ],
  "constraints": [
    {"type": "TIME"|"APPROVAL"|"USER_INPUT", "value": "..."}
  ],
  "target_hint": "reference to previous task/article (or null)",
  "confidence": 0.0-1.0
}

## Rules
0. Never return an empty actions array when the user asks to create, search,
   analyze, modify, optimize, publish, or query. Extract at least the most
   obvious requested action, and preserve every other explicitly requested
   action as well.
1. "创建/写/生成/发布/搞个/来一篇 文章/帖子/教程" → CREATE CONTENT
2. "修改/优化/完善/调整 文章/标题/内容" → UPDATE CONTENT (unless conditional)
3. "有则...无则.../如果...否则.../找到...就...找不到就..." → CONDITIONAL mode + conditions[]
4. "搜索...然后...分析...生成" (all for one goal) → COMPOSITE mode
5. "搜索/查找/找一下" alone → SEARCH POST
6. "发布/定时" → PUBLISH CONTENT (with TIME constraint if time given)
7. ANY mention of "确认/审核/审一下/看一下" before/after "发布/发" → APPROVAL constraint + PUBLISH action
   Examples: "发布前确认", "审核后发布", "让我看了再发", "先别发看过再发", "确认后五分钟发"
8. "刚才/上次/之前/第一篇" → target_hint
9. "取消/撤销" → DELETE SCHEDULE
10. "改发布时间/调整发布时间/改到X点发/别X点发改X点" → UPDATE SCHEDULE (not UPDATE CONTENT)
11. Simple greeting/info question → QUERY (mode=SIMPLE) with QUERY action
12. "查看草稿/列出草稿/没发的文章/未发布内容/找稿子" → QUERY DRAFT
13. Numbered steps (1. 2. 3.) → COMPOSITE mode
14. IMPORTANT: Always include at least one action in the actions array

## Examples

Input: "Complex operations: search popular articles, analyze why they are popular, if an Agent learning draft exists optimize it otherwise create it, ask for approval before publishing, then publish five minutes later."
Output: {"mode":"CONDITIONAL","goal":"Operate an Agent learning topic","actions":[{"action":"SEARCH","resource":"POST"},{"action":"ANALYZE","resource":"POST"},{"action":"UPDATE_OR_CREATE","resource":"DRAFT"},{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[{"type":"IF_EXISTS","resource":"DRAFT","then_action":"UPDATE","else_action":"CREATE"}],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"},{"type":"TIME","value":"5 minutes later"}],"target_hint":"Agent learning draft","confidence":0.95}

Input: "Write a Java article, show it to me first, and publish it after I approve."
Output: {"mode":"COMPOSITE","goal":"Write and publish a Java article after approval","actions":[{"action":"CREATE","resource":"CONTENT"},{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"}],"target_hint":null,"confidence":0.95}

Input: "If a previous Java article exists, optimize it; otherwise rewrite it."
Output: {"mode":"CONDITIONAL","goal":"Update or rewrite a Java article","actions":[{"action":"UPDATE_OR_CREATE","resource":"DRAFT"}],"conditions":[{"type":"IF_EXISTS","resource":"DRAFT","then_action":"UPDATE","else_action":"CREATE"}],"constraints":[],"target_hint":"Java article","confidence":0.95}

Input: "写一篇Java文章"
Output: {"mode":"SIMPLE","goal":"写Java文章","actions":[{"action":"CREATE","resource":"CONTENT"}],"conditions":[],"constraints":[],"target_hint":null,"confidence":0.95}

Input: "查看我的草稿"
Output: {"mode":"SIMPLE","goal":"查看草稿","actions":[{"action":"QUERY","resource":"DRAFT"}],"conditions":[],"constraints":[],"target_hint":null,"confidence":0.9}

Input: "把发布时间改成晚上9点"
Output: {"mode":"SIMPLE","goal":"修改发布时间","actions":[{"action":"UPDATE","resource":"SCHEDULE"}],"conditions":[],"constraints":[{"type":"TIME","value":"晚上9点"}],"target_hint":null,"confidence":0.9}

Input: "如果有旧文章就优化，没有就创建"
Output: {"mode":"CONDITIONAL","goal":"优化或创建文章","actions":[{"action":"UPDATE_OR_CREATE","resource":"CONTENT"}],"conditions":[{"type":"IF_EXISTS","resource":"DRAFT","then_action":"UPDATE","else_action":"CREATE"}],"constraints":[],"target_hint":null,"confidence":0.9}

Input: "搜索热门文章然后写一篇Java总结"
Output: {"mode":"COMPOSITE","goal":"搜索热门并写Java总结","actions":[{"action":"SEARCH","resource":"POST"},{"action":"CREATE","resource":"CONTENT"}],"conditions":[],"constraints":[],"target_hint":null,"confidence":0.9}

Input: "发布之前让我确认一下"
Output: {"mode":"SIMPLE","goal":"发布前确认","actions":[{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"}],"target_hint":null,"confidence":0.9}

Input: "先别直接发，我看过以后再发布"
Output: {"mode":"SIMPLE","goal":"审核后发布","actions":[{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"}],"target_hint":null,"confidence":0.9}

Input: "写好先给我看看，我同意以后五分钟发"
Output: {"mode":"SIMPLE","goal":"确认后定时发布","actions":[{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"},{"type":"TIME","value":"五分钟后"}],"target_hint":null,"confidence":0.9}

Input: "等我确认之后再定时发布"
Output: {"mode":"SIMPLE","goal":"确认后发布","actions":[{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"}],"target_hint":null,"confidence":0.9}

## CRITICAL rules
- NEVER return empty actions array — at minimum include the most obvious action
- NEVER drop user's explicitly mentioned operations
- Conditional branches (有则/无则) MUST use UPDATE_OR_CREATE, not just UPDATE or CREATE alone
- Approval requests (确认/审核/看一下) MUST include APPROVAL constraint, not just PUBLISH
- Time specifications (明天/晚上/几点/分钟后) MUST include TIME constraint
- Unclear requests: make your best guess rather than returning empty

Output valid JSON only. No markdown, no explanation."""


# ── Phase 6.8.1 Stage D-A: IntentDraft prompt (simpler, free-form) ─────

_L2_DRAFT_SYSTEM = """You are an intent understanding module. Describe what the user wants in simple free-form text.

## Output Schema
{
  "goal": "one-sentence summary",
  "actions": ["what the user wants to do, in plain language"],
  "conditions": ["any conditional logic the user mentioned"],
  "constraints": ["any time or approval requirements"],
  "target_hint": "reference to previous task/article or null",
  "confidence": 0.9
}

## CRITICAL rules
- NEVER return empty actions array — at minimum include the most obvious action
- List EVERY operation the user explicitly mentioned
- Use simple words: "search for X", "create content about Y", "publish", "update the draft"
- If user says "有X就改没有就建" → put that in conditions as "if draft exists update else create"
- If user says "发布前确认" → put in constraints as "approve before publish"
- If user says "明天9点" → put in constraints as "tomorrow 9am"
- If user says "搜索X然后写Y" → actions: ["search for X", "create content about Y"]
- The output is free-form text fields — be descriptive and complete

## Examples

User: "帮我运营一个Agent学习专题：先搜索热门内容并分析，如果之前有Agent草稿就优化没有就创建，发布前确认，确认后五分钟发布"

Output:
{
  "goal": "运营Agent学习专题",
  "actions": ["search for popular Agent content", "analyze trends", "create or update Agent learning draft"],
  "conditions": ["if Agent draft exists update it else create new one"],
  "constraints": ["approve before publish", "publish 5 minutes after approval"],
  "target_hint": "Agent学习草稿",
  "confidence": 0.9
}

User: "写一篇Java文章"
Output: {"goal":"写Java文章","actions":["create content about Java"],"conditions":[],"constraints":[],"target_hint":null,"confidence":0.95}

User: "把发布时间改成晚上9点"
Output: {"goal":"修改发布时间","actions":["update schedule time"],"conditions":[],"constraints":["9pm tonight"],"target_hint":null,"confidence":0.9}

User: "如果有旧文章就优化，没有就创建"
Output: {"goal":"优化或创建文章","actions":["create or update content"],"conditions":["if draft exists update else create"],"constraints":[],"target_hint":null,"confidence":0.9}

User: "搜索热门文章然后写一篇总结"
Output: {"goal":"搜索并写总结","actions":["search for popular posts","create content with summary"],"conditions":[],"constraints":[],"target_hint":null,"confidence":0.9}

User: "发布之前让我确认"
Output: {"goal":"发布前确认","actions":["publish content"],"conditions":[],"constraints":["approve before publish"],"target_hint":null,"confidence":0.95}

Output valid JSON only. No markdown, no explanation."""


# ── Phase 6.8.1 Stage D-B: IntentElements prompt ──────────────────────

_L2_ELEMENTS_SYSTEM = """You are an intent extraction module. Extract structured elements from the user message.

## Output Schema
{
  "goal": "one-sentence summary",
  "action_mentions": [
    {"verb": "<standard-verb>", "object": "<what-it-acts-on>"}
  ],
  "condition_mentions": [
    {"text": "<conditional logic description>"}
  ],
  "constraint_mentions": ["<time or approval requirement>"],
  "target_hint": null,
  "confidence": 0.9
}

## Standard verbs (use EXACTLY these)
- search | find | lookup
- create | write | generate | compose
- update | edit | modify | improve | optimize | revise
- publish | post | schedule | release
- delete | cancel | remove
- analyze | summarize | review
- query | view | check | list

## Object examples
- "community posts" | "article" | "draft" | "schedule" | "content"

## CRITICAL rules
- NEVER output empty action_mentions — at minimum extract one verb+object
- List EVERY action the user explicitly mentioned
- If user says "search X then write Y" → two action_mentions
- If user says "有X就修改没有就创建" → one action_mention + one condition_mention with text describing the branch
- If user says "发布前确认" → constraint_mentions: ["approval before publish"]
- If user says "明天9点" → constraint_mentions: ["tomorrow 9am"]
- Use ONLY the standard verbs listed above (lowercase)

## Examples

User: "搜索热门Java帖子，然后写一篇总结"
Output: {"goal":"搜索热门并写总结","action_mentions":[{"verb":"search","object":"community posts"},{"verb":"write","object":"article"}],"condition_mentions":[],"constraint_mentions":[],"target_hint":null,"confidence":0.95}

User: "如果有旧文章就修改，没有就创建"
Output: {"goal":"有则修改无则创建","action_mentions":[{"verb":"create","object":"article"}],"condition_mentions":[{"text":"if draft exists then update else create"}],"constraint_mentions":[],"target_hint":null,"confidence":0.9}

User: "发布之前让我确认一下"
Output: {"goal":"发布前确认","action_mentions":[{"verb":"publish","object":"content"}],"condition_mentions":[],"constraint_mentions":["approval before publish"],"target_hint":null,"confidence":0.95}

User: "把发布时间改成晚上9点"
Output: {"goal":"修改发布时间","action_mentions":[{"verb":"update","object":"schedule"}],"condition_mentions":[],"constraint_mentions":["9pm"],"target_hint":null,"confidence":0.9}

User: "帮我运营Agent专题：搜索热门内容分析，有Agent草稿就优化没有就创建，发布前确认，确认后五分钟发布"
Output: {"goal":"运营Agent专题","action_mentions":[{"verb":"search","object":"community posts"},{"verb":"analyze","object":"content"},{"verb":"create","object":"article"},{"verb":"publish","object":"content"}],"condition_mentions":[{"text":"if Agent draft exists then update else create"}],"constraint_mentions":["approval before publish","5 minutes after approval"],"target_hint":"Agent草稿","confidence":0.9}

Output valid JSON only. No markdown, no explanation."""


# ── Phase 6.8.1: LLM output normalization ──────────────────────────────

def _normalize_intent_data(data: dict) -> None:
    """Normalize LLM JSON output before Pydantic validation in-place.

    Handles common LLM quirks:
    - lowercase enum values ("create" → "CREATE")
    - null resource strings ("null" → None)
    - extra unexpected fields (stripped by Pydantic)
    """
    _VALID_ACTIONS = {"CREATE", "UPDATE", "DELETE", "QUERY", "SEARCH",
                      "ANALYZE", "PUBLISH", "UPDATE_OR_CREATE"}
    _VALID_RESOURCES = {"CONTENT", "DRAFT", "SCHEDULE", "POST", "TASK"}
    _VALID_MODES = {"SIMPLE", "COMPOSITE", "CONDITIONAL"}
    _VALID_COND_TYPES = {"IF_EXISTS", "IF_NOT_EXISTS"}

    # Normalize mode
    if "mode" in data and isinstance(data["mode"], str):
        upper = data["mode"].upper()
        if upper in _VALID_MODES:
            data["mode"] = upper

    # Normalize actions
    if "actions" in data and isinstance(data["actions"], list):
        for a in data["actions"]:
            if isinstance(a, dict):
                if "action" in a and isinstance(a["action"], str):
                    upper = a["action"].upper()
                    if upper in _VALID_ACTIONS:
                        a["action"] = upper
                if "resource" in a:
                    if a["resource"] is None:
                        pass  # None is fine
                    elif isinstance(a["resource"], str):
                        if a["resource"].lower() in ("null", "none", ""):
                            a["resource"] = None
                        else:
                            upper = a["resource"].upper()
                            if upper in _VALID_RESOURCES:
                                a["resource"] = upper

    # Normalize conditions
    if "conditions" in data and isinstance(data["conditions"], list):
        for c in data["conditions"]:
            if isinstance(c, dict):
                if "type" in c and isinstance(c["type"], str):
                    upper = c["type"].upper()
                    if upper in _VALID_COND_TYPES:
                        c["type"] = upper
                if "resource" in c:
                    if c["resource"] is None:
                        pass
                    elif isinstance(c["resource"], str):
                        if c["resource"].lower() in ("null", "none", ""):
                            c["resource"] = None
                        else:
                            upper = c["resource"].upper()
                            if upper in _VALID_RESOURCES:
                                c["resource"] = upper
                # Normalize then_action/else_action
                for key in ("then_action", "else_action"):
                    if key in c and isinstance(c[key], str):
                        upper = c[key].upper()
                        if upper in _VALID_ACTIONS:
                            c[key] = upper
                        elif c[key].lower() in ("null", "none", ""):
                            c[key] = None

    # Normalize constraints
    _VALID_CONSTRAINT_TYPES = {"TIME", "APPROVAL", "USER_INPUT"}
    if "constraints" in data and isinstance(data["constraints"], list):
        for ct in data["constraints"]:
            if isinstance(ct, dict) and "type" in ct and isinstance(ct["type"], str):
                upper = ct["type"].upper()
                if upper in _VALID_CONSTRAINT_TYPES:
                    ct["type"] = upper


# ── public API ───────────────────────────────────────────────────────

class TaskUnderstanding:
    """Two-layer intent understanding: deterministic L1 + LLM L2."""

    def __init__(self, llm: Any | None = None, model: str = "") -> None:
        self._llm = llm
        self._model = model
        self._last_routing_reason = ""
        self._repair_stats: dict[str, int] = {
            "attempts": 0, "successes": 0, "failures": 0, "fallbacks": 0,
        }
        self._validation_traces: list[IntentValidationTrace] = []
        self._last_repair_prompt = ""
        self._last_repair_response = ""
        self._last_direct_issues: list[dict[str, object]] = []
        self._llm_traces: list[IntentLLMTrace] = []

    @property
    def validation_traces(self) -> list[IntentValidationTrace]:
        """Validation and repair traces collected by this instance."""
        return list(self._validation_traces)

    @property
    def llm_traces(self) -> list[IntentLLMTrace]:
        """Raw Direct IntentSpec LLM response diagnostics."""
        return list(self._llm_traces)

    # ── main entry ───────────────────────────────────────────────

    async def understand(
        self,
        user_message: str,
        *,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> TaskIntent:
        """Produce a TaskIntent for one user turn.

        *existing_tasks* is a list of {task_id, goal, goal_category, …}
        dicts used by L2 to match "刚才那个" references.
        """
        # L1 fast path for clear, single-intent messages
        l1 = self._quick_intent(user_message, existing_tasks)

        # Phase 6.8.1: enhanced L2 via IntentSpec (score-based routing)
        # Also try v2 when original _needs_l2 triggers — v2 has better accuracy
        needs_l2_v2 = self._needs_l2_v2(user_message)
        needs_l2_original = self._needs_l2(user_message)

        if not needs_l2_v2 and needs_l2_original:
            self._last_routing_reason = "L2-v2:triggered-by-legacy-L2"

        if needs_l2_v2 or needs_l2_original:
            try:
                spec = await self._try_l2_v2(user_message, existing_tasks)
                if spec is not None:
                    intent = to_task_intent(spec)
                    intent.source = "L2"
                    intent.intent_spec = spec.model_dump(mode="json")
                    return intent
            except Exception:
                logger.debug("L2 v2 failed, falling through to original path")

        # Original L1+L2 logic (unchanged fallback)
        if l1 is not None and not needs_l2_original:
            l1.source = "L1"
            return l1

        # L2 deep path (original)
        try:
            l2 = await self._llm_understand(user_message, existing_tasks)
            if l2 is not None:
                l2.source = "L2"
                return l2
        except Exception:
            logger.debug("L2 understanding failed, falling back to L1")

        # Fallback
        if l1 is not None:
            l1.source = "L1"
            return l1
        return TaskIntent(relation="DIRECT", goal_category="QUERY_INFO",
                          goal=user_message[:200], source="L1", confidence=0.5)

    # ── L1: deterministic rules ──────────────────────────────────

    def _quick_intent(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> TaskIntent | None:
        """Translate keyword signals into a provisional TaskIntent."""
        norm = text.strip()
        lower = norm.lower()

        asks_create = any(w in lower for w in _CREATE_WORDS)
        asks_revise = any(w in lower for w in _REVISE_WORDS)
        asks_schedule = any(w in lower for w in _SCHEDULE_WORDS) or (
            "发布" in lower and self._has_future_time(norm)
        )
        asks_cancel = any(w in lower for w in _CANCEL_WORDS)
        asks_search = any(w in lower for w in _SEARCH_WORDS)
        asks_query = any(w in lower for w in _QUERY_WORDS)
        asks_improve = any(w in lower for w in ("优化", "提升", "改进", "丰富", "精简"))
        asks_analyze = any(w in lower for w in _ANALYZE_WORDS)

        # Conditional pattern: "有则...无则..." → NOT a direct modify
        if self._is_conditional(norm):
            # "有则修改，无则创建" → the "修改" here is a conditional branch,
            # not a direct user instruction to modify an existing resource.
            asks_revise = False
            if not asks_create:
                asks_create = True

        # Schedule-time disambiguation: "改发布时间/调整定时" → SCHEDULE not CONTENT
        _SCHEDULE_NOUNS = ("发布时间", "定时发布", "定时", "几点发", "什么时间发")
        if asks_revise and any(w in lower for w in _SCHEDULE_NOUNS):
            asks_revise = False
            asks_schedule = True

        # If nothing matched, return None (let caller fall back to DIRECT)
        if not any((asks_create, asks_revise, asks_schedule, asks_cancel,
                    asks_search, asks_query, asks_improve, asks_analyze)):
            # Check for community reference hints even without explicit markers
            if self._asks_for_community(norm):
                asks_search = True
            else:
                return None

        # ── translate to TaskIntent ──
        relation: str = "NEW_TASK"
        category: str = "QUERY_INFO"
        target_hint: str | None = None

        if asks_cancel:
            relation = "CANCEL_TASK"
            category = "MANAGE_SCHEDULE"
            target_hint = self._extract_hint(norm, existing_tasks)
        elif asks_create and asks_revise:
            relation = "MODIFY_TASK"
            category = "IMPROVE_CONTENT"
            target_hint = self._extract_hint(norm, existing_tasks)
        elif asks_revise or asks_improve:
            relation = "MODIFY_TASK"
            category = "IMPROVE_CONTENT"
            target_hint = self._extract_hint(norm, existing_tasks)
        elif asks_create and asks_search and asks_analyze:
            relation = "NEW_TASK"
            category = "CREATE_CONTENT"
        elif asks_create and asks_search:
            relation = "NEW_TASK"
            category = "CREATE_CONTENT"
        elif asks_create and asks_schedule:
            relation = "NEW_TASK"
            category = "CREATE_CONTENT"
        elif asks_create:
            relation = "NEW_TASK"
            category = "CREATE_CONTENT"
        elif asks_schedule:
            relation = "MODIFY_TASK" if existing_tasks else "NEW_TASK"
            category = "PUBLISH_CONTENT"
            target_hint = self._extract_hint(norm, existing_tasks)
        elif asks_search:
            relation = "NEW_TASK"
            category = "ANALYZE_COMMUNITY"
        elif asks_query:
            relation = "QUERY_TASK" if existing_tasks else "DIRECT"
            category = "QUERY_INFO"
            target_hint = self._extract_hint(norm, existing_tasks)

        return TaskIntent(
            relation=relation,  # type: ignore[arg-type]
            goal=norm[:200],
            goal_category=category,
            target_task_hint=target_hint,
            confidence=0.85,
            requirements=self._derive_requirements(
                asks_create, asks_revise, asks_schedule, asks_search,
                asks_analyze=asks_analyze,
            ),
            resource_requests=self._derive_resource_requests(
                asks_create=asks_create,
                asks_revise=asks_revise,
                asks_schedule=asks_schedule,
                asks_cancel=asks_cancel,
                asks_search=asks_search,
                asks_improve=asks_improve,
                relation=relation,
                target_hint=target_hint,
            ),
        )

    # ── L2: LLM deep understanding ────────────────────────────────

    async def _llm_understand(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> TaskIntent | None:
        if self._llm is None:
            return None

        tasks_ctx = ""
        if existing_tasks:
            lines = []
            for t in existing_tasks[:5]:
                tid = t.get("task_id", "")
                goal = t.get("goal", "")[:80]
                cat = t.get("goal_category", "")
                lines.append(f"- {tid}: [{cat}] {goal}")
            tasks_ctx = "\n".join(lines)

        user_prompt = f"""Existing tasks:
{tasks_ctx or '(none)'}

User: {text}"""

        resp = await self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _L2_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=300,
        )

        raw = resp.choices[0].message.content or "{}"
        return self._parse_llm_output(raw, text, existing_tasks)

    def _parse_llm_output(
        self,
        raw: str,
        fallback_text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> TaskIntent | None:
        # Strip markdown fences
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            return None

        hint: str | None = data.get("target_task_hint")
        target_id: str | None = data.get("target_task_id")

        # Resolve hint → task_id when possible
        if hint and not target_id and existing_tasks:
            target_id = self._resolve_hint_to_id(hint, existing_tasks)

        try:
            return TaskIntent(
                relation=data.get("relation", "NEW_TASK"),
                goal=data.get("goal", fallback_text[:200]),
                goal_category=data.get("goal_category", "QUERY_INFO"),
                target_task_id=target_id,
                target_task_hint=hint,
                requirements=data.get("requirements", []),
                constraints=data.get("constraints", []),
                confidence=float(data.get("confidence", 0.8)),
            )
        except ValidationError:
            return None

    # ── L1 helpers ────────────────────────────────────────────────

    @staticmethod
    def _needs_l2(text: str) -> bool:
        """Should we escalate to LLM?"""
        if any(w in text for w in _AMBIGUOUS_VERBS):
            return True
        # Count total occurrences of composite markers
        count = sum(text.count(m) for m in _COMPOSITE_MARKERS)
        if count >= 2:                     # "搜索…然后…之后…"
            return True
        if count >= 1 and len(text) > 40:  # long composite message
            return True
        for pat in _CROSS_REF_PATTERNS:
            if pat.search(text):
                return True
        return False

    # ── Phase 6.8.1: enhanced L2 routing + IntentSpec path ─────────

    def _needs_l2_v2(self, text: str) -> bool:
        """Score-based L2 trigger. score >= 2 → escalate to LLM.

        Stage D-B scoring:
          condition:  +3  (如果/否则/有则/无则/要是/假如)
          approval:   +3  (确认/审核 before/after 发布/发)
          multi-act:  +3  (然后/再/最后/同时/并且 — at least 2 markers)
          time-mut:   +2  (发布时间/定时/改到/改成/几点发/改时间)
          history:    +1  (刚才/上次/之前/刚刚/最近)
          long:       +1  (>100 chars)
        """
        score = 0
        reasons: list[str] = []

        # 条件表达: +3
        if re.search(r"如果|否则|有则|无则|要是|假如", text):
            score += 3
            reasons.append("conditional(+3)")

        # 审批信号: +3
        if re.search(r"(?:确认|审核|审一下|看一下|看了|审阅).*(?:发布|发|再发)|(?:发布|发).*(?:确认|审核|审|看)", text):
            score += 3
            reasons.append("approval(+3)")

        # 多步骤信号: +3 (at least 2 markers)
        count = sum(text.count(m) for m in ("然后", "再", "最后", "同时", "并且"))
        if count >= 2:
            score += 3
            reasons.append(f"multi-step({count},+3)")
        elif count >= 1:
            score += 1
            reasons.append(f"multi-step({count},+1)")

        # 时间变更: +2
        if re.search(r"发布时间|定时发布|改到.{0,5}发|改成.{0,5}发|几点发|什么时间发|改时间|延后|提前|晚一点发", text):
            score += 2
            reasons.append("time-mutation(+2)")

        # 长请求: +1
        if len(text) > 100:
            score += 1
            reasons.append("long(+1)")

        # 历史引用: +1
        if any(w in text for w in ("刚才", "上次", "之前", "刚刚", "最近")):
            score += 1
            reasons.append("history(+1)")

        result = score >= 2
        reason_str = f"score={score} " + " ".join(reasons) if reasons else f"score={score} (simple)"
        self._last_routing_reason = f"L2-v2:{reason_str}" if result else f"L1:{reason_str}"
        return result

    async def _try_l2_v2(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec | None:
        """Try L2 v2: primary path — Elements→Builder, fallback to direct IntentSpec."""
        # Stage D-B: Primary — IntentElements → Builder (structured, reliable)
        spec = await self._llm_understand_direct_v2(text, existing_tasks)
        if spec is not None:
            validator = IntentValidator()
            result = validator.validate(spec, text)
            trace = IntentValidationTrace(
                raw_intent_spec=spec.model_dump(mode="json"),
                validation_errors=(
                    self._last_direct_issues
                    + [issue.model_dump(mode="json") for issue in result.issues]
                ),
            )
            if result.is_valid:
                trace.final_result = spec.model_dump(mode="json")
                self._validation_traces.append(trace)
                return spec
            if result.needs_repair and self._llm is not None:
                self._repair_stats["attempts"] += 1
                trace.repair_triggered = True
                self._last_repair_prompt = ""
                self._last_repair_response = ""
                repaired = await self._llm_repair_spec(text, spec, result)
                trace.repair_prompt = self._last_repair_prompt
                trace.repair_response = self._last_repair_response
                if repaired is not None:
                    result2 = validator.validate(repaired, text)
                    if result2.is_valid:
                        self._repair_stats["successes"] += 1
                        trace.final_result = repaired.model_dump(mode="json")
                        self._validation_traces.append(trace)
                        return repaired
                    trace.final_result = repaired.model_dump(mode="json")
                self._repair_stats["failures"] += 1
                self._validation_traces.append(trace)
            else:
                self._validation_traces.append(trace)
        elif self._last_direct_issues:
            self._validation_traces.append(IntentValidationTrace(
                raw_intent_spec={},
                validation_errors=self._last_direct_issues,
                final_result=None,
            ))

        self._repair_stats["fallbacks"] += 1
        return None

    async def _llm_understand_direct_v2(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec | None:
        """LLM deep understanding → IntentSpec via Pydantic validation."""
        if self._llm is None:
            return None

        self._last_direct_issues = []
        context_hint = build_intent_context_hint(text)
        context_json = json.dumps(
            context_hint.model_dump(mode="json"),
            ensure_ascii=False,
        )
        max_tokens = self._intent_llm_budget(context_hint)

        tasks_ctx = ""
        if existing_tasks:
            lines = []
            for t in existing_tasks[:5]:
                tid = t.get("task_id", "")
                goal = t.get("goal", "")[:80]
                lines.append(f"- {tid}: {goal}")
            tasks_ctx = "\n".join(lines)

        async def call(user_content: str) -> str:
            started = time.perf_counter()
            request: dict[str, Any] = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _L2_SYSTEM_V2},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
                "max_tokens": max_tokens,
            }
            # DeepSeek's reasoning tokens share the completion budget. Intent
            # extraction only needs the structured result, so disable thinking
            # where the OpenAI-compatible endpoint supports this option.
            if "deepseek" in self._model.lower() and "reasoner" not in self._model.lower():
                request["extra_body"] = {"thinking": {"type": "disabled"}}
            response = await self._llm.chat.completions.create(**request)
            latency_ms = (time.perf_counter() - started) * 1000.0
            choice = response.choices[0]
            raw_content = choice.message.content or ""
            usage = getattr(response, "usage", None)
            if usage is None:
                usage_data: dict[str, Any] = {}
            elif hasattr(usage, "model_dump"):
                usage_data = usage.model_dump(mode="json")
            elif hasattr(usage, "dict"):
                usage_data = usage.dict()
            elif isinstance(usage, dict):
                usage_data = dict(usage)
            else:
                usage_data = {"value": str(usage)}

            trace = IntentLLMTrace(
                raw_response_content=raw_content,
                finish_reason=getattr(choice, "finish_reason", None),
                model=self._model,
                usage=usage_data,
                latency_ms=latency_ms,
                parse_status="EMPTY_RESPONSE" if not raw_content.strip() else "NOT_PARSED",
            )
            self._llm_traces.append(trace)
            return raw_content

        user_content = (
            f"Existing tasks:\n{tasks_ctx or '(none)'}\n\n"
            f"Intent context hint:\n{context_json}\n\nUser: {text}"
        )
        raw = await call(user_content)
        if not raw.strip():
            self._last_direct_issues.append({
                "type": "EMPTY_LLM_RESPONSE",
                "message": "Direct IntentSpec LLM returned an empty response",
                "expected_fields": ["IntentSpec"],
                "suggestion": ["RETRY_WITH_COMPRESSED_CONTEXT"],
            })
            retry_content = (
                "Retry the same extraction using this compressed structural context. "
                "Return the complete IntentSpec JSON only.\n\n"
                f"Compressed intent context:\n{context_json}\n\n"
                f"User message:\n{text}"
            )
            raw = await call(retry_content)
            if not raw.strip():
                return None
        parsed = self._parse_intent_spec(raw)
        trace = self._llm_traces[-1]
        if trace.finish_reason == "length":
            trace.parse_status = "TRUNCATED_RESPONSE"
            self._last_direct_issues.append({
                "type": "TRUNCATED_RESPONSE",
                "message": "Direct IntentSpec LLM response reached the token limit",
                "expected_fields": ["complete IntentSpec JSON"],
                "suggestion": ["INCREASE_INTENT_LLM_BUDGET"],
            })
        elif parsed is not None:
            trace.parse_status = "PARSED"
        else:
            try:
                data = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
            except json.JSONDecodeError:
                trace.parse_status = "INVALID_JSON"
                issue_type = "INVALID_JSON"
            else:
                trace.parse_status = "SCHEMA_VALIDATION_FAILED" if isinstance(data, dict) else "INVALID_JSON"
                issue_type = trace.parse_status
            self._last_direct_issues.append({
                "type": issue_type,
                "message": "Direct IntentSpec response could not be parsed into the schema",
                "expected_fields": ["IntentSpec"],
                "suggestion": ["RETURN_COMPLETE_INTENTSPEC_JSON"],
            })
        return parsed

    @staticmethod
    def _intent_llm_budget(context_hint: Any) -> int:
        """Choose an output budget from surface complexity signals only."""
        if context_hint.has_condition:
            return 2000 if context_hint.has_multiple_actions else 1500
        if context_hint.has_multiple_actions:
            return 2000 if (
                context_hint.has_approval or context_hint.has_time_constraint
            ) else 1200
        return 600

    # ── Phase 6.8.1 Stage D-B: Elements-based understanding ──────────

    async def _llm_understand_v2(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec | None:
        """Backward-compatible alias for the Direct IntentSpec call."""
        return await self._llm_understand_direct_v2(text, existing_tasks)

    async def _llm_understand_elements(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec | None:
        """LLM → IntentElements → IntentSpecBuilder → IntentSpec."""
        if self._llm is None:
            return None

        tasks_ctx = ""
        if existing_tasks:
            lines = [f"- {t.get('task_id', '')}: {t.get('goal', '')[:80]}"
                     for t in existing_tasks[:5]]
            tasks_ctx = "\n".join(lines)

        resp = await self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _L2_ELEMENTS_SYSTEM},
                {"role": "user", "content": f"Existing tasks:\n{tasks_ctx or '(none)'}\n\nUser: {text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=500,
        )

        raw = resp.choices[0].message.content or "{}"
        elements = parse_elements(raw)
        if elements is None:
            return None

        return build_elements(elements)

    @staticmethod
    def _parse_elements(raw: str) -> Any | None:
        """Parse LLM JSON output into IntentElements."""
        return parse_elements(raw)

    # ── Phase 6.8.1 Stage D-A: Draft-based understanding ──────────────

    async def _llm_understand_draft(
        self,
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> IntentSpec | None:
        """LLM → IntentDraft (free-form) → IntentCompiler → IntentSpec."""
        if self._llm is None:
            return None

        tasks_ctx = ""
        if existing_tasks:
            lines = [f"- {t.get('task_id', '')}: {t.get('goal', '')[:80]}"
                     for t in existing_tasks[:5]]
            tasks_ctx = "\n".join(lines)

        resp = await self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _L2_DRAFT_SYSTEM},
                {"role": "user", "content": f"Existing tasks:\n{tasks_ctx or '(none)'}\n\nUser: {text}"},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=500,
        )

        raw = resp.choices[0].message.content or "{}"
        draft = parse_draft(raw)
        if draft is None:
            return None

        return compile_draft(draft)

    @staticmethod
    def _parse_draft(raw: str) -> Any | None:
        """Parse LLM JSON output into IntentDraft."""
        return parse_draft(raw)

    async def _llm_repair_spec(
        self, text: str, spec: IntentSpec, validation: object
    ) -> IntentSpec | None:
        """Targeted repair: fix specific validator issues without re-understanding."""
        if self._llm is None:
            return None

        import json as _json
        issues = getattr(validation, "issues", [])
        if issues:
            errors_text = _json.dumps(
                [issue.model_dump(mode="json") for issue in issues],
                indent=2,
                ensure_ascii=False,
            )
        else:
            errors_text = "\n".join(
                f"- {e}" for e in (validation.errors + validation.suggested_fixes)
            )
        spec_json = _json.dumps(spec.model_dump(mode="json"), indent=2, ensure_ascii=False)
        repair_prompt = f"""Fix the following IntentSpec based on validator feedback.

## Original user message
{text}

## Current IntentSpec (has issues)
{spec_json}

## Validator issues (the only fields that may be changed)
{errors_text}

## Output schema
{{"mode":"SIMPLE|COMPOSITE|CONDITIONAL","goal":"...","actions":[{{"action":"CREATE|UPDATE|DELETE|QUERY|SEARCH|ANALYZE|PUBLISH|UPDATE_OR_CREATE","resource":"CONTENT|DRAFT|SCHEDULE|POST|TASK|null"}}],"conditions":[{{"type":"IF_EXISTS|IF_NOT_EXISTS","resource":"...","then_action":"...","else_action":"..."}}],"constraints":[{{"type":"TIME|APPROVAL|USER_INPUT","value":"..."}}],"target_hint":null,"confidence":0.9}}

## Targeted repair rules
1. Fix ONLY the issues explicitly reported by the validator
2. Keep all existing actions unchanged unless the validator explicitly reports empty actions
3. Keep goal, target_hint, confidence, and valid constraints unchanged
4. If actions is empty, add only the minimum obvious action required by the validator
5. If CONDITIONAL mode but no conditions, add the minimum IF_EXISTS condition
6. If UPDATE_OR_CREATE without conditions, add the minimum IF_EXISTS condition
7. If the validator reports missing approval, add only APPROVAL with value BEFORE_PUBLISH
8. If the validator reports a schedule resource mismatch, change only that resource to SCHEDULE

Output valid JSON only. No markdown."""

        resp = await self._llm.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": repair_prompt},
                {"role": "user", "content": "Output the fixed JSON only."},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content or "{}"
        self._last_repair_prompt = repair_prompt
        self._last_repair_response = raw
        return self._parse_intent_spec(raw)

    @staticmethod
    def _parse_intent_spec(raw: str) -> IntentSpec | None:
        """Parse LLM JSON output into IntentSpec via Pydantic validation.

        Normalizes LLM output (case, extra fields) before validation.
        """
        import json as _json

        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = _json.loads(cleaned)
        except _json.JSONDecodeError:
            logger.debug("IntentSpec parse: invalid JSON")
            return None

        if not isinstance(data, dict):
            return None

        # Normalize LLM output before Pydantic validation
        _normalize_intent_data(data)

        try:
            return IntentSpec.model_validate(data)
        except Exception as e:
            logger.debug(f"IntentSpec validation failed: {e}")
            return None

    # ── Phase 6.8.1 helpers ──────────────────────────────────────────

    @staticmethod
    def _has_future_time(text: str) -> bool:
        return bool(re.search(
            r"[零〇一二两三四五六七八九十百\d]+\s*(?:分钟|分|小时|个小时|天)\s*(?:之后|后)|"
            r"明天|后天|今天(?:上午|早上|下午|晚上|今晚)|下周|"
            r"20\d{2}[-年/]\d{1,2}[-月/]\d{1,2}日?|"
            r"今晚|明早|明晚|晚上\s*\d+|早上\s*\d+|下午\s*\d+",
            text,
        ))

    @staticmethod
    def _is_conditional(text: str) -> bool:
        """Detect conditional patterns like '有则修改，无则创建'."""
        return bool(re.search(r"有则|无则|如果.{0,10}(?:有|没有|存在|不存在)", text))

    @staticmethod
    def _asks_for_community(text: str) -> bool:
        lower = text.lower()
        if any(m in lower for m in ("不要参考社区", "无需参考社区", "不参考社区")):
            return False
        return "社区" in lower and any(m in lower for m in ("结合", "参考", "基于", "依据"))

    @staticmethod
    def _extract_hint(
        text: str,
        existing_tasks: list[dict[str, str]] | None = None,
    ) -> str | None:
        """Extract a task reference hint from the user message."""
        # Temporal hints
        if any(w in text for w in ("刚才", "上次", "之前", "刚刚", "最近")):
            if existing_tasks:
                return existing_tasks[0].get("task_id", "")  # most recent
            return "recent"
        # Label hints: "Java文章", "那个Python的"
        for kw in ("文章", "帖子", "任务", "定时", "发布"):
            idx = text.find(kw)
            if idx >= 0:
                prefix = text[max(0, idx - 15):idx]
                if len(prefix) >= 2:
                    return prefix.strip()
        return None

    @staticmethod
    def _resolve_hint_to_id(
        hint: str,
        tasks: list[dict[str, str]],
    ) -> str | None:
        """Simple substring match of hint against task goals."""
        for t in tasks:
            goal = t.get("goal", "")
            if hint in goal or (t.get("goal_summary") and hint in str(t.get("goal_summary", ""))):
                return t.get("task_id")
        return tasks[0].get("task_id") if tasks else None

    @staticmethod
    def _derive_requirements(
        asks_create: bool,
        asks_revise: bool,
        asks_schedule: bool,
        asks_search: bool,
        asks_analyze: bool = False,
    ) -> list[dict[str, Any]]:
        reqs: list[dict[str, Any]] = []
        if asks_search:
            reqs.append({"type": "SEARCH"})
        if asks_analyze:
            reqs.append({"type": "ANALYZE"})
        if asks_create:
            reqs.append({"type": "CREATE"})
        elif asks_revise:
            reqs.append({"type": "IMPROVE"})
        if asks_schedule:
            reqs.append({"type": "PUBLISH"})
        return reqs

    @staticmethod
    def _derive_resource_requests(
        *,
        asks_create: bool,
        asks_revise: bool,
        asks_schedule: bool,
        asks_cancel: bool,
        asks_search: bool,
        asks_improve: bool,
        relation: str,
        target_hint: str | None,
    ) -> list[dict[str, str]]:
        """Derive resource-level operations from L1 signals + relation.

        Phase 5.6: the key rule is that CREATE operations are used when
        relation==NEW_TASK (user wants new resources), UPDATE when
        relation==MODIFY_TASK (user wants to change existing resources).
        """
        reqs: list[dict[str, str]] = []

        if asks_create:
            reqs.append({"operation": "CREATE", "resource_type": "CONTENT_DRAFT"})
        elif asks_revise or asks_improve:
            reqs.append({
                "operation": "UPDATE",
                "resource_type": "CONTENT_DRAFT",
                "hint": target_hint or "",
            })

        if asks_schedule:
            if relation == "NEW_TASK":
                reqs.append({"operation": "CREATE", "resource_type": "SCHEDULE"})
            else:
                reqs.append({
                    "operation": "UPDATE",
                    "resource_type": "SCHEDULE",
                    "hint": target_hint or "",
                })

        if asks_cancel:
            reqs.append({
                "operation": "DELETE",
                "resource_type": "SCHEDULE",
                "hint": target_hint or "",
            })

        if asks_search:
            reqs.append({"operation": "QUERY", "resource_type": "POST"})

        return reqs

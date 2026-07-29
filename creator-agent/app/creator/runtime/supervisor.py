from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorTaskKind,
)
from app.creator.runtime.models import (
    AgentCapability,
    ArtifactKind,
    ArtifactRef,
    BudgetUsage,
    CreatorGraphState,
    HumanDecisionRequest,
    PlanSnapshot,
    PlanStep,
    PlanStepStatus,
    ProgressEntry,
    RuntimeFailure,
    StepExecution,
    SupervisorAction,
    SupervisorDecision,
    utc_now,
)
from app.creator.runtime.registry import AgentRegistryError, CreatorAgentRegistry


class SupervisorPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_plan_steps: int = Field(default=12, ge=1, le=64)
    critic_acceptance_score: float = Field(default=0.70, ge=0.0, le=1.0)


@dataclass(frozen=True)
class SupervisorTurn:
    decision: SupervisorDecision
    plan: PlanSnapshot | None = None
    plan_is_new: bool = False
    usage_delta: BudgetUsage = BudgetUsage(supervisor_turns=1)
    progress: tuple[ProgressEntry, ...] = ()


class CreatorSupervisorAgent:
    """Deterministic control plane for dynamic specialist selection and replanning."""

    name = "CreatorSupervisorAgent"

    def __init__(
        self,
        registry: CreatorAgentRegistry,
        *,
        policy: SupervisorPolicy | None = None,
    ):
        self._registry = registry
        self._policy = policy or SupervisorPolicy()

    def decide(self, state: CreatorGraphState) -> SupervisorTurn:
        budget_failure = self._budget_failure(state)
        if budget_failure is not None:
            return self._failure_turn(budget_failure)

        current_plan = state["plan"]
        if current_plan is not None:
            failed = self._failed_steps(current_plan, state["executions"])
            if failed:
                return self._recover_or_fail(state, current_plan, failed[0])

            ready = self._ready_steps(current_plan, state["executions"])
            if ready:
                return self._dispatch_turn(state, current_plan, ready)

            if not self._plan_completed(current_plan, state["executions"]):
                return self._failure_turn(
                    RuntimeFailure(
                        code="PLAN_DEADLOCK",
                        message=_text(
                            state,
                            (
                                f"计划版本 {current_plan.revision} 没有可执行步骤，"
                                "且无法完成"
                            ),
                            (
                                f"Plan revision {current_plan.revision} has no ready "
                                "steps and cannot complete"
                            ),
                        ),
                    )
                )

        return self._advance_workflow(state)

    def _advance_workflow(self, state: CreatorGraphState) -> SupervisorTurn:
        kind = state["identity"].task_kind
        if kind == CreatorTaskKind.CREATE_CONTENT:
            return self._advance_create_content(state)
        if kind == CreatorTaskKind.BUILD_STRATEGY:
            return self._advance_strategy(state)
        if kind == CreatorTaskKind.ANALYZE_CONTENT:
            return self._advance_analysis(state)
        if kind == CreatorTaskKind.RESEARCH_TOPIC:
            return self._advance_research(state)
        if kind == CreatorTaskKind.IMPROVE_DRAFT:
            return self._advance_improvement(state)
        return self._failure_turn(
            RuntimeFailure(
                code="TASK_KIND_UNSUPPORTED",
                message=_text(
                    state,
                    f"不支持的创作者任务类型：{kind.value}",
                    f"Unsupported creator task kind: {kind.value}",
                ),
            )
        )

    def _advance_create_content(self, state: CreatorGraphState) -> SupervisorTurn:
        missing_context = self._missing_kinds(
            state,
            (
                ArtifactKind.CREATOR_PROFILE,
                ArtifactKind.CONTENT_ANALYSIS,
                ArtifactKind.EVIDENCE_PACK,
            ),
        )
        topics = self._latest_artifact(state, ArtifactKind.TOPIC_OPTIONS)
        if missing_context or topics is None:
            steps = self._context_and_topic_steps(
                state,
                include_topics=True,
            )
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "并行收集创作者上下文，然后生成选题方案。",
                    "Collect creator context in parallel, then generate topics.",
                ),
                steps=steps,
            )

        if state["goal"].constraints.get("topic_revision_requested_from") == topics.id:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "根据人工反馈重新生成选题方案。",
                    "Regenerate topic options from human feedback.",
                ),
                steps=(
                    PlanStep(
                        id="revise-topics",
                        capability=AgentCapability.PLAN_TOPICS,
                        objective=_text(
                            state,
                            "根据创作者反馈修订选题方案。",
                            "Revise topic options from the creator feedback.",
                        ),
                        output_kind=ArtifactKind.TOPIC_OPTIONS,
                        input_kinds=(
                            ArtifactKind.CREATOR_PROFILE,
                            ArtifactKind.CONTENT_ANALYSIS,
                            ArtifactKind.EVIDENCE_PACK,
                        ),
                    ),
                ),
            )

        if not self._topic_is_approved(state):
            return self._human_turn(
                kind=CreatorDecisionKind.TOPIC_SELECTION,
                prompt=_text(
                    state,
                    "生成大纲前，请先选择一个选题方向。",
                    "Select one topic direction before outline generation.",
                ),
                source=topics,
                allowed_actions=(
                    CreatorDecisionAction.SELECT,
                    CreatorDecisionAction.EDIT,
                    CreatorDecisionAction.REQUEST_CHANGES,
                ),
            )

        outline = self._latest_artifact(state, ArtifactKind.CONTENT_OUTLINE)
        if outline is None:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "根据选定主题生成文章大纲。",
                    "Build an outline from the selected topic.",
                ),
                steps=(
                    PlanStep(
                        id="build-outline",
                        capability=AgentCapability.BUILD_OUTLINE,
                        objective=_text(
                            state,
                            "将选定主题展开为内容大纲。",
                            "Develop the selected topic into a content outline.",
                        ),
                        output_kind=ArtifactKind.CONTENT_OUTLINE,
                        input_kinds=(
                            ArtifactKind.TOPIC_OPTIONS,
                            ArtifactKind.EVIDENCE_PACK,
                        ),
                    ),
                ),
            )

        if (
            state["goal"].constraints.get("outline_revision_requested_from")
            == outline.id
        ):
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "根据人工反馈修订文章大纲。",
                    "Revise the outline from human feedback.",
                ),
                steps=(
                    PlanStep(
                        id="revise-outline",
                        capability=AgentCapability.BUILD_OUTLINE,
                        objective=_text(
                            state,
                            "根据创作者反馈修订内容大纲。",
                            "Revise the content outline from creator feedback.",
                        ),
                        output_kind=ArtifactKind.CONTENT_OUTLINE,
                        input_kinds=(
                            ArtifactKind.TOPIC_OPTIONS,
                            ArtifactKind.EVIDENCE_PACK,
                        ),
                    ),
                ),
            )

        if not self._outline_is_approved(state):
            return self._human_turn(
                kind=CreatorDecisionKind.OUTLINE_APPROVAL,
                prompt=_text(
                    state,
                    "开始写作前，请批准或修订文章大纲。",
                    "Approve or revise the outline before drafting.",
                ),
                source=outline,
                allowed_actions=(
                    CreatorDecisionAction.APPROVE,
                    CreatorDecisionAction.EDIT,
                    CreatorDecisionAction.REQUEST_CHANGES,
                ),
            )

        return self._advance_draft_review(state)

    def _advance_strategy(self, state: CreatorGraphState) -> SupervisorTurn:
        topics = self._latest_artifact(state, ArtifactKind.TOPIC_OPTIONS)
        if topics is None:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "根据创作者画像、历史内容分析和研究结果生成选题策略。",
                    "Build strategy from profile, history analysis, and research.",
                ),
                steps=self._context_and_topic_steps(state, include_topics=True),
            )
        return self._finish_turn(
            topics,
            _text(state, "选题策略已生成。", "Topic strategy is ready."),
        )

    def _advance_analysis(self, state: CreatorGraphState) -> SupervisorTurn:
        analysis = self._latest_artifact(state, ArtifactKind.CONTENT_ANALYSIS)
        profile = self._latest_artifact(state, ArtifactKind.CREATOR_PROFILE)
        if analysis is None or profile is None:
            steps = []
            if profile is None:
                steps.append(
                    PlanStep(
                        id="load-memory",
                        capability=AgentCapability.LOAD_CREATOR_MEMORY,
                        objective=_text(
                            state,
                            "加载创作者画像与偏好信号。",
                            "Load the creator profile and preference signals.",
                        ),
                        output_kind=ArtifactKind.CREATOR_PROFILE,
                    )
                )
            if analysis is None:
                steps.append(
                    PlanStep(
                        id="analyze-content",
                        capability=AgentCapability.ANALYZE_CONTENT,
                        objective=_text(
                            state,
                            "分析历史内容及互动表现。",
                            "Analyze historical content and engagement.",
                        ),
                        output_kind=ArtifactKind.CONTENT_ANALYSIS,
                    )
                )
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "并行收集创作者画像和内容分析。",
                    "Collect profile and content analysis in parallel.",
                ),
                steps=tuple(steps),
            )
        return self._finish_turn(
            analysis,
            _text(state, "内容分析已完成。", "Content analysis is ready."),
        )

    def _advance_research(self, state: CreatorGraphState) -> SupervisorTurn:
        evidence = self._latest_artifact(state, ArtifactKind.EVIDENCE_PACK)
        if evidence is None:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "研究任务指定的主题。",
                    "Research the requested topic.",
                ),
                steps=(
                    PlanStep(
                        id="research-topic",
                        capability=AgentCapability.RESEARCH_TOPIC,
                        objective=_text(
                            state,
                            "收集证据并明确尚未解决的检索缺口。",
                            "Collect evidence and expose unresolved search gaps.",
                        ),
                        output_kind=ArtifactKind.EVIDENCE_PACK,
                    ),
                ),
            )
        return self._finish_turn(
            evidence,
            _text(state, "研究证据包已生成。", "Research evidence pack is ready."),
        )

    def _advance_improvement(self, state: CreatorGraphState) -> SupervisorTurn:
        if self._latest_draft(state) is None:
            return self._failure_turn(
                RuntimeFailure(
                    code="SOURCE_DRAFT_REQUIRED",
                    message=_text(
                        state,
                        "IMPROVE_DRAFT 需要通过 constraints.draft 提供源草稿产物。",
                        (
                            "IMPROVE_DRAFT requires constraints.draft to seed a "
                            "source draft artifact"
                        ),
                    ),
                )
            )
        return self._advance_draft_review(state)

    def _advance_draft_review(self, state: CreatorGraphState) -> SupervisorTurn:
        draft = self._latest_draft(state)
        if draft is None:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "根据已批准的大纲撰写完整正文草稿。",
                    "Write the complete draft from the approved outline.",
                ),
                steps=(
                    PlanStep(
                        id="write-draft",
                        capability=AgentCapability.WRITE_DRAFT,
                        objective=_text(
                            state,
                            "撰写一篇受证据约束的完整正文草稿。",
                            "Write an evidence-aware complete content draft.",
                        ),
                        output_kind=ArtifactKind.DRAFT,
                        input_kinds=(
                            ArtifactKind.CONTENT_OUTLINE,
                            ArtifactKind.EVIDENCE_PACK,
                        ),
                    ),
                ),
            )

        if state["goal"].constraints.get("draft_revision_requested_from") == draft.id:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "根据人工分段批注或反馈修订正文草稿。",
                    "Revise the draft from human section notes or feedback.",
                ),
                steps=(
                    PlanStep(
                        id="revise-draft-human",
                        capability=AgentCapability.REVISE_DRAFT,
                        objective=_text(
                            state,
                            "将人工批注和反馈应用到被审阅的同一版草稿。",
                            (
                                "Apply human draft annotations and feedback to the "
                                "exact reviewed draft."
                            ),
                        ),
                        output_kind=ArtifactKind.DRAFT,
                        input_kinds=(
                            ArtifactKind.DRAFT,
                            ArtifactKind.SOURCE_DRAFT,
                            ArtifactKind.CONTENT_OUTLINE,
                            ArtifactKind.EVIDENCE_PACK,
                        ),
                    ),
                ),
                usage_delta=BudgetUsage(
                    supervisor_turns=1,
                    writer_revisions=1,
                ),
            )

        if not self._draft_is_approved(state, draft):
            return self._human_turn(
                kind=CreatorDecisionKind.DRAFT_REVIEW,
                prompt=_text(
                    state,
                    "请审阅正文、批注薄弱章节，或批准草稿进入质量评审。",
                    (
                        "Review the draft, annotate weak sections, or approve it "
                        "before critic evaluation."
                    ),
                ),
                source=draft,
                allowed_actions=(
                    CreatorDecisionAction.APPROVE,
                    CreatorDecisionAction.EDIT,
                    CreatorDecisionAction.REQUEST_CHANGES,
                ),
            )

        critique = self._latest_artifact(state, ArtifactKind.CRITIQUE)
        critique_reviews_draft = (
            critique is not None
            and critique.metadata.get("reviewed_artifact_id") == draft.id
        )
        if not critique_reviews_draft:
            return self._new_plan_turn(
                state,
                reason=_text(
                    state,
                    "按照质量标准评审实际的最新草稿。",
                    "Review the actual latest draft against the quality rubric.",
                ),
                steps=(
                    PlanStep(
                        id="critique-draft",
                        capability=AgentCapability.CRITIQUE_CONTENT,
                        objective=_text(
                            state,
                            "评审最新草稿并给出质量结论。",
                            "Critique the latest draft and issue a quality verdict.",
                        ),
                        output_kind=ArtifactKind.CRITIQUE,
                        input_kinds=(
                            ArtifactKind.DRAFT,
                            ArtifactKind.SOURCE_DRAFT,
                            ArtifactKind.EVIDENCE_PACK,
                        ),
                    ),
                ),
            )

        assert critique is not None
        accepted = bool(critique.metadata.get("accepted"))
        score = float(critique.metadata.get("overall_score") or 0.0)
        if accepted and score >= self._policy.critic_acceptance_score:
            evaluation = self._latest_artifact(
                state,
                ArtifactKind.EVALUATION_REPORT,
            )
            if evaluation is None or critique.id not in evaluation.parent_ids:
                return self._new_plan_turn(
                    state,
                    reason=_text(
                        state,
                        "定稿前评估已通过质量评审的运行结果。",
                        "Evaluate the accepted run before finalization.",
                    ),
                    steps=(
                        PlanStep(
                            id="evaluate-run",
                            capability=AgentCapability.EVALUATE_RUN,
                            objective=_text(
                                state,
                                "生成版本化运行评估报告。",
                                "Compute the versioned runtime evaluation report.",
                            ),
                            output_kind=ArtifactKind.EVALUATION_REPORT,
                            input_kinds=(
                                ArtifactKind.CRITIQUE,
                                ArtifactKind.DRAFT,
                                ArtifactKind.SOURCE_DRAFT,
                                ArtifactKind.EVIDENCE_PACK,
                                ArtifactKind.CONTENT_OUTLINE,
                                ArtifactKind.CREATOR_PROFILE,
                            ),
                        ),
                    ),
                )
            return self._finish_turn(
                draft,
                _text(
                    state,
                    "草稿已通过质量评审和运行评估门禁。",
                    "Draft passed critic and evaluation gates.",
                ),
            )

        if state["usage"].writer_revisions >= state["limits"].max_writer_revisions:
            return self._failure_turn(
                RuntimeFailure(
                    code="QUALITY_GATE_FAILED",
                    message=_text(
                        state,
                        "Writer 修订预算已用尽，草稿仍未通过质量评审。",
                        (
                            "Critic rejected the draft after the writer revision "
                            "budget was exhausted"
                        ),
                    ),
                )
            )

        return self._new_plan_turn(
            state,
            reason=_text(
                state,
                "根据最新质量评审意见修订正文草稿。",
                "Revise the draft from the latest critic feedback.",
            ),
            steps=(
                PlanStep(
                    id="revise-draft",
                    capability=AgentCapability.REVISE_DRAFT,
                    objective=_text(
                        state,
                        "将质量评审意见应用到被审阅的同一版草稿。",
                        "Apply critic feedback to the exact reviewed draft.",
                    ),
                    output_kind=ArtifactKind.DRAFT,
                    input_kinds=(
                        ArtifactKind.DRAFT,
                        ArtifactKind.SOURCE_DRAFT,
                        ArtifactKind.CRITIQUE,
                        ArtifactKind.CONTENT_OUTLINE,
                        ArtifactKind.EVIDENCE_PACK,
                    ),
                ),
            ),
            usage_delta=BudgetUsage(
                supervisor_turns=1,
                writer_revisions=1,
            ),
        )

    def _context_and_topic_steps(
        self,
        state: CreatorGraphState,
        *,
        include_topics: bool,
    ) -> tuple[PlanStep, ...]:
        steps: list[PlanStep] = []
        dependencies: list[str] = []
        for kind, step in (
            (
                ArtifactKind.CREATOR_PROFILE,
                PlanStep(
                    id="load-memory",
                    capability=AgentCapability.LOAD_CREATOR_MEMORY,
                    objective=_text(
                        state,
                        "加载创作者偏好与画像信号。",
                        "Load creator preferences and profile signals.",
                    ),
                    output_kind=ArtifactKind.CREATOR_PROFILE,
                ),
            ),
            (
                ArtifactKind.CONTENT_ANALYSIS,
                PlanStep(
                    id="analyze-content",
                    capability=AgentCapability.ANALYZE_CONTENT,
                    objective=_text(
                        state,
                        "分析历史内容及互动规律。",
                        "Analyze historical posts and engagement patterns.",
                    ),
                    output_kind=ArtifactKind.CONTENT_ANALYSIS,
                ),
            ),
            (
                ArtifactKind.EVIDENCE_PACK,
                PlanStep(
                    id="research-topic",
                    capability=AgentCapability.RESEARCH_TOPIC,
                    objective=_text(
                        state,
                        "研究任务目标并记录证据缺口。",
                        "Research the goal and record evidence gaps.",
                    ),
                    output_kind=ArtifactKind.EVIDENCE_PACK,
                ),
            ),
        ):
            if self._latest_artifact(state, kind) is None:
                steps.append(step)
                dependencies.append(step.id)
        if (
            include_topics
            and self._latest_artifact(state, ArtifactKind.TOPIC_OPTIONS) is None
        ):
            steps.append(
                PlanStep(
                    id="plan-topics",
                    capability=AgentCapability.PLAN_TOPICS,
                    objective=_text(
                        state,
                        "生成彼此区分且受证据约束的选题方案。",
                        "Generate distinct, evidence-aware topic options.",
                    ),
                    output_kind=ArtifactKind.TOPIC_OPTIONS,
                    input_kinds=(
                        ArtifactKind.CREATOR_PROFILE,
                        ArtifactKind.CONTENT_ANALYSIS,
                        ArtifactKind.EVIDENCE_PACK,
                    ),
                    dependencies=tuple(dependencies),
                )
            )
        return tuple(steps)

    def _new_plan_turn(
        self,
        state: CreatorGraphState,
        *,
        reason: str,
        steps: tuple[PlanStep, ...],
        usage_delta: BudgetUsage | None = None,
    ) -> SupervisorTurn:
        revision = (
            max(
                (plan.revision for plan in state["plan_history"]),
                default=0,
            )
            + 1
        )
        plan = PlanSnapshot(revision=revision, reason=reason, steps=steps)
        failure = self._validate_plan(state, plan)
        if failure is not None:
            return self._failure_turn(failure)
        ready = self._ready_steps(plan, state["executions"])
        if not ready:
            return self._failure_turn(
                RuntimeFailure(
                    code="PLAN_HAS_NO_ENTRY",
                    message=_text(
                        state,
                        f"计划版本 {revision} 没有可执行的入口步骤。",
                        f"Plan revision {revision} has no executable entry step",
                    ),
                )
            )
        turn = self._dispatch_turn(state, plan, ready)
        if turn.decision.action == SupervisorAction.FAIL:
            return turn
        return SupervisorTurn(
            decision=turn.decision,
            plan=plan,
            plan_is_new=True,
            usage_delta=usage_delta or BudgetUsage(supervisor_turns=1),
            progress=(
                ProgressEntry(
                    sequence_key=f"plan:{revision}",
                    type="plan.created",
                    message=reason,
                ),
            ),
        )

    def _dispatch_turn(
        self,
        state: CreatorGraphState,
        plan: PlanSnapshot,
        ready: tuple[PlanStep, ...],
    ) -> SupervisorTurn:
        if (
            state["usage"].agent_dispatches + len(ready)
            > state["limits"].max_agent_dispatches
        ):
            return self._failure_turn(
                RuntimeFailure(
                    code="AGENT_DISPATCH_BUDGET_EXHAUSTED",
                    message=_text(
                        state,
                        "继续执行将超出 Agent 调度预算。",
                        "Agent dispatch budget would be exceeded",
                    ),
                )
            )
        if state["usage"].model_calls + len(ready) > state["limits"].max_model_calls:
            return self._failure_turn(
                RuntimeFailure(
                    code="MODEL_CALL_BUDGET_EXHAUSTED",
                    message=_text(
                        state,
                        "继续执行将超出模型调用预算。",
                        "Model call budget would be exceeded",
                    ),
                )
            )
        dispatch_reason = _text(
            state,
            f"调度 {len(ready)} 个已就绪的计划步骤。",
            f"Dispatch {len(ready)} ready plan step(s).",
        )
        return SupervisorTurn(
            decision=SupervisorDecision(
                action=SupervisorAction.DISPATCH,
                reason=dispatch_reason,
                dispatch_step_ids=tuple(step.id for step in ready),
            ),
            plan=plan,
            progress=(
                ProgressEntry(
                    sequence_key=(
                        f"dispatch:{plan.revision}:"
                        + ",".join(step.id for step in ready)
                    ),
                    type="plan.dispatched",
                    message=dispatch_reason,
                ),
            ),
        )

    def _recover_or_fail(
        self,
        state: CreatorGraphState,
        plan: PlanSnapshot,
        execution: StepExecution,
    ) -> SupervisorTurn:
        step = next(step for step in plan.steps if step.id == execution.step_id)
        if not execution.retryable or step.attempt >= step.max_attempts:
            return self._failure_turn(
                RuntimeFailure(
                    code=execution.error_code or "SPECIALIST_FAILED",
                    message=(
                        execution.error_message
                        or _text(
                            state,
                            "专业 Agent 步骤执行失败。",
                            "Specialist step failed",
                        )
                    ),
                    retryable=False,
                    step_id=step.id,
                    agent=execution.agent,
                )
            )
        if state["usage"].replans >= state["limits"].max_replans:
            return self._failure_turn(
                RuntimeFailure(
                    code="REPLAN_BUDGET_EXHAUSTED",
                    message=_text(
                        state,
                        "可重试的 Agent 故障已超出重新规划预算。",
                        "Retryable specialist failure exceeded replan budget",
                    ),
                    step_id=step.id,
                    agent=execution.agent,
                )
            )
        retry_step = step.model_copy(
            update={
                "id": f"{step.id}-retry-{step.attempt + 1}",
                "dependencies": (),
                "attempt": step.attempt + 1,
            }
        )
        return self._new_plan_turn(
            state,
            reason=_text(
                state,
                (
                    f"{execution.agent} 出现可重试故障"
                    f"（{execution.error_code}），重新规划。"
                ),
                (
                    f"Replan after retryable failure in {execution.agent} "
                    f"({execution.error_code})."
                ),
            ),
            steps=(retry_step,),
            usage_delta=BudgetUsage(supervisor_turns=1, replans=1),
        )

    def _validate_plan(
        self,
        state: CreatorGraphState,
        plan: PlanSnapshot,
    ) -> RuntimeFailure | None:
        if not plan.steps:
            return RuntimeFailure(
                code="EMPTY_PLAN",
                message=_text(
                    state,
                    "Supervisor 生成了空的可执行计划。",
                    "Supervisor emitted an empty executable plan",
                ),
            )
        if len(plan.steps) > self._policy.max_plan_steps:
            return RuntimeFailure(
                code="PLAN_TOO_LARGE",
                message=_text(
                    state,
                    f"计划步骤数超过上限 {self._policy.max_plan_steps}。",
                    f"Plan contains more than {self._policy.max_plan_steps} steps",
                ),
            )
        try:
            self._registry.assert_available(step.capability for step in plan.steps)
        except AgentRegistryError as exc:
            return RuntimeFailure(
                code="CAPABILITY_UNAVAILABLE",
                message=str(exc),
            )
        if _has_cycle(plan):
            return RuntimeFailure(
                code="PLAN_CYCLE",
                message=_text(
                    state,
                    f"计划版本 {plan.revision} 包含循环依赖。",
                    f"Plan revision {plan.revision} contains a dependency cycle",
                ),
            )
        if (
            state["usage"].agent_dispatches + len(plan.steps)
            > state["limits"].max_agent_dispatches
        ):
            return RuntimeFailure(
                code="AGENT_DISPATCH_BUDGET_EXHAUSTED",
                message=_text(
                    state,
                    "剩余 Agent 调度预算不足以执行当前计划。",
                    "Plan cannot fit in the remaining agent dispatch budget",
                ),
            )
        return None

    def _budget_failure(self, state: CreatorGraphState) -> RuntimeFailure | None:
        usage = state["usage"]
        limits = state["limits"]
        if usage.supervisor_turns >= limits.max_supervisor_turns:
            return RuntimeFailure(
                code="SUPERVISOR_TURN_BUDGET_EXHAUSTED",
                message=_text(
                    state,
                    "Supervisor 轮次预算已用尽。",
                    "Supervisor turn budget was exhausted",
                ),
            )
        if usage.output_tokens > limits.max_output_tokens:
            return RuntimeFailure(
                code="OUTPUT_TOKEN_BUDGET_EXHAUSTED",
                message=_text(
                    state,
                    "Agent 输出 Token 已超出预算。",
                    "Agent output token budget was exceeded",
                ),
            )
        return None

    @staticmethod
    def _ready_steps(
        plan: PlanSnapshot,
        executions: dict[str, StepExecution],
    ) -> tuple[PlanStep, ...]:
        by_step = {
            execution.step_id: execution
            for execution in executions.values()
            if execution.plan_revision == plan.revision
        }
        succeeded = {
            step_id
            for step_id, execution in by_step.items()
            if execution.status == PlanStepStatus.SUCCEEDED
        }
        return tuple(
            step
            for step in plan.steps
            if step.id not in by_step
            and all(dependency in succeeded for dependency in step.dependencies)
        )

    @staticmethod
    def _failed_steps(
        plan: PlanSnapshot,
        executions: dict[str, StepExecution],
    ) -> tuple[StepExecution, ...]:
        return tuple(
            execution
            for execution in executions.values()
            if execution.plan_revision == plan.revision
            and execution.status == PlanStepStatus.FAILED
        )

    @staticmethod
    def _plan_completed(
        plan: PlanSnapshot,
        executions: dict[str, StepExecution],
    ) -> bool:
        succeeded = {
            execution.step_id
            for execution in executions.values()
            if execution.plan_revision == plan.revision
            and execution.status == PlanStepStatus.SUCCEEDED
        }
        return all(step.id in succeeded for step in plan.steps)

    @staticmethod
    def _latest_artifact(
        state: CreatorGraphState,
        kind: ArtifactKind,
    ) -> ArtifactRef | None:
        matches = [ref for ref in state["artifacts"].values() if ref.kind == kind]
        if not matches:
            return None
        return max(matches, key=lambda ref: (ref.revision, ref.created_at))

    def _latest_draft(self, state: CreatorGraphState) -> ArtifactRef | None:
        candidates = [
            ref
            for ref in state["artifacts"].values()
            if ref.kind in {ArtifactKind.DRAFT, ArtifactKind.SOURCE_DRAFT}
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda ref: (ref.revision, ref.created_at))

    def _missing_kinds(
        self,
        state: CreatorGraphState,
        kinds: tuple[ArtifactKind, ...],
    ) -> tuple[ArtifactKind, ...]:
        return tuple(
            kind for kind in kinds if self._latest_artifact(state, kind) is None
        )

    @staticmethod
    def _topic_is_approved(state: CreatorGraphState) -> bool:
        constraints = state["goal"].constraints
        mode = str(constraints.get("approval_mode", "HUMAN")).upper()
        if mode == "AUTO" or constraints.get("selected_topic_id"):
            return True
        if mode != "ADAPTIVE":
            return False
        topics = _latest_ref(state, ArtifactKind.TOPIC_OPTIONS)
        evidence = _latest_ref(state, ArtifactKind.EVIDENCE_PACK)
        return bool(
            topics is not None
            and topics.confidence >= 0.75
            and evidence is not None
            and evidence.confidence >= 0.50
        )

    @staticmethod
    def _outline_is_approved(state: CreatorGraphState) -> bool:
        constraints = state["goal"].constraints
        mode = str(constraints.get("approval_mode", "HUMAN")).upper()
        if mode == "AUTO" or constraints.get("outline_approved") is True:
            return True
        if mode != "ADAPTIVE":
            return False
        outline = _latest_ref(state, ArtifactKind.CONTENT_OUTLINE)
        return bool(outline is not None and outline.confidence >= 0.80)

    @staticmethod
    def _draft_is_approved(state: CreatorGraphState, draft: ArtifactRef) -> bool:
        constraints = state["goal"].constraints
        if str(constraints.get("approval_mode", "HUMAN")).upper() == "AUTO":
            return True
        if constraints.get("draft_approved_artifact_id") == draft.id:
            return True
        # One human revise cycle may auto-continue into critic without a second gate.
        if constraints.get("draft_auto_approve_next"):
            revision_from = constraints.get("draft_revision_requested_from")
            if revision_from and revision_from != draft.id:
                return True
        return False

    @staticmethod
    def _finish_turn(source: ArtifactRef, reason: str) -> SupervisorTurn:
        return SupervisorTurn(
            decision=SupervisorDecision(
                action=SupervisorAction.FINISH,
                reason=reason,
                final_source_artifact_id=source.id,
            ),
            progress=(
                ProgressEntry(
                    sequence_key=f"finish:{source.id}",
                    type="run.finalizing",
                    message=reason,
                ),
            ),
        )

    @staticmethod
    def _human_turn(
        *,
        kind: CreatorDecisionKind,
        prompt: str,
        source: ArtifactRef,
        allowed_actions: tuple[CreatorDecisionAction, ...],
    ) -> SupervisorTurn:
        return SupervisorTurn(
            decision=SupervisorDecision(
                action=SupervisorAction.REQUEST_HUMAN,
                reason=prompt,
                human_request=HumanDecisionRequest(
                    kind=kind,
                    prompt=prompt,
                    source_artifact_id=source.id,
                    allowed_actions=allowed_actions,
                ),
            ),
            progress=(
                ProgressEntry(
                    sequence_key=f"human:{kind.value}:{source.id}",
                    type="decision.requested",
                    message=prompt,
                ),
            ),
        )

    @staticmethod
    def _failure_turn(failure: RuntimeFailure) -> SupervisorTurn:
        return SupervisorTurn(
            decision=SupervisorDecision(
                action=SupervisorAction.FAIL,
                reason=failure.message,
                failure=failure,
            ),
            progress=(
                ProgressEntry(
                    sequence_key=f"failure:{failure.code}:{utc_now().isoformat()}",
                    type="run.failed",
                    message=failure.message,
                    step_id=failure.step_id,
                    agent=failure.agent,
                ),
            ),
        )


def _text(
    state: CreatorGraphState,
    chinese: str,
    english: str,
) -> str:
    constraints = state["goal"].constraints
    language = str(constraints.get("language") or "").strip().lower()
    if language:
        return chinese if language.startswith("zh") else english
    return (
        chinese
        if any("\u4e00" <= character <= "\u9fff" for character in state["goal"].text)
        else english
    )


def execution_id(plan_revision: int, step: PlanStep) -> str:
    return f"p{plan_revision}:{step.id}:a{step.attempt}"


def _latest_ref(
    state: CreatorGraphState,
    kind: ArtifactKind,
) -> ArtifactRef | None:
    matches = [ref for ref in state["artifacts"].values() if ref.kind == kind]
    if not matches:
        return None
    return max(matches, key=lambda ref: (ref.revision, ref.created_at))


def _has_cycle(plan: PlanSnapshot) -> bool:
    dependencies = {step.id: set(step.dependencies) for step in plan.steps}
    ready = [step_id for step_id, required in dependencies.items() if not required]
    visited = 0
    while ready:
        completed = ready.pop()
        visited += 1
        for step_id, required in dependencies.items():
            if completed not in required:
                continue
            required.remove(completed)
            if not required:
                ready.append(step_id)
    return visited != len(dependencies)

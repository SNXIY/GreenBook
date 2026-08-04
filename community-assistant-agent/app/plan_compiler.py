from __future__ import annotations

from pydantic import ValidationError

from app.agent_registry import AgentRegistry
from app.domain import (
    AgentPlan,
    AgentPlanStep,
    PlanCompileResult,
    PlanDiagnostic,
)
from app.tools import ToolRegistry


class PlanCompiler:
    """Compile an LLM proposal into an executable, capability-covered plan.

    The planner proposes work. This compiler is the deterministic boundary that
    validates tools, step atomicity, arguments and Agent eligibility before any
    plan is persisted or executed.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        agents: AgentRegistry,
    ) -> None:
        self.tools = tools
        self.agents = agents

    def compile(
        self,
        plan: AgentPlan,
        *,
        require_goal_coverage: bool = True,
    ) -> PlanCompileResult:
        plan = self._normalize_capabilities(plan)
        diagnostics: list[PlanDiagnostic] = []
        routed_steps = []
        steps_by_id = {str(step.task_id): step for step in plan.steps}

        for step in plan.steps:
            task_id = str(step.task_id)
            try:
                definition = self.tools.get(step.tool)
            except ValueError:
                diagnostics.append(
                    PlanDiagnostic(
                        code="UNKNOWN_TOOL",
                        message=f"任务 {task_id} 使用了未注册工具 {step.tool}",
                        task_id=task_id,
                        details={
                            "tool": step.tool,
                            "available_tools": list(self.tools.names()),
                        },
                    )
                )
                continue

            if not definition.planner_visible:
                diagnostics.append(
                    PlanDiagnostic(
                        code="TOOL_NOT_PLANNER_VISIBLE",
                        message=f"工具 {step.tool} 不能由 Planner 直接选择",
                        task_id=task_id,
                        details={"tool": step.tool},
                    )
                )
                continue

            tool_agents = [
                agent
                for agent in self.agents.public_catalog()
                if step.tool in agent["tools"]
                or any(
                    pattern.endswith(".*")
                    and step.tool.startswith(pattern[:-1])
                    for pattern in agent["tools"]
                )
            ]
            full_capability_agents = [
                agent
                for agent in tool_agents
                if all(
                    self.agents.capability_graph.covers(
                        agent["capabilities"],
                        capability,
                    )
                    for capability in step.capabilities
                )
            ]
            if len(step.capabilities) > 1 and not full_capability_agents:
                diagnostics.append(
                    PlanDiagnostic(
                        code="COMPOSITE_STEP_REQUIRES_DECOMPOSITION",
                        message=(
                            f"任务 {task_id} 将多个能力压在一个执行步骤中，"
                            "需要拆成可独立路由的原子步骤"
                        ),
                        task_id=task_id,
                        details={
                            "tool": step.tool,
                            "requested_capabilities": step.capabilities,
                            "eligible_agents": [
                                {
                                    "name": agent["name"],
                                    "capabilities": agent["capabilities"],
                                }
                                for agent in tool_agents
                            ],
                        },
                    )
                )
                continue

            candidates = self.agents.candidates(step)
            if not candidates:
                diagnostics.append(
                    PlanDiagnostic(
                        code="NO_EXECUTABLE_AGENT",
                        message=(
                            f"没有 Agent 能执行工具 {step.tool}"
                            f"及主要能力 {step.primary_capability or '未声明'}"
                        ),
                        task_id=task_id,
                        details={
                            "tool": step.tool,
                            "primary_capability": step.primary_capability,
                        },
                    )
                )
                continue

            planner_arguments = {
                key: value
                for key, value in step.arguments.items()
                if key not in definition.runtime_bound_arguments
            }
            missing_examples = (
                definition.runtime_bound_arguments
                - set(definition.runtime_argument_examples)
            )
            if missing_examples:
                diagnostics.append(
                    PlanDiagnostic(
                        code="INVALID_RUNTIME_ARGUMENT_CONTRACT",
                        message=f"工具 {step.tool} 缺少运行时参数校验样例",
                        task_id=task_id,
                        details={
                            "tool": step.tool,
                            "missing_examples": sorted(missing_examples),
                        },
                    )
                )
                continue

            target_resolvers = {
                binding.resolver
                for binding in definition.artifact_bindings
                if binding.resolver.startswith("target_")
            }
            if target_resolvers and not definition.required_target_roles:
                diagnostics.append(
                    PlanDiagnostic(
                        code="MISSING_TARGET_ROLE_CONTRACT",
                        message=f"宸ュ叿 {step.tool} 鐨勭洰鏍囧弬鏁颁缁哄皯 required_target_roles",
                        task_id=task_id,
                        details={"tool": step.tool, "target_resolvers": sorted(target_resolvers)},
                    )
                )
                continue
            undeclared_roles = {
                binding.target_role
                for binding in definition.artifact_bindings
                if binding.resolver.startswith("target_")
                and binding.target_role is not None
                and binding.target_role not in (
                    definition.required_target_roles
                    | definition.optional_target_roles
                )
            }
            if undeclared_roles:
                diagnostics.append(
                    PlanDiagnostic(
                        code="UNDECLARED_TARGET_ROLE",
                        message=f"宸ュ叿 {step.tool} 浣跨敤浜嗘湭澹版槑鐨勭洰鏍囪鑹?",
                        task_id=task_id,
                        details={"roles": sorted(undeclared_roles)},
                    )
                )
                continue
            ancestor_artifacts = self._ancestor_artifacts(
                step,
                steps_by_id=steps_by_id,
            )
            ancestor_artifact_types = set(ancestor_artifacts.values())
            artifact_sources = {
                binding.argument: self._artifact_sources(
                    step,
                    accepts=binding.accepts,
                    resolver=binding.resolver,
                    steps_by_id=steps_by_id,
                )
                for binding in definition.artifact_bindings
            }
            missing_sources = {
                binding.argument: sorted(binding.accepts)
                for binding in definition.artifact_bindings
                if binding.required
                and not binding.resolver.startswith("target_")
                and ancestor_artifact_types.isdisjoint(binding.accepts)
            }
            if missing_sources:
                diagnostics.append(
                    PlanDiagnostic(
                        code="MISSING_RUNTIME_ARGUMENT_SOURCE",
                        message=(
                            f"任务 {task_id} 的运行时参数缺少可信上游产物"
                        ),
                        task_id=task_id,
                        details={
                            "tool": step.tool,
                            "ancestor_artifact_types": sorted(
                                ancestor_artifact_types
                            ),
                            "required_artifact_types": missing_sources,
                        },
                    )
                )
                continue
            validation_arguments = {
                **planner_arguments,
                **definition.runtime_argument_examples,
            }
            try:
                self.tools.validate(step.tool, validation_arguments)
            except (ValidationError, ValueError) as exc:
                diagnostics.append(
                    PlanDiagnostic(
                        code="INVALID_TOOL_ARGUMENTS",
                        message=f"任务 {task_id} 的工具参数不符合契约",
                        task_id=task_id,
                        details={
                            "tool": step.tool,
                            "error": str(exc)[:1_000],
                        },
                    )
                )
                continue

            routed_steps.append(
                step.model_copy(
                    update={
                        "agent": candidates[0].name,
                        "arguments": planner_arguments,
                        "artifact_sources": artifact_sources,
                        "success_criteria": (
                            step.success_criteria
                            or [f"{step.tool} 返回通过类型校验的真实结果"]
                        ),
                        "expected_artifact_type": (
                            step.expected_artifact_type
                            or definition.artifact_type.lower()
                        ),
                    }
                )
            )

        required = (
            set(plan.intent_detail.required_capabilities)
            if plan.intent_detail is not None
            else set()
        )
        planned = self.agents.capability_graph.expand({
            capability
            for step in plan.steps
            for capability in (
                [step.primary_capability] if step.primary_capability else []
            )
            + step.capabilities
        })
        missing = sorted(required - planned)
        if require_goal_coverage and missing:
            diagnostics.append(
                PlanDiagnostic(
                    code="GOAL_CAPABILITY_GAP",
                    message="候选计划尚未覆盖用户目标所需的全部能力",
                    details={
                        "required_capabilities": sorted(required),
                        "planned_capabilities": sorted(planned),
                        "missing_capabilities": missing,
                    },
                )
            )

        if diagnostics:
            return PlanCompileResult(
                status="NEEDS_REPLAN",
                diagnostics=diagnostics,
            )

        return PlanCompileResult(
            status="EXECUTABLE",
            compiled_plan=plan.model_copy(update={"steps": routed_steps}),
        )

    def _normalize_capabilities(self, plan: AgentPlan) -> AgentPlan:
        """Canonicalize bounded model vocabulary before capability routing.

        The model is allowed to understand natural language, but runtime
        contracts use stable names. A harmless synonym such as ``scheduling``
        must not turn an otherwise valid plan into a replan loop.
        """
        graph = self.agents.capability_graph
        intent_detail = plan.intent_detail
        if intent_detail is not None:
            # Canonicalize synonyms, then drop unknown names so a planner typo
            # like a pre-alias schedule_update cannot force an endless replan.
            required = [
                capability
                for capability in graph.normalize(
                    intent_detail.required_capabilities
                )
                if graph.knows(capability)
            ]
            intent_detail = intent_detail.model_copy(
                update={"required_capabilities": required}
            )
        steps = [
            step.model_copy(
                update={
                    "primary_capability": (
                        graph.canonicalize(step.primary_capability)
                        if step.primary_capability
                        else None
                    ),
                    "capabilities": graph.normalize(step.capabilities),
                }
            )
            for step in plan.steps
        ]
        return plan.model_copy(
            update={"intent_detail": intent_detail, "steps": steps}
        )

    def _ancestor_artifacts(
        self,
        step: AgentPlanStep,
        *,
        steps_by_id: dict[str, AgentPlanStep],
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {}
        pending = list(step.depends_on)
        visited: set[str] = set()
        while pending:
            task_id = pending.pop()
            if task_id in visited:
                continue
            visited.add(task_id)
            ancestor = steps_by_id.get(task_id)
            if ancestor is None:
                continue
            try:
                artifacts[task_id] = self.tools.get(ancestor.tool).artifact_type
            except ValueError:
                pass
            pending.extend(ancestor.depends_on)
        return artifacts

    def _artifact_sources(
        self,
        step: AgentPlanStep,
        *,
        accepts: frozenset[str],
        resolver: str,
        steps_by_id: dict[str, AgentPlanStep],
    ) -> list[str]:
        """Select compatible producers according to the binding semantics.

        A revision step commonly depends on the old draft while the following
        schedule update depends on both. Binding every transitive draft makes
        the runtime choose between the obsolete and revised versions. Searching
        breadth-first keeps direct, newest artifacts authoritative while still
        allowing a transitive producer when no nearer step creates that type.

        Aggregate evidence bindings are intentionally different: Creator needs
        every relevant upstream analysis artifact, not only the closest one.
        """
        if resolver == "creator_references":
            return [
                task_id
                for task_id, artifact_type in self._ancestor_artifacts(
                    step,
                    steps_by_id=steps_by_id,
                ).items()
                if artifact_type in accepts
            ]
        frontier = list(dict.fromkeys(str(item) for item in step.depends_on))
        visited: set[str] = set()
        while frontier:
            matches: list[str] = []
            next_frontier: list[str] = []
            for task_id in frontier:
                if task_id in visited:
                    continue
                visited.add(task_id)
                ancestor = steps_by_id.get(task_id)
                if ancestor is None:
                    continue
                try:
                    artifact_type = self.tools.get(ancestor.tool).artifact_type
                except ValueError:
                    artifact_type = None
                if artifact_type in accepts:
                    matches.append(task_id)
                next_frontier.extend(str(item) for item in ancestor.depends_on)
            if matches:
                return matches
            frontier = list(dict.fromkeys(next_frontier))
        return []

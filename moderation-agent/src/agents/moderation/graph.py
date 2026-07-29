from langgraph.graph import END, START, StateGraph

from agents.moderation.nodes import (
    AdversarialReviewNodes,
    EvidenceCollectionNodes,
    EvidenceReviewerNodes,
    LLMAdversarialReviewModel,
    LLMEvidenceReviewerModel,
    LLMModerationModel,
    LLMModerationToolAgent,
    LLMPolicyGrader,
    LLMPolicyQueryPlanner,
    LLMPolicyQueryRewriter,
    ModerationDependencies,
    ModerationNodes,
    ModerationToolAgentNodes,
    PolicyEngineNodes,
    PolicyRAGNodes,
)
from agents.moderation.routes import decision_route, review_mode_route
from agents.moderation.state import ModerationState
from agents.moderation.tools.executor import ModerationToolExecutionNode
from community.tools import default_community_context_loader
from core import settings
from moderation.services.preflight import ModerationPreflightService
from rag.cases import default_case_retriever
from rag.policy import default_agentic_policy_retriever, default_policy_retriever


def build_moderation_graph(dependencies: ModerationDependencies | None = None):
    if dependencies is None:
        moderation_model = LLMModerationModel()
        adversarial_model = LLMAdversarialReviewModel()
        tool_agent = LLMModerationToolAgent()
        reviewer_config = settings.evidence_reviewer_config()
        preflight_config = settings.moderation_preflight_config()
        dependencies = ModerationDependencies(
            classifier=moderation_model,
            judge=moderation_model,
            policy_retriever=default_policy_retriever,
            case_retriever=default_case_retriever,
            context_loader=default_community_context_loader,
            tool_agent=tool_agent,
            tool_calling_config=settings.moderation_tool_calling_config(),
            risk_investigator=adversarial_model,
            safe_advocate=adversarial_model,
            adversarial_judge=adversarial_model,
            policy_query_planner=LLMPolicyQueryPlanner(),
            agentic_policy_retriever=default_agentic_policy_retriever,
            policy_grader=LLMPolicyGrader(),
            policy_query_rewriter=LLMPolicyQueryRewriter(),
            policy_rag_config=settings.agentic_policy_rag_config(),
            evidence_reviewer=LLMEvidenceReviewerModel(reviewer_config),
            evidence_reviewer_config=reviewer_config,
            thresholds=settings.moderation_thresholds(),
            low_risk_fast_path_enabled=settings.MODERATION_LOW_RISK_FAST_PATH_ENABLED,
            adaptive_cascade_enabled=settings.MODERATION_ADAPTIVE_CASCADE_ENABLED,
            policy_engine_enabled=settings.MODERATION_POLICY_ENGINE_ENABLED,
            preflight=ModerationPreflightService(preflight_config),
            preflight_config=preflight_config,
        )
    nodes = ModerationNodes(dependencies)
    adversarial_nodes = AdversarialReviewNodes(dependencies)
    tool_agent_nodes = ModerationToolAgentNodes(dependencies)
    evidence_nodes = EvidenceCollectionNodes()
    policy_rag_nodes = PolicyRAGNodes(dependencies)
    policy_engine_nodes = PolicyEngineNodes(dependencies)
    reviewer_nodes = EvidenceReviewerNodes(dependencies)
    tool_execution_node = ModerationToolExecutionNode(dependencies)

    graph = StateGraph(ModerationState)
    graph.add_node("preprocess", nodes.preprocess)
    graph.add_node("select_evidence_strategy", tool_agent_nodes.select_evidence_strategy)
    graph.add_node("gather_context", nodes.gather_context)
    graph.add_node("scan_signals", nodes.scan_signals)
    graph.add_node("run_preflight", nodes.run_preflight)
    graph.add_node("prepare_preflight_decision", nodes.prepare_preflight_decision)
    graph.add_node("classify_risk", nodes.classify)
    graph.add_node("route_risk", nodes.route_risk)
    graph.add_node("select_reasoning_tier", tool_agent_nodes.select_reasoning_tier)
    graph.add_node("retrieve_evidence", nodes.retrieve_evidence)
    graph.add_node("initialize_tool_agent", tool_agent_nodes.initialize)
    graph.add_node(
        "prepare_low_risk_fast_path",
        tool_agent_nodes.prepare_low_risk_fast_path,
    )
    graph.add_node("prepare_cascade_fast_decision", nodes.prepare_cascade_fast_decision)
    graph.add_node("moderation_tool_agent", tool_agent_nodes.moderation_tool_agent)
    graph.add_node("moderation_tools", tool_execution_node)
    graph.add_node("mark_tool_budget", tool_agent_nodes.mark_budget_exceeded)
    graph.add_node("prepare_fixed_fallback", tool_agent_nodes.prepare_fixed_fallback)
    graph.add_node("evidence_collection_finalize", evidence_nodes.finalize)
    graph.add_node("select_policy_rag_strategy", policy_rag_nodes.select_policy_rag_strategy)
    graph.add_node("policy_query_planner", policy_rag_nodes.policy_query_planner)
    graph.add_node("policy_retriever", policy_rag_nodes.policy_retriever)
    graph.add_node("policy_grader", policy_rag_nodes.policy_grader)
    graph.add_node("policy_query_rewriter", policy_rag_nodes.policy_query_rewriter)
    graph.add_node("mark_policy_partial_stop", policy_rag_nodes.mark_partial_stop)
    graph.add_node("mark_policy_human_stop", policy_rag_nodes.mark_human_stop)
    graph.add_node("policy_evidence_finalize", policy_rag_nodes.policy_evidence_finalize)
    graph.add_node("select_review_mode", nodes.select_review_mode)
    graph.add_node("judge", nodes.judge)
    graph.add_node("risk_investigator", adversarial_nodes.risk_investigator)
    graph.add_node("safe_advocate", adversarial_nodes.safe_advocate)
    graph.add_node("adversarial_judge", adversarial_nodes.adversarial_judge)
    graph.add_node("check_evidence", nodes.check_evidence)
    graph.add_node("build_evidence_ledger", policy_engine_nodes.build_evidence_ledger)
    graph.add_node("apply_policy_engine", policy_engine_nodes.apply_policy_engine)
    graph.add_node("evidence_reviewer", reviewer_nodes.review)
    graph.add_node("validate_reviewer_route", reviewer_nodes.validate_route)
    graph.add_node("prepare_tool_revision", reviewer_nodes.prepare_tool_revision)
    graph.add_node(
        "prepare_policy_after_evidence",
        reviewer_nodes.prepare_policy_after_evidence,
    )
    graph.add_node("prepare_policy_revision", reviewer_nodes.prepare_policy_revision)
    graph.add_node("prepare_judgment_revision", reviewer_nodes.prepare_judgment_revision)
    graph.add_node("reviewer_risk_revision", adversarial_nodes.risk_investigator)
    graph.add_node("reviewer_safe_revision", adversarial_nodes.safe_advocate)
    graph.add_node("reviewer_risk_joint_revision", adversarial_nodes.risk_investigator)
    graph.add_node("reviewer_safe_joint_revision", adversarial_nodes.safe_advocate)
    graph.add_node("action_route", reviewer_nodes.prepare_action_route)
    graph.add_node("auto_pass", nodes.auto_pass)
    graph.add_node("auto_reject", nodes.auto_reject)
    graph.add_node("auto_limit", nodes.auto_limit)
    graph.add_node("human_review", nodes.human_review)
    graph.add_node("save_final_result", nodes.save_final_result)
    # add_edge 是死路
    # add_conditional_edges 是分岔路
    graph.add_edge(START, "preprocess")
    graph.add_edge("preprocess", "select_evidence_strategy")
    graph.add_conditional_edges(
        "select_evidence_strategy",
        tool_agent_nodes.evidence_strategy_route,
        {
            "adaptive": "gather_context",
            "dynamic": "scan_signals",
            "fixed": "gather_context",
        },
    )
    graph.add_edge("gather_context", "scan_signals")
    graph.add_edge("scan_signals", "run_preflight")
    graph.add_conditional_edges(
        "run_preflight",
        nodes.preflight_route,
        {
            "enforce": "prepare_preflight_decision",
            "continue": "classify_risk",
        },
    )
    graph.add_edge("prepare_preflight_decision", "check_evidence")
    graph.add_edge("classify_risk", "route_risk")
    graph.add_edge("route_risk", "select_reasoning_tier")
    graph.add_conditional_edges(
        "select_reasoning_tier",
        tool_agent_nodes.reasoning_tier_route,
        {
            "dynamic": "initialize_tool_agent",
            "fixed": "retrieve_evidence",
            "fast": "prepare_low_risk_fast_path",
        },
    )
    graph.add_conditional_edges(
        "prepare_low_risk_fast_path",
        tool_agent_nodes.route_after_fast_path_prepare,
        {
            "direct": "prepare_cascade_fast_decision",
            "evidence": "retrieve_evidence",
        },
    )
    graph.add_edge("prepare_cascade_fast_decision", "check_evidence")
    graph.add_edge("initialize_tool_agent", "moderation_tool_agent")
    graph.add_conditional_edges(
        "moderation_tool_agent",
        tool_agent_nodes.route_after_tool_agent,
        {
            "tools": "moderation_tools",
            "finalize": "evidence_collection_finalize",
            "fallback": "prepare_fixed_fallback",
            "budget": "mark_tool_budget",
        },
    )
    graph.add_edge("moderation_tools", "moderation_tool_agent")
    graph.add_edge("mark_tool_budget", "evidence_collection_finalize")
    graph.add_edge("prepare_fixed_fallback", "gather_context")
    graph.add_edge("retrieve_evidence", "evidence_collection_finalize")
    graph.add_conditional_edges(
        "evidence_collection_finalize",
        tool_agent_nodes.route_after_finalize,
        {
            "review": "select_policy_rag_strategy",
            "fallback": "prepare_fixed_fallback",
            "reviewer_policy": "prepare_policy_after_evidence",
        },
    )
    graph.add_conditional_edges(
        "prepare_policy_after_evidence",
        reviewer_nodes.route_policy_after_evidence,
        {
            "grade": "policy_grader",
            "plan": "policy_query_planner",
            "judgment": "prepare_judgment_revision",
        },
    )
    graph.add_conditional_edges(
        "select_policy_rag_strategy",
        policy_rag_nodes.policy_rag_strategy_route,
        {
            "agentic": "policy_query_planner",
            "legacy": "select_review_mode",
        },
    )
    graph.add_edge("policy_query_planner", "policy_retriever")
    graph.add_edge("policy_retriever", "policy_grader")
    graph.add_conditional_edges(
        "policy_grader",
        policy_rag_nodes.route_after_policy_grade,
        {
            "accept": "policy_evidence_finalize",
            "rewrite": "policy_query_rewriter",
            "partial_stop": "mark_policy_partial_stop",
            "human_stop": "mark_policy_human_stop",
        },
    )
    graph.add_conditional_edges(
        "policy_query_rewriter",
        policy_rag_nodes.route_after_policy_rewrite,
        {
            "retrieve": "policy_retriever",
            "partial_stop": "mark_policy_partial_stop",
            "human_stop": "mark_policy_human_stop",
        },
    )
    graph.add_edge("mark_policy_partial_stop", "policy_evidence_finalize")
    graph.add_edge("mark_policy_human_stop", "policy_evidence_finalize")
    graph.add_conditional_edges(
        "policy_evidence_finalize",
        reviewer_nodes.route_after_policy_finalize,
        {
            "review": "select_review_mode",
            "revision": "prepare_judgment_revision",
        },
    )
    graph.add_conditional_edges(
        "select_review_mode",
        review_mode_route,
        {
            "judge": "judge",
            "risk_investigator": "risk_investigator",
            "safe_advocate": "safe_advocate",
        },
    )
    graph.add_edge(["risk_investigator", "safe_advocate"], "adversarial_judge")
    graph.add_edge("judge", "check_evidence")
    graph.add_edge("adversarial_judge", "check_evidence")
    graph.add_edge("reviewer_risk_revision", "adversarial_judge")
    graph.add_edge("reviewer_safe_revision", "adversarial_judge")
    graph.add_edge(
        ["reviewer_risk_joint_revision", "reviewer_safe_joint_revision"],
        "adversarial_judge",
    )
    graph.add_conditional_edges(
        "prepare_judgment_revision",
        reviewer_nodes.route_judgment_revision,
        {
            "single": "judge",
            "judge": "adversarial_judge",
            "risk": "reviewer_risk_revision",
            "safe": "reviewer_safe_revision",
            "risk_joint": "reviewer_risk_joint_revision",
            "safe_joint": "reviewer_safe_joint_revision",
        },
    )
    graph.add_conditional_edges(
        "check_evidence",
        nodes.route_after_evidence_check,
        {
            "finalize": "action_route",
            "policy_engine": "build_evidence_ledger",
            "review": "evidence_reviewer",
        },
    )
    if dependencies.policy_engine_enabled:
        graph.add_edge("build_evidence_ledger", "apply_policy_engine")
        graph.add_conditional_edges(
            "apply_policy_engine",
            policy_engine_nodes.route_after_policy_engine,
            {
                "finalize": "action_route",
                "review": "evidence_reviewer",
            },
        )
    graph.add_edge("evidence_reviewer", "validate_reviewer_route")
    graph.add_conditional_edges(
        "validate_reviewer_route",
        reviewer_nodes.route_after_validation,
        {
            "finalize": "action_route",
            "collect_more_evidence": "prepare_tool_revision",
            "retrieve_more_policy": "prepare_policy_revision",
            "revise_judgment": "prepare_judgment_revision",
            "human_review": "human_review",
        },
    )
    graph.add_edge("prepare_tool_revision", "moderation_tool_agent")
    graph.add_conditional_edges(
        "prepare_policy_revision",
        reviewer_nodes.route_policy_revision,
        {
            "rewrite": "policy_query_rewriter",
            "plan": "policy_query_planner",
            "human_review": "human_review",
        },
    )
    graph.add_conditional_edges(
        "action_route",
        lambda state: decision_route(state, dependencies.thresholds),
        {
            "auto_pass": "auto_pass",
            "auto_reject": "auto_reject",
            "auto_limit": "auto_limit",
            "human_review": "human_review",
        },
    )
    graph.add_edge("auto_pass", "save_final_result")
    graph.add_edge("auto_reject", "save_final_result")
    graph.add_edge("auto_limit", "save_final_result")
    graph.add_edge("human_review", "save_final_result")
    graph.add_edge("save_final_result", END)

    compiled = graph.compile()
    compiled.name = "moderation-agent"
    return compiled


moderation_agent = build_moderation_graph()

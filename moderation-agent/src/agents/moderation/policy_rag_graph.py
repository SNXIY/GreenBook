from langgraph.graph import END, START, StateGraph

from agents.moderation.nodes.dependencies import ModerationDependencies
from agents.moderation.nodes.policy_rag import PolicyRAGNodes
from agents.moderation.state import ModerationState


def build_agentic_policy_rag_graph(dependencies: ModerationDependencies):
    nodes = PolicyRAGNodes(dependencies)
    graph = StateGraph(ModerationState)
    graph.add_node("policy_query_planner", nodes.policy_query_planner)
    graph.add_node("policy_retriever", nodes.policy_retriever)
    graph.add_node("policy_grader", nodes.policy_grader)
    graph.add_node("policy_query_rewriter", nodes.policy_query_rewriter)
    graph.add_node("mark_policy_partial_stop", nodes.mark_partial_stop)
    graph.add_node("mark_policy_human_stop", nodes.mark_human_stop)
    graph.add_node("policy_evidence_finalize", nodes.policy_evidence_finalize)

    graph.add_edge(START, "policy_query_planner")
    graph.add_edge("policy_query_planner", "policy_retriever")
    graph.add_edge("policy_retriever", "policy_grader")
    graph.add_conditional_edges(
        "policy_grader",
        nodes.route_after_policy_grade,
        {
            "accept": "policy_evidence_finalize",
            "rewrite": "policy_query_rewriter",
            "partial_stop": "mark_policy_partial_stop",
            "human_stop": "mark_policy_human_stop",
        },
    )
    graph.add_conditional_edges(
        "policy_query_rewriter",
        nodes.route_after_policy_rewrite,
        {
            "retrieve": "policy_retriever",
            "partial_stop": "mark_policy_partial_stop",
            "human_stop": "mark_policy_human_stop",
        },
    )
    graph.add_edge("mark_policy_partial_stop", "policy_evidence_finalize")
    graph.add_edge("mark_policy_human_stop", "policy_evidence_finalize")
    graph.add_edge("policy_evidence_finalize", END)

    compiled = graph.compile()
    compiled.name = "agentic-policy-rag"
    return compiled

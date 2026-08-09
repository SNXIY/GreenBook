"""Community task plan templates.

Each template defines a DAG of capabilities with artifact flow between
steps.  The orchestrator selects a template based on the TaskIntent's
requirements and goal_category.
"""

from __future__ import annotations

from .models import PlanStep, PlanTemplate

# ── single-step templates ────────────────────────────────────────────

SINGLE_CREATE = PlanTemplate(
    name="SINGLE_CREATE",
    description="Create a single draft without research or scheduling",
    steps=[
        PlanStep(
            capability="GENERATE_CONTENT",
            description="Generate content based on user instructions",
            output_artifact_type="DRAFT",
        ),
    ],
)

SINGLE_IMPROVE = PlanTemplate(
    name="SINGLE_IMPROVE",
    description="Revise an existing draft",
    steps=[
        PlanStep(
            capability="IMPROVE_CONTENT",
            description="Revise existing draft based on user instructions",
            input_artifact_types=["DRAFT"],
            output_artifact_type="DRAFT",
        ),
    ],
)

SINGLE_SEARCH = PlanTemplate(
    name="SINGLE_SEARCH",
    description="Search community content",
    steps=[
        PlanStep(
            capability="SEARCH_COMMUNITY",
            description="Search community posts by topic",
            output_artifact_type="SEARCH_RESULT",
        ),
    ],
)

SINGLE_PUBLISH = PlanTemplate(
    name="SINGLE_PUBLISH",
    description="Schedule a draft for publication",
    steps=[
        PlanStep(
            capability="SCHEDULE_PUBLISH",
            description="Schedule draft for future publication",
            input_artifact_types=["DRAFT"],
            output_artifact_type="SCHEDULE",
        ),
    ],
)

SINGLE_CANCEL = PlanTemplate(
    name="SINGLE_CANCEL",
    description="Cancel a scheduled publication",
    steps=[
        PlanStep(
            capability="CANCEL_SCHEDULE",
            description="Cancel the scheduled publication",
            input_artifact_types=["SCHEDULE"],
        ),
    ],
)

# ── multi-step templates ─────────────────────────────────────────────

CREATE_WITH_RESEARCH = PlanTemplate(
    name="CREATE_WITH_RESEARCH",
    description="Search community → analyze patterns → generate content",
    steps=[
        PlanStep(
            capability="SEARCH_COMMUNITY",
            description="Search community for reference content",
            output_artifact_type="SEARCH_RESULT",
            parallelizable=False,
        ),
        PlanStep(
            capability="ANALYZE_CONTENT_PATTERNS",
            description="Analyze writing patterns and trends from search results",
            depends_on=["_dep_0"],               # placeholder — filled at instantiate time
            input_artifact_types=["SEARCH_RESULT"],
            output_artifact_type="ANALYSIS_REPORT",
            parallelizable=False,
        ),
        PlanStep(
            capability="GENERATE_CONTENT",
            description="Generate new content grounded in the analysis",
            depends_on=["_dep_1"],
            input_artifact_types=["ANALYSIS_REPORT"],
            output_artifact_type="DRAFT",
            parallelizable=False,
        ),
    ],
)

CREATE_AND_PUBLISH = PlanTemplate(
    name="CREATE_AND_PUBLISH",
    description="Generate content → validate quality → schedule publish",
    steps=[
        PlanStep(
            capability="GENERATE_CONTENT",
            description="Generate content based on user instructions",
            output_artifact_type="DRAFT",
        ),
        PlanStep(
            capability="VALIDATE_QUALITY",
            description="Validate content quality (title, code examples, constraints)",
            depends_on=["_dep_0"],
            input_artifact_types=["DRAFT"],
            output_artifact_type="VALIDATION_REPORT",
        ),
        PlanStep(
            capability="SCHEDULE_PUBLISH",
            description="Schedule the validated draft for publication",
            depends_on=["_dep_1"],
            input_artifact_types=["DRAFT"],
            output_artifact_type="SCHEDULE",
        ),
    ],
)

FULL_PIPELINE = PlanTemplate(
    name="FULL_PIPELINE",
    description="Search → analyze → create → validate → publish",
    steps=[
        PlanStep(
            capability="SEARCH_COMMUNITY",
            description="Search community for reference content",
            output_artifact_type="SEARCH_RESULT",
        ),
        PlanStep(
            capability="ANALYZE_CONTENT_PATTERNS",
            description="Analyze writing patterns from search results",
            depends_on=["_dep_0"],
            input_artifact_types=["SEARCH_RESULT"],
            output_artifact_type="ANALYSIS_REPORT",
        ),
        PlanStep(
            capability="GENERATE_CONTENT",
            description="Generate new content grounded in analysis",
            depends_on=["_dep_1"],
            input_artifact_types=["ANALYSIS_REPORT"],
            output_artifact_type="DRAFT",
        ),
        PlanStep(
            capability="VALIDATE_QUALITY",
            description="Validate content meets all constraints",
            depends_on=["_dep_2"],
            input_artifact_types=["DRAFT"],
            output_artifact_type="VALIDATION_REPORT",
        ),
        PlanStep(
            capability="SCHEDULE_PUBLISH",
            description="Schedule the validated draft for publication",
            depends_on=["_dep_3"],
            input_artifact_types=["DRAFT"],
            output_artifact_type="SCHEDULE",
        ),
    ],
)

IMPROVE_WITH_RESEARCH = PlanTemplate(
    name="IMPROVE_WITH_RESEARCH",
    description="Search community → analyze → revise existing draft",
    steps=[
        PlanStep(
            capability="SEARCH_COMMUNITY",
            description="Search community for reference content",
            output_artifact_type="SEARCH_RESULT",
        ),
        PlanStep(
            capability="ANALYZE_CONTENT_PATTERNS",
            description="Analyze writing patterns from search results",
            depends_on=["_dep_0"],
            input_artifact_types=["SEARCH_RESULT"],
            output_artifact_type="ANALYSIS_REPORT",
        ),
        PlanStep(
            capability="IMPROVE_CONTENT",
            description="Revise existing draft informed by analysis",
            depends_on=["_dep_1"],
            input_artifact_types=["ANALYSIS_REPORT", "DRAFT"],
            output_artifact_type="DRAFT",
        ),
    ],
)

SINGLE_MANAGE_SCHEDULE = PlanTemplate(
    name="SINGLE_MANAGE_SCHEDULE",
    description="Update an existing scheduled publication's time",
    steps=[
        PlanStep(
            capability="MANAGE_SCHEDULE",
            description="Update the existing schedule's run_at time",
            input_artifact_types=["SCHEDULE"],
            output_artifact_type="SCHEDULE",
        ),
    ],
)

CREATE_AND_IMPROVE = PlanTemplate(
    name="CREATE_AND_IMPROVE",
    description="Generate content → immediately improve quality in one pass",
    steps=[
        PlanStep(
            capability="GENERATE_CONTENT",
            description="Generate initial content",
            output_artifact_type="DRAFT",
        ),
        PlanStep(
            capability="IMPROVE_CONTENT",
            description="Improve the generated content (title, quality, code examples)",
            depends_on=["_dep_0"],
            input_artifact_types=["DRAFT"],
            output_artifact_type="DRAFT",
        ),
    ],
)

# ── master catalog ───────────────────────────────────────────────────

ALL_TEMPLATES: dict[str, PlanTemplate] = {
    t.name: t for t in [
        SINGLE_CREATE,
        SINGLE_IMPROVE,
        SINGLE_SEARCH,
        SINGLE_PUBLISH,
        SINGLE_CANCEL,
        SINGLE_MANAGE_SCHEDULE,
        CREATE_WITH_RESEARCH,
        CREATE_AND_PUBLISH,
        CREATE_AND_IMPROVE,
        FULL_PIPELINE,
        IMPROVE_WITH_RESEARCH,
    ]
}


def get_template(name: str) -> PlanTemplate | None:
    return ALL_TEMPLATES.get(name)

# Memory Injection Quality Analysis

This is an offline context-evidence analysis, not an LLM response-quality
benchmark. Prompt text and production logic were not changed.

Dataset: **30 cases**.

## Metrics

| Metric | Value |
|---|---:|
| Positive preference alignment | 1.0000 |
| Harmful injection candidate rate | 0.2000 |
| Baseline memory context rate | 0.0000 |

## Positive Examples

- `Write a technical deep article about Agent architecture` → `['writing_depth', 'technology_stack', 'response_style']`
- `Write a technical deep article about MCP boundaries` → `['writing_depth', 'technology_stack', 'response_style']`
- `Write a technical deep article about PostgreSQL reliability` → `['writing_depth', 'technology_stack', 'response_style']`
- `Write a technical deep article about workflow design` → `['writing_depth', 'technology_stack', 'response_style']`
- `Write a technical deep article about RAG evaluation` → `['writing_depth', 'technology_stack', 'response_style']`

## Negative Examples

- None

## Harmful Injection Candidates

- `Schedule a post tomorrow about Agent architecture` → injected `['technology_stack', 'response_style', 'writing_depth']`
- `Schedule a post tomorrow about MCP boundaries` → injected `['technology_stack', 'response_style', 'writing_depth']`
- `Schedule a post tomorrow about PostgreSQL reliability` → injected `['technology_stack', 'response_style', 'writing_depth']`
- `Schedule a post tomorrow about workflow design` → injected `['technology_stack', 'response_style', 'writing_depth']`
- `Schedule a post tomorrow about RAG evaluation` → injected `['technology_stack', 'response_style', 'writing_depth']`
- `Schedule a post tomorrow about Agent architecture` → injected `['technology_stack', 'response_style', 'writing_depth']`

The harmful candidates are unrelated task requests that still receive active
same-scope preference evidence from the current retriever. They require a
retrieval-threshold/product decision before any prompt-level expansion.

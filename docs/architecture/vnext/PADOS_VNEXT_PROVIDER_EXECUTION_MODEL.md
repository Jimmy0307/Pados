# PADOS vNext Provider and Execution Model

Status: `PROPOSED`.

## Provider categories

| Category | Examples | Automation rule |
| --- | --- | --- |
| Interactive subscription channel | ChatGPT Plus/Team/Enterprise, Codex subscription, Claude Code subscription, Cursor/IDE subscription | Human-supervised session only unless an approved product capability explicitly supports governed automation |
| Metered API provider | OpenAI API, Anthropic API, Gemini/Vertex, Azure OpenAI, OpenRouter, Groq, Together, Mistral | May support headless execution through a registered adapter and budget policy |
| Local compute provider | Ollama, LM Studio, local CLI, future local inference | May support local headless execution within local resource, data and sandbox policy |

## Provider contract

Every provider registration must declare: `credential_mode`, `billing_mode`, `automation_level`, `interaction_mode`, `supports_headless_execution`, `quota_visibility`, `usage_metric_visibility`, `rate_limit_visibility`, `project_scope`, `confidentiality_level`, `allowed_data_classification`, `cost_center`, and `fallback_eligibility`.

Subscription identity is not API identity. ChatGPT Plus is not OpenAI API; Claude Code subscription is not Anthropic API. A scheduler must never infer headless authority, token visibility or billing terms from a user-session subscription.

## Execution modes

1. **Managed API Execution**: registered API credentials, headless permitted, measured usage and policy-enforced fallback.
2. **Controlled CLI Execution**: registered local/remote CLI, sandbox/worktree and command allowlists, artifact capture.
3. **Human-supervised Interactive Execution**: a human retains the session and consent boundary; PADOS records a request, context pack, outcome and evidence but does not impersonate the session.

## Routing decision order

1. Project scope, data classification and policy eligibility.
2. Required capability and adapter health.
3. Execution-mode eligibility and human gate.
4. Quota, rate limit, concurrency and cost budget.
5. Priority, latency and approved fallback chain.
6. Persisted routing decision, usage observations and evidence.

An adapter returns observations, artifacts and usage. Only Harness changes task/execution state.

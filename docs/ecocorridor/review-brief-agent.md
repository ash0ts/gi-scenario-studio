# Review Brief Agent

The Review Brief Agent prepares an evidence-linked checklist for field and
feasibility review from one portfolio the practitioner has already selected.
It does not choose a portfolio, change a model, attest model fitness, or grant
an ecological or engineering approval.

The useful division of labor is deliberate:

- the agent decides which already-authorized finding branches to inspect and
  orders closed review actions;
- guarded tools expose only opaque aliases and closed enums;
- deterministic local code resolves aliases, renders every sentence, and adds
  the exact direct-run and evidence links;
- SQLite remains authoritative and auto-saves only an immutable
  `UNCONFIRMED` draft after validation and exact trace readback;
- W&B Weave records the model turns and tool calls in its Agents view.

This is a bounded review task, not an open-ended chat. A successful invocation
has at most eight model turns, eight tool calls, 64 KiB of alias-only context,
and one fixed deadline. Parallel tool calls, sessions, search, MCP, filesystem,
shell, solver, mutation, approval, export, and generic database access are not
available to the agent.

## Practitioner flow

On the Robustness Board, select a directly evaluated portfolio and choose
**Prepare field-review brief**. The drawer shows the immutable analysis,
portfolio, evidence revision, provider, cloud boundary, and limitations before
the provider is invoked. The worker then applies these ordered gates:

```text
complete scenario ensemble
  → authorized finding discovery
  → guarded alias-only traversal
  → deterministic citation and claim validation
  → exact Weave trace flush and readback
  → atomic UNCONFIRMED draft
  → professional telemetry outbox only when exact Restricted authorization applies
```

The ordered list in the interface describes release gates, not estimated
progress. Failure at any point records a checksum-verified local rejection and
saves no draft. Regeneration creates a new immutable version linked with
`supersedes_draft_id`; a decision-board or evidence-revision change marks older
drafts `STALE`.

A human may acknowledge that the cited checklist was reviewed for factual
completeness. That acknowledgement cannot select or approve a portfolio, attest
the model, accept a QA finding, or change export eligibility.

## Agent process boundary

Each invocation starts a short-lived, project-scoped subprocess. The parent
worker lends provider and W&B credentials only through a framed stdin message;
credentials are never placed in command arguments, SQLite, exports, logs, or
the renderer. The child has no project path and receives an
`AuthorizedReviewSnapshot` containing only `F*`, `E*`, and `S*` aliases,
digests, finding enums, allowed action enums, and allowed reviewer-discipline
enums.

The only registered tools are:

```text
review_brief_get_scope
review_brief_list_findings
review_brief_get_findings
review_brief_list_option_constraints
review_brief_list_model_fitness_gaps
review_brief_resolve_evidence_aliases
review_brief_get_reference_status
review_brief_get_authorization_status
```

Every tool call revalidates a worker-issued, checksum-bound authorization lease
covering the project, analysis, portfolio, model revision, decision board,
authorization digests, snapshot digest, alias set, call count, and argument
schema. The lease lasts no more than seven minutes, cannot be broadened in the
child, and is reissued only after the worker re-resolves current repository and
cloud authorization. A revocation during that bounded invocation takes effect
no later than lease expiry; the worker also re-resolves the source before any
draft is saved. The provider output contains only finding aliases, closed
action kinds, and reviewer disciplines. It cannot contain prose, measurements,
units, locations, recommendations, or repository evidence IDs.

## Native Weave Agents tracing

The subprocess uses the pinned OpenAI Agents SDK and installs only W&B's
`WeaveTracingProcessor`, replacing the SDK's default OpenAI trace exporter. A
predetermined trace ID and W&B analysis-run association are supplied for the
invocation. `trace_include_sensitive_data` is false; trace metadata contains
only opaque hashes.

The child force-flushes the processor and reads back the exact root call. It
checks the call ID, trace ID, W&B run association, tool order, output hash,
authorization hashes, and completion gate before returning a receipt. The
parent returns or saves nothing without that receipt. This makes the Weave
Agents conversation—not a manually assembled tree of Ops—the primary
observability record.

W&B documents that its Agents view exposes conversations, model turns, tool
invocations, spans, tokens, cost, latency, and errors. See the official
[OpenAI Agents SDK integration](https://docs.wandb.ai/weave/guides/integrations/agents/openai-agents-sdk)
and [Agents activity view](https://docs.wandb.ai/weave/guides/tracking/view-agent-activity).

## Cloud and ecological safety

The desktop public replay never exposes this capability and makes no cloud
call. The desktop's synthetic CC0 Ridgeway fixture may use a separately
consented trace-only W&B lane. Synthetic drafts remain local and never enter
the professional full-sync outbox or artifact projector. Professional use
remains blocked unless the worker can authoritatively verify exact `Restricted`
scope and every selected asset has an allowed representation for that
destination.

The local renderer always preserves these claim boundaries:

> Direct Circuitscape result under documented assumptions.

It never converts modeled connectivity into observed animal movement,
crossing use, collision reduction, population or biodiversity benefit,
regulatory acceptance, approval, or design readiness.

## Evaluation and publication

The protected campaign compares, on the same versioned 14-case synthetic
dataset:

1. deterministic exhaustive checklist;
2. bounded single-shot full-context agent;
3. read-only tool-traversal Review Brief Agent.

The four Ridgeway teaching portfolios are joined by safeguard failure,
incomplete ensemble, QA rejection, stale evidence, unknown alias, prompt
injection, sensitive location, unsupported structured output, trace failure,
and unauthorized-action cases. Deterministic scorers govern release; model
judges are not an authority.

The campaign is not a release pass merely because the source implementation
or mock tests pass. Publication requires real provider calls, exact Weave
readback, a W&B `review_brief_evaluation` run, an immutable campaign artifact,
and a separately labelled append-only section in the existing EcoCorridor W&B
Report. An animation may be rendered only after that verified campaign exists,
and every frame must be derived from its artifact and exact traces.

Practitioner usefulness remains external evidence and requires at least 80%
positive ratings from the declared participant set. Until then, the agent is
experimental and the deterministic checklist remains the safe fallback.

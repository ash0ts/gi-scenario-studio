import type { EvidenceItem } from "../types";

export interface EvidenceGuideAnchor {
  kind: "project" | "model_fitness" | "alternative" | "exact_run" | "export";
  identifier: string;
}

export interface EvidenceGuideSuggestion {
  id: string;
  label: string;
  question: string;
}

export interface EvidenceGuideCapabilities {
  available: boolean;
  state: string;
  reason: string;
  requiresConsent: boolean;
  requiresContentLoggingConsent: boolean;
  inferenceConsent: boolean;
  contentLoggingConsent: boolean;
  provider: string | null;
  model: string | null;
  suggestedQuestions: EvidenceGuideSuggestion[];
  agentVariantId: string | null;
  releaseBasis: "automated_conformance" | null;
  practitionerUsefulnessStatus: "not_measured";
  releaseLabel: string | null;
}

export interface EvidenceGuideReference {
  evidenceId: string;
  kind: string;
  label: string;
  sourceId: string;
}

export interface EvidenceGuideAction {
  action: string;
  reason: string;
  evidenceIds: string[];
  objectIds: string[];
}

export interface EvidenceGuideAnswer {
  answerId: string | null;
  summary: string;
  evidenceReferences: EvidenceGuideReference[];
  limitations: string[];
  proposedActions: EvidenceGuideAction[];
  weaveRecorded: boolean;
  weaveUrl: string | null;
  agentVariantId: string | null;
  explanationPlanId: string | null;
  providerCallCount: number | null;
  releaseGateDigest: string | null;
  automatedConformanceRelease: boolean;
  releaseLabel: string | null;
}

export const AUTOMATED_CONFORMANCE_RELEASE_LABEL = "Automated conformance release · practitioner usefulness not measured";

export interface EvidenceGuideConversation {
  conversationId: string;
  turnCount: number;
  weaveUrl: string | null;
}

export interface EvidenceGuideAskResult {
  conversation: EvidenceGuideConversation;
  turnId: string | null;
  answer: EvidenceGuideAnswer;
  evidenceChecksPassed: boolean;
}

const objectValue = (value: unknown): Record<string, unknown> | null => (
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
);

const stringValue = (value: unknown): string | null => (
  typeof value === "string" && value.trim() ? value.trim() : null
);

const stringList = (value: unknown): string[] => (
  Array.isArray(value)
    ? value.map(stringValue).filter((item): item is string => item !== null)
    : []
);

const firstString = (...values: unknown[]): string | null => {
  for (const value of values) {
    const candidate = stringValue(value);
    if (candidate) return candidate;
  }
  return null;
};

export function evidenceGuideAnchor(item: EvidenceItem): EvidenceGuideAnchor {
  const kind: EvidenceGuideAnchor["kind"] = item.kind === "alternative"
    ? "alternative"
    : item.kind === "run"
      ? "exact_run"
      : item.kind === "model"
        ? "model_fitness"
        : item.id.toLowerCase().includes("export")
          ? "export"
          : "project";
  return {
    kind,
    identifier: item.id,
  };
}

export function localEvidenceGuideSuggestions(item: EvidenceItem): EvidenceGuideSuggestion[] {
  if (item.id === "ALT-017") {
    return [
      {
        id: "alt-017-hidden-guardrail",
        label: "Why can this still fail?",
        question: "Why can this alternative be nondominated and still fail the decision rules?",
      },
      {
        id: "alt-017-controlling-storm",
        label: "Which storm exposes the issue?",
        question: "Which selected storm exposes the adverse peak response, and what changed from baseline?",
      },
    ];
  }
  if (item.id === "ALT-207") {
    return [
      {
        id: "alt-207-ecological-boundary",
        label: "Does this prove ecological benefit?",
        question: "Does this result prove ecological benefit, and what would still need to be measured?",
      },
      {
        id: "alt-207-next-review",
        label: "What should happen next?",
        question: "What should an engineer investigate before carrying this candidate into feasibility review?",
      },
    ];
  }
  if (item.status === "exact") {
    return [{
      id: "exact-result-boundary",
      label: "What does this result establish?",
      question: "What does this exact result establish, and what remains outside the evidence?",
    }];
  }
  return [{
    id: "record-use",
    label: "How should I use this record?",
    question: "How should I use this record in an engineering screening decision?",
  }];
}

function normalizeSuggestions(value: unknown): EvidenceGuideSuggestion[] {
  if (!Array.isArray(value)) return [];
  const normalized: EvidenceGuideSuggestion[] = [];
  value.forEach((entry, index) => {
    if (typeof entry === "string" && entry.trim()) {
      normalized.push({ id: `worker-suggestion-${index}`, label: entry.trim(), question: entry.trim() });
      return;
    }
    const item = objectValue(entry);
    if (!item) return;
    const question = firstString(item.question, item.prompt, item.label);
    if (!question) return;
    normalized.push({
      id: firstString(item.id, item.key) ?? `worker-suggestion-${index}`,
      label: firstString(item.label, item.title) ?? question,
      question,
    });
  });
  return normalized;
}

export function normalizeEvidenceGuideCapabilities(
  value: unknown,
  fallbackSuggestions: EvidenceGuideSuggestion[] = [],
): EvidenceGuideCapabilities {
  const record = objectValue(value);
  if (!record || typeof record.available !== "boolean") {
    return {
      available: false,
      state: "UNAVAILABLE",
      reason: "The scientific worker did not return a valid Evidence Guide capability record.",
      requiresConsent: false,
      requiresContentLoggingConsent: false,
      inferenceConsent: false,
      contentLoggingConsent: false,
      provider: null,
      model: null,
      suggestedQuestions: fallbackSuggestions,
      agentVariantId: null,
      releaseBasis: null,
      practitionerUsefulnessStatus: "not_measured",
      releaseLabel: null,
    };
  }
  const workerSuggestions = normalizeSuggestions(record.suggested_questions);
  return {
    available: record.available,
    state: firstString(record.state) ?? (record.available ? "AVAILABLE" : "UNAVAILABLE"),
    reason: firstString(record.reason) ?? (record.available
      ? "Evidence-bound explanations are available."
      : "The Evidence Guide is unavailable for this project."),
    requiresConsent: record.requires_consent === true,
    requiresContentLoggingConsent: record.requires_content_logging_consent === true,
    inferenceConsent: record.inference_consent === true,
    contentLoggingConsent: record.content_logging_consent === true,
    provider: firstString(record.provider),
    model: firstString(record.model),
    suggestedQuestions: workerSuggestions.length > 0 ? workerSuggestions : fallbackSuggestions,
    agentVariantId: firstString(record.agent_variant_id),
    releaseBasis: record.release_basis === "automated_conformance"
      ? "automated_conformance"
      : null,
    practitionerUsefulnessStatus: "not_measured",
    releaseLabel: record.release_label === AUTOMATED_CONFORMANCE_RELEASE_LABEL
      ? AUTOMATED_CONFORMANCE_RELEASE_LABEL
      : null,
  };
}

function normalizeConversation(value: unknown): EvidenceGuideConversation {
  const record = objectValue(value) ?? {};
  const turns = Array.isArray(record.turns) ? record.turns : [];
  const nextSequence = typeof record.next_turn_sequence === "number" && Number.isInteger(record.next_turn_sequence)
    ? Math.max(0, record.next_turn_sequence)
    : 0;
  return {
    conversationId: firstString(record.conversation_id, record.id) ?? "",
    turnCount: Math.max(turns.length, nextSequence),
    weaveUrl: firstString(record.weave_url),
  };
}

function normalizeReference(value: unknown): EvidenceGuideReference | null {
  const record = objectValue(value);
  if (!record) return null;
  const evidenceId = firstString(record.evidence_id, record.id);
  const label = firstString(record.label, record.title);
  if (!evidenceId || !label) return null;
  return {
    evidenceId,
    kind: firstString(record.kind) ?? "evidence",
    label,
    sourceId: firstString(record.source_id) ?? evidenceId,
  };
}

function normalizeAction(value: unknown): EvidenceGuideAction | null {
  const record = objectValue(value);
  if (!record) return null;
  const reason = firstString(record.reason, record.label);
  if (!reason) return null;
  return {
    action: firstString(record.action) ?? "review",
    reason,
    evidenceIds: stringList(record.evidence_ids),
    objectIds: stringList(record.object_ids),
  };
}

export function normalizeEvidenceGuideAskResult(value: unknown): EvidenceGuideAskResult {
  const root = objectValue(value) ?? {};
  const rawAnswer = objectValue(root.answer) ?? {};
  const rawEvaluation = objectValue(root.evaluation) ?? {};
  const rawTurn = objectValue(root.turn) ?? {};
  const rawWeave = objectValue(rawAnswer.weave_conversation)
    ?? objectValue(rawAnswer.weave_trace)
    ?? {};
  const conversation = normalizeConversation(root.conversation);
  const weaveUrl = firstString(rawWeave.url, rawWeave.weave_url, conversation.weaveUrl);
  const weaveRecorded = Boolean(
    firstString(rawWeave.conversation_id, rawWeave.trace_id, rawWeave.id, weaveUrl),
  );
  const summary = firstString(rawAnswer.summary, rawAnswer.answer);
  if (!conversation.conversationId || !summary) {
    throw new Error("The worker returned an incomplete Evidence Guide response.");
  }
  if (!weaveRecorded) {
    throw new Error("The worker did not confirm the required Weave trace, so no answer can be shown.");
  }
  if (rawEvaluation.valid !== true) {
    throw new Error("The answer did not pass deterministic evidence checks, so it was not shown.");
  }
  const evidenceReferences = (Array.isArray(rawAnswer.evidence_references) ? rawAnswer.evidence_references : [])
    .map(normalizeReference)
    .filter((item): item is EvidenceGuideReference => item !== null);
  const limitations = stringList(rawAnswer.limitations);
  if (evidenceReferences.length === 0 || limitations.length === 0) {
    throw new Error("The answer was missing required evidence references or limitations, so it was not shown.");
  }
  const agentVariantId = firstString(rawAnswer.agent_variant_id);
  const explanationPlanId = firstString(rawAnswer.explanation_plan_id);
  const providerCallCount = typeof rawAnswer.provider_call_count === "number"
    && Number.isInteger(rawAnswer.provider_call_count)
    ? rawAnswer.provider_call_count
    : null;
  const releaseGateDigest = firstString(rawAnswer.release_gate_digest);
  const productionVariant = agentVariantId === "A1" || agentVariantId === "A2" || agentVariantId === "A3";
  const automatedConformanceRelease = productionVariant
    && rawAnswer.release_basis === "automated_conformance"
    && rawAnswer.practitioner_usefulness_status === "not_measured";
  const expectedCalls = agentVariantId === "A1" ? 1 : productionVariant ? 2 : null;
  if (productionVariant && (
    !automatedConformanceRelease
    || !explanationPlanId
    || providerCallCount !== expectedCalls
    || !releaseGateDigest
    || !/^[0-9a-f]{64}$/.test(releaseGateDigest)
  )) {
    throw new Error("The answer was missing its automated conformance release basis, so it was not shown.");
  }
  return {
    conversation,
    turnId: firstString(rawTurn.turn_id, rawTurn.id, rawAnswer.turn_id),
    answer: {
      answerId: firstString(rawAnswer.answer_id, rawTurn.answer_id),
      summary,
      evidenceReferences,
      limitations,
      proposedActions: (Array.isArray(rawAnswer.proposed_actions) ? rawAnswer.proposed_actions : [])
        .map(normalizeAction)
        .filter((item): item is EvidenceGuideAction => item !== null),
      weaveRecorded,
      weaveUrl,
      agentVariantId,
      explanationPlanId,
      providerCallCount,
      releaseGateDigest,
      automatedConformanceRelease,
      releaseLabel: automatedConformanceRelease
        ? AUTOMATED_CONFORMANCE_RELEASE_LABEL
        : null,
    },
    evidenceChecksPassed: rawEvaluation.valid === true,
  };
}

export function normalizeEvidenceGuideConversation(value: unknown): EvidenceGuideConversation {
  const root = objectValue(value) ?? {};
  const conversation = normalizeConversation(root.conversation ?? root);
  const turns = Array.isArray(root.turns) ? root.turns : [];
  return { ...conversation, turnCount: Math.max(conversation.turnCount, turns.length) };
}

export function evidenceGuideUnavailableReason(mode: "tauri" | "browser-demo", projectDirectory?: string): string | null {
  // The labeled browser replay has a bounded capability adapter. It reads the
  // checked release-gate receipt and can therefore show the honest fail-closed
  // state without credentials, inference, or a synthetic assistant answer.
  if (mode === "browser-demo") return null;
  if (!projectDirectory) {
    return "Open a worker-backed project before asking an evidence question. The local evidence record remains available below.";
  }
  return null;
}

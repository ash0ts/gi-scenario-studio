import { describe, expect, it } from "vitest";
import type { EvidenceItem } from "../types";
import {
  evidenceGuideAnchor,
  localEvidenceGuideSuggestions,
  normalizeEvidenceGuideAskResult,
  normalizeEvidenceGuideCapabilities,
} from "./evidenceGuide";

const alternative: EvidenceItem = {
  id: "ALT-017",
  kind: "alternative",
  eyebrow: "Exact alternative",
  title: "Low-cost caution",
  subtitle: "Nondominated but unacceptable",
  status: "exact",
  fields: [],
};

describe("Evidence Guide frontend contracts", () => {
  it("anchors a conversation to the selected evidence instead of opening a global chat", () => {
    expect(evidenceGuideAnchor(alternative)).toEqual({
      kind: "alternative",
      identifier: "ALT-017",
    });
    expect(localEvidenceGuideSuggestions(alternative).map((item) => item.question)).toContain(
      "Why can this alternative be nondominated and still fail the decision rules?",
    );
  });

  it("keeps the ecological claim boundary explicit for the carry-forward candidate", () => {
    const questions = localEvidenceGuideSuggestions({ ...alternative, id: "ALT-207" });
    expect(questions[0]?.question).toContain("prove ecological benefit");
    expect(questions[1]?.question).toContain("feasibility review");
  });

  it("normalizes worker suggestions while failing closed on malformed capability payloads", () => {
    expect(normalizeEvidenceGuideCapabilities({ available: true, state: "AVAILABLE", suggested_questions: [
      "Why does this fail?",
      { id: "next", label: "What next?", question: "What should be checked next?" },
    ] }).suggestedQuestions).toEqual([
      { id: "worker-suggestion-0", label: "Why does this fail?", question: "Why does this fail?" },
      { id: "next", label: "What next?", question: "What should be checked next?" },
    ]);
    const malformed = normalizeEvidenceGuideCapabilities({ ok: true });
    expect(malformed.available).toBe(false);
    expect(malformed.reason).toContain("valid Evidence Guide capability record");
  });

  it("accepts an answer only when it is bound to a conversation and summary", () => {
    const result = normalizeEvidenceGuideAskResult({
      conversation: { conversation_id: "conversation-1", turns: [{ turn_id: "turn-1" }] },
      turn: { turn_id: "turn-1" },
      answer: {
        answer_id: "answer-1",
        summary: "ALT-017 is nondominated in the displayed projection but fails the selected-storm peak guardrail.",
        evidence_references: [{
          evidence_id: "exact-run:run-long",
          kind: "exact_run",
          label: "Long-event exact run",
          source_id: "run-long",
        }],
        limitations: ["This does not establish field performance."],
        proposed_actions: [{ action: "review", reason: "Inspect the long-event run.", evidence_ids: ["exact-run:run-long"] }],
        weave_conversation: { conversation_id: "conversation-1", url: "https://wandb.ai/entity/project/weave" },
      },
      evaluation: { valid: true },
    });

    expect(result.conversation.conversationId).toBe("conversation-1");
    expect(result.answer.evidenceReferences[0]?.sourceId).toBe("run-long");
    expect(result.answer.weaveRecorded).toBe(true);
    expect(result.evidenceChecksPassed).toBe(true);
    expect(() => normalizeEvidenceGuideAskResult({ conversation: { conversation_id: "conversation-1" }, answer: {} })).toThrow(
      "incomplete Evidence Guide response",
    );
    expect(() => normalizeEvidenceGuideAskResult({
      conversation: { conversation_id: "conversation-1" },
      answer: {
        summary: "An untraced answer",
        evidence_references: [{ evidence_id: "source-1", label: "Source", source_id: "source-1" }],
        limitations: ["A limitation"],
      },
      evaluation: { valid: true },
    })).toThrow("required Weave trace");
  });

  it("shows the exact automated-conformance boundary only for a complete production receipt", () => {
    const result = normalizeEvidenceGuideAskResult({
      conversation: { conversation_id: "conversation-v2", turns: [{ turn_id: "turn-v2" }] },
      turn: { turn_id: "turn-v2" },
      answer: {
        answer_id: "answer-v2",
        answer: "Repository-rendered explanation.",
        evidence_references: [{
          evidence_id: "evidence-v2",
          kind: "decision_rule",
          label: "Stored rule",
          source_id: "rule-v2",
        }],
        limitations: ["This is not a design approval."],
        weave_conversation: {
          conversation_id: "conversation-v2",
          url: "https://wandb.ai/entity/project/weave",
        },
        agent_variant_id: "A1",
        explanation_plan_id: "recorded_rules_fail_v1",
        provider_call_count: 1,
        release_gate_digest: "a".repeat(64),
        release_basis: "automated_conformance",
        practitioner_usefulness_status: "not_measured",
      },
      evaluation: { valid: true },
    });
    expect(result.answer.releaseLabel).toBe(
      "Automated conformance release · practitioner usefulness not measured",
    );
    expect(result.answer.automatedConformanceRelease).toBe(true);

    expect(() => normalizeEvidenceGuideAskResult({
      conversation: { conversation_id: "conversation-v2" },
      answer: {
        answer: "Missing release digest.",
        evidence_references: [{ evidence_id: "evidence-v2", label: "Stored rule" }],
        limitations: ["A limitation"],
        weave_conversation: { conversation_id: "conversation-v2" },
        agent_variant_id: "A1",
        explanation_plan_id: "recorded_rules_fail_v1",
        provider_call_count: 1,
        release_basis: "automated_conformance",
        practitioner_usefulness_status: "not_measured",
      },
      evaluation: { valid: true },
    })).toThrow("automated conformance release basis");
  });
});

import { useEffect, useState } from "react";

import { AssistantAnswerSchema, AssistantFeedbackResponseSchema, AssistantTraceResponseSchema, createAssistantCallId, type AssistantAnswer, type AssistantStatus } from "../lib/assistant";
import { hasNativeExternalLinkHost, hostErrorMessage, hostRequest, openObservabilityReference, rpcData } from "../lib/host";
import type { RpcPayload } from "../types";
import { AssistantProviderSetup } from "./AssistantProviderSetup";
import { Icon } from "./Icon";

const QUESTION_PRESETS = [
  "Why is this alternative shown?",
  "Which documented scenario controls this claim?",
  "What changed from the baseline in the direct evidence?",
  "Why does this option fail or require review?",
  "What should an engineer or ecologist investigate next?",
] as const;

interface EvidenceAssistantPanelProps {
  projectHandle: string | null;
  evidenceId: string;
  claimReady: boolean;
  allowConfiguration: boolean;
  unavailableReason: string;
}

export function EvidenceAssistantPanel({ projectHandle, evidenceId, claimReady, allowConfiguration, unavailableReason }: EvidenceAssistantPanelProps) {
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [question, setQuestion] = useState<string>(QUESTION_PRESETS[0]);
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [state, setState] = useState<"idle" | "asking" | "checking-trace" | "opening-trace" | "feedback" | "error">("idle");
  const [message, setMessage] = useState("");
  const [traceChecked, setTraceChecked] = useState(false);
  const [feedback, setFeedback] = useState<"HELPFUL" | "NOT_HELPFUL" | null>(null);

  useEffect(() => {
    setAnswer(null);
    setMessage("");
    setTraceChecked(false);
    setFeedback(null);
  }, [evidenceId]);

  const ask = async () => {
    if (!projectHandle || !status?.configured || !claimReady || !question.trim()) return;
    const callId = createAssistantCallId("evidence");
    setState("asking");
    setMessage("");
    setTraceChecked(false);
    setFeedback(null);
    try {
      const response = await hostRequest<RpcPayload<unknown>>(answer ? "assistant.follow_up" : "assistant.explain", {
        project_handle: projectHandle,
        call_id: callId,
        question: question.trim(),
        evidence_ids: [evidenceId],
        sensitive_policy: { prohibited_terms: [], exact_location_authorized: false },
      });
      const parsed = AssistantAnswerSchema.parse(rpcData(response)).answer;
      if (parsed.call_id !== callId || !parsed.evidence_ids.includes(evidenceId)) {
        throw new Error("The validated response does not resolve the selected evidence identity.");
      }
      setAnswer(parsed);
      setState("idle");
      setMessage("Answer returned only after deterministic validation and Weave trace read-back passed.");
    } catch (reason) {
      setAnswer(null);
      setState("error");
      setMessage(hostErrorMessage(reason, "No answer was returned because provider, evidence, or trace validation failed."));
    }
  };

  const checkTrace = async () => {
    if (!projectHandle || !answer) return;
    setState("checking-trace");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("assistant.get_trace", { project_handle: projectHandle, call_id: answer.call_id });
      const trace = AssistantTraceResponseSchema.parse(rpcData(response));
      if (
        trace.call_id !== answer.call_id
        || trace.trace_id !== answer.trace_id
        || trace.association.association_id !== answer.association.association_id
        || trace.association.wandb_run_id !== answer.association.wandb_run_id
      ) throw new Error("Trace or W&B run association differs from the validated answer.");
      setTraceChecked(true);
      setState("idle");
      setMessage("Local trace receipt matches the read-back-verified answer. A cloud link remains unavailable until W&B returns a confirmed reference.");
    } catch (reason) {
      setTraceChecked(false);
      setState("error");
      setMessage(hostErrorMessage(reason, "The trace receipt could not be revalidated."));
    }
  };

  const recordFeedback = async (rating: "HELPFUL" | "NOT_HELPFUL") => {
    if (!projectHandle || !answer) return;
    setState("feedback");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("assistant.feedback", { project_handle: projectHandle, call_id: answer.call_id, rating, comment: "" });
      AssistantFeedbackResponseSchema.parse(rpcData(response));
      setFeedback(rating);
      setState("idle");
      setMessage("Feedback was attached to the confirmed Weave trace.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "Feedback was not recorded because the trace sink did not confirm it."));
    }
  };

  const openTrace = async () => {
    if (!projectHandle || !answer?.trace_url) return;
    setState("opening-trace");
    setMessage("");
    try {
      await openObservabilityReference(projectHandle, "trace", answer.trace_id);
      setState("idle");
      setMessage("Opened the exact read-back-verified Weave trace in the system browser.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The confirmed Weave trace could not be opened."));
    }
  };

  return (
    <section className="rail-section evidence-assistant" aria-labelledby="evidence-assistant-heading">
      <div className="assistant-heading"><div><span className="section-kicker">Optional · read only</span><h3 id="evidence-assistant-heading">Evidence Assistant</h3></div><span className="trace-required"><Icon name="shield" /> Weave trace required</span></div>
      <p>Ask about the selected direct-run evidence. The assistant cannot edit, attest, approve, select a project, or create an ecological value.</p>
      <AssistantProviderSetup projectHandle={projectHandle} allowConfiguration={allowConfiguration} unavailableReason={unavailableReason} onStatus={setStatus} />
      <div className="assistant-question-presets" aria-label="Evidence question suggestions">
        {QUESTION_PRESETS.map((preset) => <button type="button" key={preset} onClick={() => setQuestion(preset)} aria-pressed={question === preset}>{preset}</button>)}
      </div>
      <label><span>Question about <span className="mono">{evidenceId}</span></span><textarea rows={3} value={question} onChange={(event) => setQuestion(event.target.value)} /></label>
      <button className="secondary-button assistant-ask" type="button" disabled={!claimReady || !status?.configured || !question.trim() || state === "asking"} title={!status?.configured ? "A verified Restricted binding, provider credential, and working Weave trace sink are required." : undefined} onClick={() => void ask()}>{state === "asking" ? "Tracing and validating…" : answer ? "Ask a traced follow-up" : "Ask traced assistant"}</button>
      {answer && (
        <article className="assistant-answer">
          <div className="assistant-answer-state"><Icon name="check" /><strong>Validated · read back from trace</strong></div>
          <p>{answer.text}</p>
          {answer.numeric_claims.length > 0 && <dl className="assistant-numeric-claims">{answer.numeric_claims.map((claim) => <div key={`${claim.evidence_id}-${claim.value}-${claim.units}`}><dt className="mono">{claim.evidence_id}</dt><dd><strong>{claim.value}</strong> {claim.units}</dd></div>)}</dl>}
          <div className="assistant-limitations"><strong>Required limitations</strong><ul>{answer.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul><p>A direct Circuitscape result is not observed animal movement, crossing use, collision reduction, population viability, biodiversity benefit, or an approved corridor.</p></div>
          <dl className="assistant-trace-meta"><div><dt>Provider model</dt><dd className="mono">{answer.provider_model}</dd></div><div><dt>Trace</dt><dd className="mono">{answer.trace_id}</dd></div><div><dt>Evidence authorization</dt><dd className="mono">{answer.evidence_authorization.decision_id}</dd></div><div><dt>Cloud representation</dt><dd>{answer.evidence_authorization.generalized_derivative_count > 0 ? `${answer.evidence_authorization.generalized_derivative_count} approved generalized derivative` : "Authorized structured evidence only"}</dd></div><div><dt>Associated W&B run</dt><dd className="mono">{answer.association.wandb_run_id}</dd></div><div><dt>Analysis</dt><dd className="mono">{answer.association.analysis_id ?? "Project intake"}</dd></div><div><dt>Validator</dt><dd className="mono">{answer.validator_version}</dd></div></dl>
          <div className="assistant-answer-actions">
            <button className="text-action" type="button" disabled={state === "checking-trace"} onClick={() => void checkTrace()}>{traceChecked ? "Trace receipt confirmed" : "Recheck trace receipt"}</button>
            <button className="text-action" type="button" disabled={!answer.trace_url || !hasNativeExternalLinkHost() || state === "opening-trace"} title={!answer.trace_url ? "The trace sink did not return a confirmed provider URL." : !hasNativeExternalLinkHost() ? "Trace links open only through the signed desktop host." : undefined} onClick={() => void openTrace()}><Icon name="external" /> {state === "opening-trace" ? "Opening trace…" : "Open Weave trace"}</button>
            <span>Was this evidence explanation useful?</span>
            <button className="icon-button" type="button" aria-label="Mark assistant answer helpful" aria-pressed={feedback === "HELPFUL"} disabled={Boolean(feedback) || state === "feedback"} onClick={() => void recordFeedback("HELPFUL")}>+</button>
            <button className="icon-button" type="button" aria-label="Mark assistant answer not helpful" aria-pressed={feedback === "NOT_HELPFUL"} disabled={Boolean(feedback) || state === "feedback"} onClick={() => void recordFeedback("NOT_HELPFUL")}>−</button>
          </div>
        </article>
      )}
      {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
      {!status?.configured && <small>The deterministic Evidence Rail above remains available without any assistant call.</small>}
    </section>
  );
}

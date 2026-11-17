import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { z } from "zod";

import type { ReferencePairComparison, ReferenceScenarioBinding } from "../generated/contracts";
import { hostErrorMessage, hostRequest, rpcData } from "../lib/host";
import { AnalysisStateResponseSchema, ReferencePlanResponseSchema, ReferenceResultResponseSchema, ReferenceStateResponseSchema, ReferenceStatusResponseSchema } from "../lib/professional";
import type { EvidenceSelection, ProfessionalProjectState, RpcPayload } from "../types";
import { Icon } from "./Icon";

type ReferencePlanResponse = z.infer<typeof ReferencePlanResponseSchema>;
type ReferenceStatusResponse = z.infer<typeof ReferenceStatusResponseSchema>;
type ReferenceResultResponse = z.infer<typeof ReferenceResultResponseSchema>;
type ReferencePlanIdentity = Pick<ReferencePlanResponse, "analysis_id" | "plan_id">;

interface ReferenceComparisonPanelProps {
  projectHandle: string;
  projectState: ProfessionalProjectState;
  onEvidence: (selection: EvidenceSelection) => void;
  pollIntervalMs?: number;
}

type RequestState = "idle" | "planning" | "starting" | "checking" | "controlling" | "loading-result" | "error";

function stableFingerprint(value: string): string {
  let left = 0x811c9dc5;
  let right = 0x9e3779b9;
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    left = Math.imul(left ^ code, 0x01000193);
    right = Math.imul(right ^ (code + index), 0x85ebca6b);
  }
  return `${(left >>> 0).toString(16).padStart(8, "0")}${(right >>> 0).toString(16).padStart(8, "0")}`;
}

/** One active semantic revision maps to one replay-safe reference-analysis identity. */
export function referenceAnalysisId(projectId: string, revisionId: string): string {
  const project = projectId.replace(/[^A-Za-z0-9_.:-]/g, "_").slice(0, 34);
  const revision = revisionId.replace(/[^A-Za-z0-9_.:-]/g, "_").slice(0, 34);
  return `reference_${project}_${revision}_${stableFingerprint(`${projectId}\u0000${revisionId}`)}`;
}

/** Create a distinct immutable attempt without weakening the project/revision identity prefix. */
export function freshReferenceAnalysisId(projectId: string, revisionId: string, nonce = globalThis.crypto.randomUUID()): string {
  const stable = referenceAnalysisId(projectId, revisionId);
  // RPC analysis IDs are capped at 128 characters. The stable prefix can be
  // 96 characters, leaving 23 for a random suffix after `_attempt_`.
  const randomSuffix = nonce.replace(/[^A-Za-z0-9]/g, "").slice(0, 23);
  if (randomSuffix.length < 16) throw new Error("A cryptographically random reference-attempt identity is unavailable.");
  return `${stable}_attempt_${randomSuffix}`;
}

function modelValue(value: number): string {
  return value.toPrecision(9);
}

function terminalError(error: Record<string, unknown> | null): string | null {
  if (!error) return null;
  for (const key of ["user_message", "message", "technical_message"]) {
    const candidate = error[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate;
  }
  return "The direct reference run ended without a usable result. Inspect the retained diagnostics before planning a distinct immutable attempt.";
}

function dispositionCopy(result: ReferenceResultResponse | null, status: ReferenceStatusResponse | null) {
  const disposition = result?.evidence_disposition ?? status?.evidence_disposition ?? "PENDING";
  if (disposition === "ACTIVE_PASSED") return {
    state: "pass" as const,
    label: "Active reference match passed",
    detail: "Every retained focal-pair value is within its reviewed absolute-plus-relative tolerance. This supports review of numerical reproduction only; it does not validate ecological assumptions or approve the model.",
  };
  if (disposition === "ACTIVE_FAILED") return {
    state: "fail" as const,
    label: "Active reference match failed",
    detail: "At least one directly computed focal-pair value is outside its reviewed tolerance. The result remains evidence and blocks reference-fit claims for this model revision.",
  };
  if (disposition === "HISTORICAL_STALE") return {
    state: "review" as const,
    label: "Historical result — not current",
    detail: "The retained result does not match the active model revision, engine, or reviewed binding set. It remains inspectable but cannot gate the current Model Fitness Passport.",
  };
  if (disposition === "CONFLICT_NON_GATING") return {
    state: "fail" as const,
    label: "Conflicting current results",
    detail: "More than one current exact result disagrees. No favorable result is selected and reference evidence cannot gate Model Fitness until the conflict is resolved.",
  };
  return {
    state: "review" as const,
    label: "Reference evidence pending",
    detail: "No complete, current exact comparison is available yet.",
  };
}

function ReferenceDisposition({ state, children }: { state: "pass" | "fail" | "review"; children: string }) {
  return <span className={`status-pill status-${state}`}>{state === "pass" ? <Icon name="check" /> : state === "fail" ? <Icon name="close" /> : <Icon name="warning" />}{children}</span>;
}

function evidenceSelection(
  plan: ReferenceResultResponse["plan"],
  result: ReferenceResultResponse,
  comparison: ReferenceResultResponse["result"]["scenario_comparisons"][number],
  pair: ReferencePairComparison,
  scenarioTitle: string,
): EvidenceSelection | null {
  const link = result.result.evidence_claim_links.find((item) => item.comparison_id === comparison.comparison_id && item.scenario_id === comparison.scenario_id && item.pair_id === pair.pair_id);
  if (!link) return null;
  return {
    claimId: link.claim_id,
    title: `Reference tolerance disposition · ${scenarioTitle} · ${pair.pair_id}`,
    value: pair.within_tolerance,
    units: "within_tolerance",
    status: "verified",
    portfolioId: plan.run_plan.baseline_portfolio_id,
    scenarioId: comparison.scenario_id,
    runId: link.direct_run_id,
    baselineRunId: link.direct_run_id,
    reducer: "Boolean result of comparing the absolute direct-minus-reference difference with the reviewed absolute-plus-relative tolerance.",
    assumption: comparison.limitations.join(" "),
    source: "PROFESSIONAL_CLAIM",
  };
}

export function ReferenceComparisonPanel({ projectHandle, projectState, onEvidence, pollIntervalMs = 1_500 }: ReferenceComparisonPanelProps) {
  const bindings = projectState.effective_bundle.reference_bindings ?? [];
  const initialAnalysisId = useMemo(
    () => referenceAnalysisId(projectState.project.project_id, projectState.active_revision.revision_id),
    [projectState.active_revision.revision_id, projectState.project.project_id],
  );
  const [attemptAnalysisId, setAttemptAnalysisId] = useState(initialAnalysisId);
  const [plan, setPlan] = useState<ReferencePlanResponse | null>(null);
  const [status, setStatus] = useState<ReferenceStatusResponse | null>(null);
  const [result, setResult] = useState<ReferenceResultResponse | null>(null);
  const [requestState, setRequestState] = useState<RequestState>("idle");
  const [message, setMessage] = useState("");
  const [recoveryHandled, setRecoveryHandled] = useState(false);
  const pollInFlight = useRef(false);
  const retainedAnalysis = useMemo(() => {
    const matching = projectState.analyses
      .filter((analysis) => analysis.analysis_kind === "REFERENCE" && analysis.revision_id === projectState.active_revision.revision_id && analysis.plan_id)
      .sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.analysis_id.localeCompare(left.analysis_id));
    return matching[0] ?? null;
  }, [projectState.active_revision.revision_id, projectState.analyses]);

  useEffect(() => {
    setPlan(null);
    setStatus(null);
    setResult(null);
    setRequestState("idle");
    setMessage("");
    setRecoveryHandled(false);
    setAttemptAnalysisId(initialAnalysisId);
  }, [initialAnalysisId, projectHandle]);

  const loadResult = useCallback(async (currentPlan: ReferencePlanIdentity, automated = false) => {
    if (!automated) setRequestState("loading-result");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.reference.result", {
        project_handle: projectHandle,
        analysis_id: currentPlan.analysis_id,
        plan_id: currentPlan.plan_id,
      });
      const parsed = ReferenceResultResponseSchema.parse(rpcData(response));
      if (parsed.analysis_id !== currentPlan.analysis_id || parsed.plan_id !== currentPlan.plan_id) throw new Error("The worker returned a result for a different immutable reference plan.");
      setResult(parsed);
      setRequestState("idle");
      setMessage(parsed.evidence_disposition === "ACTIVE_PASSED"
        ? "Exact baseline reproduction passed every reviewed pair tolerance. Ecological assumptions and fitness dimensions still require independent review."
        : parsed.evidence_disposition === "ACTIVE_FAILED"
          ? "Exact baseline reproduction completed with at least one pair outside tolerance. The failed comparisons remain inspectable evidence."
          : "The exact result is retained for inspection but is not active Model Fitness evidence.");
    } catch (reason) {
      setRequestState("error");
      setMessage(hostErrorMessage(reason, "The worker could not assemble the exact reference result."));
    }
  }, [projectHandle]);

  const refreshStatus = useCallback(async (currentPlan: ReferencePlanIdentity, automated = false) => {
    if (pollInFlight.current) return;
    pollInFlight.current = true;
    if (!automated) setRequestState("checking");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.reference.status", {
        project_handle: projectHandle,
        analysis_id: currentPlan.analysis_id,
        plan_id: currentPlan.plan_id,
      });
      const parsed = ReferenceStatusResponseSchema.parse(rpcData(response));
      if (parsed.analysis_id !== currentPlan.analysis_id || parsed.plan_id !== currentPlan.plan_id) throw new Error("The worker returned status for a different immutable reference plan.");
      setStatus(parsed);
      setRequestState("idle");
      if (parsed.state === "COMPLETED") await loadResult(currentPlan, true);
      else if (parsed.state === "FAILED" || parsed.state === "CANCELLED") setMessage(terminalError(parsed.error) ?? `The exact reference analysis ended in ${parsed.state.toLowerCase()} state.`);
      else if (!automated) setMessage(`Retained direct-run state: ${parsed.state}.`);
    } catch (reason) {
      setRequestState("error");
      setMessage(hostErrorMessage(reason, "The worker could not verify the retained reference-analysis state."));
    } finally {
      pollInFlight.current = false;
    }
  }, [loadResult, projectHandle]);

  useEffect(() => {
    if (!retainedAnalysis?.plan_id) return;
    setRequestState("checking");
    setMessage("Restoring the retained reference-analysis state from the authoritative worker record…");
    void refreshStatus({ analysis_id: retainedAnalysis.analysis_id, plan_id: retainedAnalysis.plan_id }, true);
  }, [refreshStatus, retainedAnalysis?.analysis_id, retainedAnalysis?.plan_id]);

  useEffect(() => {
    const identity = plan ?? (status ? { analysis_id: status.analysis_id, plan_id: status.plan_id } : null);
    if (!identity || !status || !["RUNNING", "PAUSING"].includes(status.state) || requestState === "error") return undefined;
    const timer = window.setInterval(() => void refreshStatus(identity, true), pollIntervalMs);
    return () => window.clearInterval(timer);
  }, [plan, pollIntervalMs, refreshStatus, requestState, status]);

  if (!bindings.length) return null;

  const prepare = async (candidateAnalysisId = attemptAnalysisId) => {
    setRequestState("planning");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.reference.plan", {
        project_handle: projectHandle,
        analysis_id: candidateAnalysisId,
      });
      const parsed = ReferencePlanResponseSchema.parse(rpcData(response));
      if (parsed.analysis_id !== candidateAnalysisId || parsed.plan.revision_id !== projectState.active_revision.revision_id) throw new Error("The worker returned a reference plan for a different model revision or immutable attempt.");
      setAttemptAnalysisId(candidateAnalysisId);
      setStatus(null);
      setResult(null);
      setPlan(parsed);
      setRequestState("idle");
      setMessage(parsed.replayed ? "The immutable reference plan was reused from the local engineering record." : "The immutable baseline-only reference plan is retained and ready for review.");
      if (parsed.state !== "READY") await refreshStatus(parsed, true);
    } catch (reason) {
      setRequestState("error");
      setMessage(hostErrorMessage(reason, "The worker rejected this reference-comparison plan."));
    }
  };

  const planFreshAttempt = async () => {
    try {
      const candidateAnalysisId = freshReferenceAnalysisId(projectState.project.project_id, projectState.active_revision.revision_id);
      setAttemptAnalysisId(candidateAnalysisId);
      await prepare(candidateAnalysisId);
      setRecoveryHandled(false);
    } catch (reason) {
      setRequestState("error");
      setMessage(hostErrorMessage(reason, "A fresh immutable reference-attempt identity could not be created."));
    }
  };

  const start = async () => {
    const identity = plan ?? (status ? { analysis_id: status.analysis_id, plan_id: status.plan_id } : null);
    if (!identity) return;
    setRequestState("starting");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("connectivity.reference.start", {
        project_handle: projectHandle,
        analysis_id: identity.analysis_id,
        plan_id: identity.plan_id,
      });
      const parsed = ReferenceStateResponseSchema.parse(rpcData(response));
      if (parsed.analysis_id !== identity.analysis_id || parsed.plan_id !== identity.plan_id) throw new Error("The worker started a different reference plan.");
      setRequestState("idle");
      setMessage("Direct baseline jobs started. Progress below comes only from the durable worker record.");
      await refreshStatus(identity, true);
    } catch (reason) {
      setRequestState("error");
      setMessage(hostErrorMessage(reason, "The worker could not start the exact reference comparison."));
    }
  };

  const control = async (operation: "pause" | "resume" | "cancel") => {
    const identity = plan ?? (status ? { analysis_id: status.analysis_id, plan_id: status.plan_id } : null);
    if (!identity) return;
    setRequestState("controlling");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>(`connectivity.analysis.${operation}`, {
        project_handle: projectHandle,
        analysis_id: identity.analysis_id,
      });
      const parsed = AnalysisStateResponseSchema.parse(rpcData(response));
      if (parsed.analysis_id !== identity.analysis_id) throw new Error("The worker controlled a different reference analysis.");
      setRequestState("idle");
      if (operation === "resume" || operation === "cancel") setRecoveryHandled(true);
      setMessage(`${operation[0].toUpperCase()}${operation.slice(1)} accepted in ${parsed.state}. Completed exact evidence remains immutable.`);
      await refreshStatus(identity, true);
    } catch (reason) {
      setRequestState("error");
      setMessage(hostErrorMessage(reason, `The exact reference analysis could not ${operation}.`));
    }
  };

  const disposition = dispositionCopy(result, status);
  const authoritativePlan = status?.plan ?? plan?.plan ?? null;
  const identity = plan ?? (status ? { analysis_id: status.analysis_id, plan_id: status.plan_id } : null);
  const totalJobs = authoritativePlan?.run_plan.direct_job_count ?? bindings.length;
  const completedJobs = status?.completed_run_count ?? 0;
  const currentState = status?.state ?? plan?.state ?? null;
  const busy = requestState !== "idle" && requestState !== "error";
  const recoveryRequired = !recoveryHandled && Boolean(projectState.pending_recovery.analyses.some((analysis) => analysis.analysis_kind === "REFERENCE" && analysis.revision_id === projectState.active_revision.revision_id && analysis.analysis_id === identity?.analysis_id && analysis.recovery_required));
  const scenarios = new Map(projectState.effective_bundle.scenarios.map((scenario) => [scenario.scenario_id, scenario.title]));
  const bindingByScenario = new Map(bindings.map((binding: ReferenceScenarioBinding) => [binding.scenario_id, binding]));
  const staleReasons = result?.stale_reasons ?? status?.stale_reasons ?? [];
  const messageTone = requestState === "error" || ["FAILED", "CANCELLED"].includes(currentState ?? "") || disposition.state === "fail"
    ? "error"
    : disposition.state === "review" && (result || status) ? "review" : "success";

  let action: { label: string; run: () => void } | null = null;
  if (!identity) action = { label: requestState === "error" ? "Retry exact reference plan" : "Plan exact reference check", run: () => void prepare() };
  else if (currentState === "READY") action = { label: `Run ${totalJobs.toLocaleString()} direct matrix job${totalJobs === 1 ? "" : "s"}`, run: () => void start() };
  else if (recoveryRequired) action = { label: "Resume and recover exact jobs", run: () => void control("resume") };
  else if (currentState === "RUNNING") action = { label: "Pause after active jobs", run: () => void control("pause") };
  else if (currentState === "PAUSED") action = { label: "Resume direct matrix jobs", run: () => void control("resume") };
  else if ((currentState === "COMPLETED" && disposition.state === "review") || ["FAILED", "CANCELLED"].includes(currentState ?? "")) action = { label: "Plan new exact reference attempt", run: () => void planFreshAttempt() };
  else if (requestState === "error") action = { label: "Retry retained-state check", run: () => void refreshStatus(identity) };
  else if (currentState === "COMPLETED" && !result) action = { label: "Load exact reference result", run: () => void loadResult(identity) };

  return (
    <section className="paper-panel reference-comparison-panel" aria-labelledby="reference-comparison-heading">
      <div className="reference-comparison-heading">
        <div><span className="section-kicker">Exact baseline reproduction</span><h2 id="reference-comparison-heading">Compare the active model with reviewed reference matrices</h2></div>
        <ReferenceDisposition state={disposition.state}>{disposition.label}</ReferenceDisposition>
      </div>
      <div className="reference-comparison-boundary"><Icon name="evidence" /><p><strong>What this check answers</strong><span>Can the active No Action model reproduce each reviewed focal-pair matrix within its recorded tolerance using direct Circuitscape jobs?</span></p><p><strong>What it cannot answer</strong><span>It does not validate resistance choices, focal areas, animal movement, crossing use, collision outcomes, design, permitting, or ecological approval.</span></p></div>

      <div className="reference-comparison-command">
        <div>
          <strong>{plan?.arithmetic ?? (authoritativePlan ? `1 No Action baseline × ${authoritativePlan.run_plan.scenario_ids.length} bound scenarios = ${authoritativePlan.run_plan.direct_job_count} direct Circuitscape matrix jobs` : `1 No Action baseline × ${bindings.length} reviewed scenario binding${bindings.length === 1 ? "" : "s"}`)}</strong>
          <span>{identity && authoritativePlan ? `Immutable plan ${identity.plan_id} · active revision ${authoritativePlan.revision_id}` : `Analysis identity ${attemptAnalysisId} is derived from this project and active semantic revision.`}</span>
        </div>
        <div className="reference-comparison-actions">
          {action && <button className="primary-button" type="button" disabled={busy} onClick={action.run}>{requestState === "planning" ? "Retaining exact plan…" : requestState === "starting" ? "Starting direct jobs…" : requestState === "checking" ? "Checking retained state…" : requestState === "controlling" ? "Updating durable state…" : requestState === "loading-result" ? "Loading exact result…" : action.label}</button>}
          {identity && ["RUNNING", "PAUSING", "PAUSED"].includes(currentState ?? "") && <button className="secondary-button" type="button" disabled={busy} onClick={() => void control("cancel")}>Cancel exact reference analysis</button>}
        </div>
      </div>

      {recoveryRequired && <div className="reference-terminal reference-recovery" role="alert"><Icon name="warning" /><p><strong>Interrupted exact reference work</strong><span>The prior worker no longer owns this durable analysis. Resume will audit and requeue abandoned jobs; no incomplete solver output is treated as evidence.</span></p></div>}
      {currentState === "RUNNING" && <div className="reference-progress" role="status" aria-live="polite"><div><strong>Direct reference jobs running</strong><span>{completedJobs.toLocaleString()} of {totalJobs.toLocaleString()} complete</span></div><progress value={completedJobs} max={totalJobs} aria-label="Exact reference jobs completed" /><span className="mono">{Math.round((completedJobs / totalJobs) * 100)}%</span></div>}
      {(currentState === "FAILED" || currentState === "CANCELLED") && <div className="reference-terminal" role="alert"><Icon name="warning" /><p><strong>Reference analysis {currentState.toLowerCase()}</strong><span>{terminalError(status?.error ?? null) ?? "No active result can be used for Model Fitness. Completed direct evidence remains retained."} Review the retained diagnostics. A later semantic model revision creates a new immutable check; this terminal record is never overwritten.</span></p></div>}
      {message && <p className={`operation-message ${messageTone}`} role={requestState === "error" || ["FAILED", "CANCELLED"].includes(currentState ?? "") ? "alert" : "status"}>{message}</p>}

      {(result || status) && <div className={`reference-disposition reference-disposition-${disposition.state}`}><strong>{disposition.label}</strong><span>{disposition.detail}</span>{staleReasons.length > 0 && <ul>{staleReasons.map((reason) => <li className="mono" key={reason}>{reason}</li>)}</ul>}</div>}

      {result && <>
        <div className="table-scroll-hint">Scroll horizontally to inspect every tolerance and direct-evidence field.</div>
        <div className="table-scroll reference-comparison-table" tabIndex={0} role="region" aria-label="Exact baseline versus reviewed reference comparisons">
          <table>
            <thead><tr><th>Scenario / pair</th><th>Reviewed effective resistance (model units)</th><th>Direct Circuitscape effective resistance (model units)</th><th>Absolute difference (model units)</th><th>Allowed difference (model units)</th><th>Result</th><th>Direct evidence</th></tr></thead>
            <tbody>{result.result.scenario_comparisons.flatMap((comparison) => comparison.pair_comparisons.map((pair) => {
              const binding = bindingByScenario.get(comparison.scenario_id);
              const scenarioTitle = scenarios.get(comparison.scenario_id) ?? comparison.scenario_id;
              const evidence = evidenceSelection(result.plan, result, comparison, pair, scenarioTitle);
              return <tr key={`${comparison.comparison_id}:${pair.pair_id}`}>
                <td><strong>{scenarioTitle}</strong><span className="table-subline mono">{comparison.scenario_id} · {pair.pair_id}</span></td>
                <td className="mono">{modelValue(pair.reference_effective_resistance)}</td>
                <td className="mono">{modelValue(pair.observed_effective_resistance)}</td>
                <td><span className="mono">{modelValue(pair.absolute_difference)}</span><span className="table-subline">{(pair.relative_difference * 100).toFixed(4)}% relative</span></td>
                <td><span className="mono">≤ {modelValue(pair.allowed_difference)}</span><span className="table-subline">abs {pair.tolerance.absolute_tolerance} + rel {(pair.tolerance.relative_tolerance * 100).toFixed(3)}%</span></td>
                <td><ReferenceDisposition state={pair.within_tolerance ? "pass" : "fail"}>{pair.within_tolerance ? "Within tolerance" : "Outside tolerance"}</ReferenceDisposition><span className="table-subline">QA {comparison.qa_status.replaceAll("_", " ").toLowerCase()}</span></td>
                <td>{evidence ? <button className="table-button" type="button" onClick={() => onEvidence(evidence)}>Trace exact claim</button> : <span className="table-subline">No claim link retained</span>}<details className="pair-lineage"><summary>Run and sources</summary><dl><div><dt>Direct run</dt><dd className="mono">{comparison.direct_run_id}</dd></div><div><dt>Reference asset</dt><dd className="mono">{comparison.reference_matrix_asset_id}</dd></div><div><dt>Reviewed basis</dt><dd>{binding?.reference_basis ?? "Retained binding"}</dd></div></dl></details></td>
              </tr>;
            }))}</tbody>
          </table>
        </div>
        <p className="reference-result-note"><strong>{result.result.status === "PASSED" ? "All reviewed tolerances passed." : "At least one reviewed tolerance failed."}</strong> Direct Circuitscape result under documented assumptions. No fitness status, attestation, shortlist, or approval was changed automatically.</p>
      </>}
    </section>
  );
}

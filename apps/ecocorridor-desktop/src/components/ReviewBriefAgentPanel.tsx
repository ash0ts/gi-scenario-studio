import { useCallback, useEffect, useMemo, useState } from "react";

import {
  REVIEW_BRIEF_ACKNOWLEDGEMENT,
  type ReviewBriefAcknowledgement,
  type ReviewBriefDraft,
  type ReviewBriefPreflight,
  type ReviewBriefRunStage,
  type ReviewBriefRunState,
} from "../lib/reviewBrief";
import { Icon } from "./Icon";
import { AssistantProviderSetup } from "./AssistantProviderSetup";
import { useDialogFocus } from "./useDialogFocus";
import "./ReviewBriefAgentPanel.css";

const STAGES: Array<{ id: ReviewBriefRunStage; label: string; detail: string }> = [
  { id: "VERIFYING_PORTFOLIO", label: "Verify portfolio ensemble", detail: "Recheck the selected portfolio and immutable analysis identity." },
  { id: "RESOLVING_EVIDENCE", label: "Resolve review evidence", detail: "Load retained rules, safeguards, feasibility gaps, and claim graphs." },
  { id: "PLANNING_REVIEW", label: "Assemble review actions", detail: "Select only closed review actions tied to authorized evidence." },
  { id: "VALIDATING_OUTPUT", label: "Validate the brief", detail: "Reject unsupported findings, unsafe language, and changed source evidence." },
  { id: "READING_TRACE", label: "Read back the Weave trace", detail: "Require the exact ordered trace and W&B run association." },
  { id: "SAVING_DRAFT", label: "Save an unconfirmed draft", detail: "Persist only the validated output; no model object is changed." },
];

const FINDING_LABELS: Record<ReviewBriefDraft["items"][number]["finding_kind"], string> = {
  RULE_FAILURE: "Rule failure",
  SURVEY_FIRST: "Survey first",
  SAFEGUARD: "Safeguard",
  REVIEW_MASK: "Review mask",
  FEASIBILITY_GAP: "Feasibility gap",
  MODEL_FITNESS: "Model fitness",
  QA: "Run QA",
  ASSUMPTION: "Assumption",
  COST: "Cost basis",
  REFERENCE: "Reference comparison",
  AUTHORIZATION: "Data authorization",
};

const TOPIC_LABELS: Record<ReviewBriefDraft["items"][number]["review_topic"], string> = {
  REQUIRED_RULE: "Configured decision rule",
  SURVEY_OR_FIELD_DATA: "Survey and field data",
  SAFEGUARD_REVIEW: "Safeguard review",
  CURRENT_REDISTRIBUTION_MASK: "Current redistribution review mask",
  GENERAL_FEASIBILITY: "Feasibility basis",
  LAND_ACCESS_OR_OWNERSHIP: "Land access and ownership",
  MAINTENANCE_RESPONSIBILITY: "Maintenance responsibility",
  CULTURAL_REVIEW: "Cultural and Tribal review",
  PERMITTING_REVIEW: "Permitting review",
  TRAFFIC_OR_STRUCTURAL_FEASIBILITY: "Traffic and structural feasibility",
  ECOLOGICAL_FIELD_RISK: "Ecological field risk",
  MODEL_FITNESS_BASIS: "Model fitness basis",
  DIRECT_RUN_QA: "Direct-run QA",
  ECOLOGICAL_ASSUMPTION: "Ecological assumption review",
  COST_BASIS: "Screening cost basis",
  REFERENCE_COMPARISON: "Reference comparison",
  DATA_AUTHORIZATION: "Data authorization",
};

const PRIORITY_LABELS: Record<ReviewBriefDraft["items"][number]["priority"], string> = {
  BLOCKING: "Blocking",
  BEFORE_FEASIBILITY: "Before feasibility",
  FOLLOW_UP: "Follow up",
};

const ACTION_LABELS: Record<ReviewBriefDraft["items"][number]["action_kind"], string> = {
  INSPECT_EVIDENCE: "Inspect the cited evidence",
  FIELD_VERIFY: "Verify in the field",
  OBTAIN_QUALIFIED_REVIEW: "Obtain qualified discipline review",
  RESOLVE_LAND_OR_OWNERSHIP: "Resolve land access or ownership",
  RESOLVE_AUTHORIZATION: "Resolve data authorization",
  COMPLETE_COST_BASIS: "Complete the cost basis",
  REVISE_MODEL_AND_RERUN: "Revise the model and rerun direct scenarios",
};

export interface ReviewBriefAgentPanelProps {
  open: boolean;
  preflight: ReviewBriefPreflight;
  runState: ReviewBriefRunState;
  drafts: ReviewBriefDraft[];
  activeDraftId: string | null;
  acknowledgements?: ReviewBriefAcknowledgement[];
  projectHandle: string | null;
  observabilityConnected: boolean;
  observabilityMessage: string;
  operationMessage?: ReviewBriefOperationMessage | null;
  onClose: () => void;
  onGenerate: (experimentalAcknowledged: boolean) => void;
  onConnectObservability?: () => void;
  onProviderStatusChange: () => void;
  onSelectDraft: (draftId: string) => void;
  onInspectEvidence: (evidenceClaimId: string) => void;
  onOpenTrace: (traceId: string) => void;
  onRecordReview?: (draftId: string, acknowledgement: typeof REVIEW_BRIEF_ACKNOWLEDGEMENT, reviewer: string) => void;
  onFeedback?: (draftId: string, rating: "HELPFUL" | "NOT_HELPFUL") => void;
}

export interface ReviewBriefOperationMessage {
  tone: "SUCCESS" | "INFO" | "WARNING" | "ERROR";
  text: string;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function ReviewBriefAgentPanel({
  open,
  preflight,
  runState,
  drafts,
  activeDraftId,
  acknowledgements = [],
  projectHandle,
  observabilityConnected,
  observabilityMessage,
  operationMessage = null,
  onClose,
  onGenerate,
  onConnectObservability,
  onProviderStatusChange,
  onSelectDraft,
  onInspectEvidence,
  onOpenTrace,
  onRecordReview,
  onFeedback,
}: ReviewBriefAgentPanelProps) {
  const [acknowledged, setAcknowledged] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [experimentalAcknowledged, setExperimentalAcknowledged] = useState(false);
  const stableClose = useCallback(onClose, [onClose]);
  const dialogRef = useDialogFocus(open, stableClose);
  const activeDraft = useMemo(
    () => drafts.find((draft) => draft.draft_id === activeDraftId) ?? drafts[0] ?? null,
    [activeDraftId, drafts],
  );
  const activeAcknowledgement = useMemo(
    () => activeDraft
      ? acknowledgements.find((item) => item.draft_id === activeDraft.draft_id) ?? null
      : null,
    [acknowledgements, activeDraft],
  );
  const reviewRecorded = activeAcknowledgement !== null;

  useEffect(() => {
    setAcknowledged(reviewRecorded);
    setReviewer("");
  }, [activeDraft?.draft_id, reviewRecorded]);

  useEffect(() => {
    setExperimentalAcknowledged(false);
  }, [preflight.analysis_id, preflight.portfolio_id, preflight.decision_board_sha256]);

  if (!open) return null;

  const running = runState.state === "RUNNING";
  const canGenerate = preflight.eligible && !running && (
    !preflight.experimental_acknowledgement_required || experimentalAcknowledged
  );

  const content = (
    <div className="review-brief-panel-content">
      <header className="review-brief-header">
        <div className="review-brief-heading-copy">
          <span className="review-brief-kicker">Read-only agent · Weave trace required</span>
          <h2 id="review-brief-title">Review Brief</h2>
          <p id="review-brief-description">Turn retained decision evidence into a cited checklist for human review.</p>
        </div>
        <button className="icon-button review-brief-close" type="button" onClick={onClose} aria-label="Close review brief">
          <Icon name="close" />
        </button>
        <div className="review-brief-transect" aria-hidden="true">
          <i className="review-brief-core core-a" />
          <i className="review-brief-core core-b" />
          <i className="review-brief-road" />
          <i className="review-brief-core core-c" />
          <i className="review-brief-core core-d" />
          <span />
        </div>
      </header>

      <div className="review-brief-scroll">
        {operationMessage && (
          <div
            className={`review-brief-message message-${operationMessage.tone.toLowerCase()}`}
            role={operationMessage.tone === "ERROR" ? "alert" : "status"}
            aria-live={operationMessage.tone === "ERROR" ? "assertive" : "polite"}
          >
            <Icon name={operationMessage.tone === "SUCCESS" ? "check" : operationMessage.tone === "INFO" ? "info" : "warning"} />
            <div>
              <strong>{operationMessage.tone === "ERROR" ? "Review Brief action failed" : operationMessage.tone === "WARNING" ? "Review Brief warning" : "Review Brief update"}</strong>
              <p>{operationMessage.text}</p>
            </div>
          </div>
        )}
        <section className="review-brief-scope" aria-labelledby="review-brief-scope-heading">
          <div className="review-brief-section-heading">
            <div>
              <span>Selected portfolio preflight</span>
              <h3 id="review-brief-scope-heading" className="mono">{preflight.portfolio_id}</h3>
            </div>
            <span className={`review-brief-boundary boundary-${preflight.cloud_boundary.toLowerCase().replaceAll("_", "-")}`}>
              {preflight.cloud_boundary === "CC0_SYNTHETIC" ? "CC0 synthetic" : preflight.cloud_boundary === "RESTRICTED_VERIFIED" ? "Restricted verified" : "Cloud blocked"}
            </span>
          </div>
          <p className="review-brief-next-action">{preflight.next_action}</p>
          <dl className="review-brief-facts">
            <div><dt>Analysis</dt><dd className="mono">{preflight.analysis_id}</dd></div>
            <div><dt>Model revision</dt><dd className="mono">{preflight.model_revision_id}</dd></div>
            <div><dt>Decision evidence</dt><dd className="mono" title={preflight.decision_board_sha256}>{preflight.decision_board_sha256.slice(0, 16)}…</dd></div>
            <div><dt>Evidence scope</dt><dd>{preflight.scenario_count} scenarios · {preflight.evidence_claim_count} claims</dd></div>
            <div><dt>Disposition</dt><dd>{preflight.disposition.replaceAll("_", " ")}</dd></div>
            <div><dt>Provider</dt><dd>{preflight.provider_model ?? "Not configured"}</dd></div>
          </dl>
          <div className="review-brief-boundary-note" role="note">
            <Icon name="shield" />
            <p><strong>No approval or model change.</strong> The agent may order authorized evidence and closed review actions. Local validation renders the brief and saves it only as unconfirmed.</p>
          </div>
          {preflight.agent_release_status === "EXPERIMENTAL" && (
            <div className="review-brief-message is-stale" role="note">
              <Icon name="warning" />
              <div>
                <strong>Experimental traversal; deterministic checklist remains the default.</strong>
                <p>No complete provider-backed campaign has promoted this agent. Its output can only become an UNCONFIRMED draft after deterministic validation and trace read-back.</p>
                <label className="review-brief-acknowledgement">
                  <input type="checkbox" checked={experimentalAcknowledged} onChange={(event) => setExperimentalAcknowledged(event.target.checked)} />
                  <span>I understand this runs the experimental agent and does not select or approve this portfolio.</span>
                </label>
              </div>
            </div>
          )}
          {!observabilityConnected ? (
            <div className="review-brief-message is-blocked" role="note">
              <Icon name="cloud" />
              <div>
                <strong>Optional tracing is not connected.</strong>
                <p>{observabilityMessage || (preflight.source_mode === "RIDGEWAY_SYNTHETIC" ? "The local decision board and deterministic checklist remain available. Connect the explicit CC0 trace-only target to try the experimental agent; W&B visibility is not asserted and full synchronization stays off." : "The local decision board and deterministic checklist remain available. Connect a supported exact-scope Restricted W&B destination only to try the experimental traced agent.")}</p>
                {onConnectObservability && <button className="secondary-button" type="button" onClick={onConnectObservability}>Connect W&B + Weave</button>}
              </div>
            </div>
          ) : (
            <AssistantProviderSetup
              projectHandle={projectHandle}
              allowConfiguration
              unavailableReason={preflight.source_mode === "RIDGEWAY_SYNTHETIC" ? "The exact CC0 trace-only W&B target must remain connected." : "The verified Restricted W&B destination must remain connected."}
              capability={preflight.source_mode === "RIDGEWAY_SYNTHETIC" ? "RIDGEWAY_REVIEW_BRIEF_ONLY" : "PROFESSIONAL_ASSISTANTS"}
              onStatus={() => onProviderStatusChange()}
            />
          )}
          {!preflight.eligible && (
            <div className="review-brief-message is-blocked" role="alert">
              <Icon name="lock" />
              <div><strong>Review Brief is unavailable.</strong><p>{preflight.blocked_reason}</p></div>
            </div>
          )}
        </section>

        {preflight.deterministic_available && (
          <section className="review-brief-deterministic" aria-labelledby="review-brief-default-heading">
            <div className="review-brief-section-heading">
              <div><span>{preflight.production_default === "DETERMINISTIC_CHECKLIST" ? "Production default · local only" : "Local exhaustive control"}</span><h3 id="review-brief-default-heading">Deterministic field-review checklist</h3></div>
              <span className="mono">{preflight.deterministic_items.length} cited items</span>
            </div>
            <p className="review-brief-progress-note">Built exhaustively from every retained review finding. It makes no provider call, does not rank the portfolio, and remains available when the experimental agent is off.</p>
            <ol className="review-brief-checklist">
              {preflight.deterministic_items.map((item, index) => (
                <li key={item.item_id} aria-labelledby={`review-default-topic-${item.item_id}`}>
                  <div className="review-brief-check-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
                  <div className="review-brief-check-body">
                    <div className="review-brief-check-meta">
                      <span className={`priority-${item.priority.toLowerCase().replaceAll("_", "-")}`}>{PRIORITY_LABELS[item.priority]}</span>
                      <span>{FINDING_LABELS[item.finding_kind]}</span>
                    </div>
                    <h4 id={`review-default-topic-${item.item_id}`}><span className="sr-only">Review topic: </span>{TOPIC_LABELS[item.review_topic]}</h4>
                    <p>{item.rendered_text}</p>
                    <dl>
                      <div><dt>Required action</dt><dd>{ACTION_LABELS[item.action_kind]}</dd></div>
                      <div><dt>Review by</dt><dd>{item.reviewer_disciplines.map((discipline) => discipline.toLowerCase()).join(" · ")}</dd></div>
                      {item.scenario_ids.length > 0 && <div><dt>Scenario</dt><dd className="mono">{item.scenario_ids.join(" · ")}</dd></div>}
                      {item.direct_run_ids.length > 0 && <div><dt>Direct runs</dt><dd className="mono">{item.direct_run_ids.join(" · ")}</dd></div>}
                    </dl>
                    <div className="review-brief-evidence-actions" aria-label={`Evidence for deterministic checklist item ${index + 1}`}>
                      {item.evidence_claim_ids.map((claimId, evidenceIndex) => (
                        <button type="button" className="review-brief-evidence-link" key={claimId} onClick={() => { onClose(); onInspectEvidence(claimId); }}>
                          <Icon name="evidence" /> Evidence {evidenceIndex + 1}<span className="sr-only">: {claimId}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                </li>
              ))}
            </ol>
            <p className="review-brief-evidence-transition">Opening cited evidence closes this modal so the Evidence Rail is visible and keyboard-accessible.</p>
          </section>
        )}

        {runState.state === "RUNNING" && (
          <section className="review-brief-run" aria-labelledby="review-brief-run-heading" aria-live="polite">
            <div className="review-brief-section-heading">
              <div><span>Worker-gated execution</span><h3 id="review-brief-run-heading">Six gates must pass before save</h3></div>
              <span className="review-brief-live-state">Awaiting terminal result</span>
            </div>
            <ol className="review-brief-stages">
              {STAGES.map((stage, index) => {
                return (
                  <li key={stage.id} className="stage-pending">
                    <span className="review-brief-stage-marker">{index + 1}</span>
                    <div><strong>{stage.label}</strong><small>{stage.detail}</small></div>
                  </li>
                );
              })}
            </ol>
            <p className="review-brief-progress-note">This RPC reports only terminal success or failure, so no individual gate is shown as complete while it runs. The worker returns no draft until every gate succeeds; failure returns an audited rejection and saves nothing.</p>
          </section>
        )}

        {runState.state === "FAILED" && (
          <div className="review-brief-message is-error" role="alert">
            <Icon name="warning" />
            <div><strong>Nothing was saved.</strong><p>{runState.message}</p><small className="mono">{runState.code}</small></div>
          </div>
        )}

        {drafts.length > 0 && (
          <section className="review-brief-history" aria-labelledby="review-brief-history-heading">
            <div className="review-brief-section-heading">
              <div><span>Immutable draft history</span><h3 id="review-brief-history-heading">Saved review briefs</h3></div>
              <span className="mono">{drafts.length} retained</span>
            </div>
            <label className="review-brief-history-picker">
              <span>Open retained draft</span>
              <select value={activeDraft?.draft_id ?? ""} onChange={(event) => onSelectDraft(event.target.value)}>
                {drafts.map((draft) => <option value={draft.draft_id} key={draft.draft_id}>{draft.status} · {formatDate(draft.created_at)} · {draft.draft_id.slice(0, 16)}…</option>)}
              </select>
            </label>
          </section>
        )}

        {activeDraft && (
          <article className={`review-brief-draft draft-${activeDraft.status.toLowerCase()}`} aria-labelledby="review-brief-draft-heading">
            <div className="review-brief-draft-status">
              <span className={`review-brief-status status-${activeDraft.status.toLowerCase()}`}>{activeDraft.status}</span>
              <span><Icon name="check" /> Trace read back and validated</span>
            </div>
            <div className="review-brief-draft-heading">
              <div>
                <span className="mono">{activeDraft.draft_id}</span>
                <h3 id="review-brief-draft-heading">Human review checklist</h3>
                <p>Auto-saved {formatDate(activeDraft.created_at)}. It does not change the model or select a portfolio.</p>
                {activeDraft.supersedes_draft_id && <p className="review-brief-lineage">Supersedes <span className="mono">{activeDraft.supersedes_draft_id}</span></p>}
              </div>
              <span className="review-brief-item-count">{activeDraft.items.length} open</span>
            </div>
            {activeDraft.status === "STALE" && (
              <div className="review-brief-message is-stale" role="alert">
                <Icon name="warning" />
                <div><strong>The source evidence changed.</strong><p>Keep this draft for history, then generate a new brief from the current analysis before recording review.</p></div>
              </div>
            )}
            {activeDraft.items.length ? (
              <ol className="review-brief-checklist">
                {activeDraft.items.map((item, index) => (
                  <li key={item.item_id} aria-labelledby={`review-brief-topic-${item.item_id}`}>
                    <div className="review-brief-check-index" aria-hidden="true">{String(index + 1).padStart(2, "0")}</div>
                    <div className="review-brief-check-body">
                      <div className="review-brief-check-meta">
                        <span className={`priority-${item.priority.toLowerCase().replaceAll("_", "-")}`}>{PRIORITY_LABELS[item.priority]}</span>
                        <span>{FINDING_LABELS[item.finding_kind]}</span>
                      </div>
                      <h4 id={`review-brief-topic-${item.item_id}`}>
                        <span className="sr-only">Review topic: </span>
                        {TOPIC_LABELS[item.review_topic]}
                      </h4>
                      <p>{item.rendered_text}</p>
                      <dl>
                        <div><dt>Required action</dt><dd>{ACTION_LABELS[item.action_kind]}</dd></div>
                        <div><dt>Review by</dt><dd>{item.reviewer_disciplines.map((discipline) => discipline.toLowerCase()).join(" · ")}</dd></div>
                        {item.scenario_ids.length > 0 && <div><dt>Scenario</dt><dd className="mono">{item.scenario_ids.join(" · ")}</dd></div>}
                        {item.direct_run_ids.length > 0 && <div><dt>Direct runs</dt><dd className="mono">{item.direct_run_ids.join(" · ")}</dd></div>}
                      </dl>
                      <div className="review-brief-evidence-actions" aria-label={`Evidence for checklist item ${index + 1}`}>
                        {item.evidence_claim_ids.map((claimId, evidenceIndex) => (
                          <button type="button" className="review-brief-evidence-link" key={claimId} onClick={() => { onClose(); onInspectEvidence(claimId); }}>
                            <Icon name="evidence" /> Evidence {evidenceIndex + 1}<span className="sr-only">: {claimId}</span>
                          </button>
                        ))}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="review-brief-empty">
                <Icon name="check" />
                <p><strong>No review gap was selected from the retained evidence.</strong> The brief adds no generic or invented action. Standard qualified review still applies.</p>
              </div>
            )}
            {activeDraft.items.length > 0 && <p className="review-brief-evidence-transition">Opening cited evidence closes this modal so the Evidence Rail is visible and keyboard-accessible.</p>}
            <section className="review-brief-limitations" aria-labelledby="review-brief-limitations-heading">
              <h4 id="review-brief-limitations-heading">Required boundaries</h4>
              <ul>{activeDraft.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
              <p>Direct modeled connectivity is not observed animal movement, crossing use, collision reduction, population or biodiversity benefit, approval, or design readiness.</p>
            </section>
            {onRecordReview && activeAcknowledgement ? (
              <section className="review-brief-attestation review-brief-attestation-recorded" aria-labelledby="review-brief-acknowledgement-heading">
                <h4 id="review-brief-acknowledgement-heading">Factual-completeness review recorded</h4>
                <dl>
                  <div><dt>Reviewer</dt><dd>{activeAcknowledgement.reviewer}</dd></div>
                  <div><dt>Recorded</dt><dd>{formatDate(activeAcknowledgement.acknowledged_at)}</dd></div>
                  <div><dt>Statement</dt><dd>{activeAcknowledgement.statement}</dd></div>
                </dl>
                <small>This retained acknowledgement does not select or approve a portfolio, attest model fitness, or change export eligibility.</small>
              </section>
            ) : onRecordReview ? (
              <div className="review-brief-attestation">
                <label className="review-brief-reviewer">
                  <span>Reviewer name or accountable role</span>
                  <input value={reviewer} maxLength={256} disabled={activeDraft.status === "STALE"} onChange={(event) => setReviewer(event.target.value)} />
                </label>
                <label className="review-brief-acknowledgement">
                  <input type="checkbox" checked={acknowledged} disabled={activeDraft.status === "STALE"} onChange={(event) => setAcknowledged(event.target.checked)} />
                  <span>{REVIEW_BRIEF_ACKNOWLEDGEMENT}</span>
                </label>
                <button className="primary-button" type="button" disabled={!acknowledged || !reviewer.trim() || activeDraft.status === "STALE"} onClick={() => onRecordReview(activeDraft.draft_id, REVIEW_BRIEF_ACKNOWLEDGEMENT, reviewer.trim())}>
                  Record human review
                </button>
                <small>This records review of this immutable brief only. It is not model attestation or portfolio approval.</small>
              </div>
            ) : null}
            {onFeedback && (
              <div className="review-brief-feedback" aria-label="Review Brief usefulness feedback">
                <span>Was this evidence checklist useful?</span>
                <button className="text-action" type="button" onClick={() => onFeedback(activeDraft.draft_id, "HELPFUL")}>Yes</button>
                <button className="text-action" type="button" onClick={() => onFeedback(activeDraft.draft_id, "NOT_HELPFUL")}>No</button>
              </div>
            )}
          </article>
        )}
      </div>

      <footer className="review-brief-footer">
        <div>
          <strong>{activeDraft ? `${activeDraft.status} draft` : preflight.deterministic_available ? "Deterministic checklist ready" : "No draft saved"}</strong>
          <span>{activeDraft ? `Source ${activeDraft.decision_board_sha256.slice(0, 12)}…` : "Only the experimental agent path requires trace read-back before auto-save."}</span>
        </div>
        <div className="review-brief-footer-actions">
          {activeDraft && <button className="secondary-button" type="button" onClick={() => onOpenTrace(activeDraft.trace.trace_id)}><Icon name="external" /> Open in Weave Agents</button>}
          <button className="primary-button" type="button" disabled={!canGenerate} title={!preflight.eligible ? preflight.blocked_reason ?? undefined : running ? "A review brief run is already active." : preflight.experimental_acknowledgement_required && !experimentalAcknowledged ? "Acknowledge the experimental-agent boundary first." : undefined} onClick={() => onGenerate(experimentalAcknowledged)}>
            <Icon name="spark" /> {running ? "Generating traced draft…" : activeDraft ? "Run experimental agent again" : "Run experimental traced agent"}
          </button>
        </div>
      </footer>
    </div>
  );

  return (
    <div className="review-brief-overlay" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialogRef} className="review-brief-panel" role="dialog" aria-modal="true" aria-labelledby="review-brief-title" aria-describedby="review-brief-description">
        {content}
      </div>
    </div>
  );
}

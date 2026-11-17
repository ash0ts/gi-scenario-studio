import { useEffect, useRef, useState } from "react";
import {
  observabilityLabel,
  projectCloudSyncDetail,
  projectCloudSyncLabel,
  projectCloudWandbUrl,
  projectCloudWeaveUrl,
  type ProjectCloudSyncState,
} from "../lib/observability";
import {
  evidenceGuideAnchor,
  evidenceGuideUnavailableReason,
  localEvidenceGuideSuggestions,
  normalizeEvidenceGuideAskResult,
  normalizeEvidenceGuideCapabilities,
  normalizeEvidenceGuideConversation,
  type EvidenceGuideAnswer,
  type EvidenceGuideCapabilities,
  type EvidenceGuideConversation,
  type EvidenceGuideReference,
} from "../lib/evidenceGuide";
import { transport } from "../lib/transport";
import type { EvidenceItem, ObservabilityState, ProjectKind } from "../types";
import { Icon } from "./Icons";

const notice = "Conceptual planning model — not for design, permitting, flood-risk determination, or representation of an existing drainage system.";

interface EvidenceRailProps {
  item: EvidenceItem;
  projectId: string;
  projectDirectory?: string;
  analysisId?: string;
  modelRevisionId?: string;
  projectKind: ProjectKind;
  observability: ObservabilityState;
  projectCloudSync: ProjectCloudSyncState | null;
  open: boolean;
  onClose: () => void;
  onEvidenceReference?: (reference: EvidenceGuideReference) => void;
  onConfigureObservability?: () => void;
}

export function explanationFor(item: EvidenceItem): string {
  if (item.id === "guided-manifest") {
    return "Use this record to audit the replay as a whole. Its job totals reconcile four untouched baselines plus 220 alternatives across four storms, and its hashes bind the source manifest, solver, and metric extractor. Open an individual alternative when you need storm-level values and artifacts.";
  }
  if (item.id === "ALT-017") {
    return "Use this record to see why the cheapest-looking option can still fail. ALT-017 reduces aggregate volume by only 4.14%, below the 6% target, and violates the selected-storm peak guardrail twice: 3.14 to 3.21 cfs in the long event and 13.33 to 13.34 cfs in the stress event. Open either storm-specific run record to inspect the exact input, output, report, and QA hashes.";
  }
  if (item.id === "ALT-207") {
    return "Use this record to justify a screening hand-off, not a design decision. ALT-207 passes the four teaching rules across its four exact runs, so it may move to site and feasibility review. That modeled hydraulic signal does not establish water-quality improvement, groundwater recharge, habitat or heat benefit, real-world flood reduction, maintenance performance, or lifecycle impact. It still needs drainage confirmation, utility locating, soils testing, constructability review, and professional model review.";
  }
  if (item.id === "qa-continuity") {
    return "Use this record to separate numerical acceptance from model fitness. Passing the fixture QA policy means the stored runs met its solver and continuity checks; it does not establish calibration, adopted boundary conditions, regulatory acceptance, or real-world flood safety.";
  }
  if (item.id === "cost-basis") {
    return "Use this record only to interpret the teaching cost comparison. Check its currency, horizon, discount rate, source, maintenance, and replacement assumptions before comparing alternatives. These synthetic values are not a bid, estimate, or local cost basis.";
  }
  if (item.status === "exact") {
    return `Use this ${item.title} record when you need to defend the displayed result. The run IDs identify the exact storm executions; the hashes bind their inputs and outputs; QA records how each execution was classified. It does not prove calibration, regulatory acceptance, or real-world performance.`;
  }
  if (item.kind === "assumption") {
    return "This record affects how alternatives are ranked or interpreted, but it is not a hydraulic observation. Confirm its source, units, vintage, and reviewer status before using it in professional work.";
  }
  if (item.kind === "storm") {
    return "This record defines one boundary condition. Compare its source and authority status before treating results as design or compliance evidence; this guided storm is synthetic and has no adopted recurrence interval.";
  }
  return "This record explains where the object or model-fitness claim came from. It helps you decide whether the model is fit for the stated screening use; it does not create a hydraulic result by itself.";
}

export function EvidenceRail({
  item,
  projectId,
  projectDirectory,
  analysisId,
  modelRevisionId,
  projectKind,
  observability,
  projectCloudSync,
  open,
  onClose,
  onEvidenceReference,
  onConfigureObservability,
}: EvidenceRailProps) {
  const exactEvidence = item.status === "exact";
  const [explanationOpen, setExplanationOpen] = useState(false);
  const [guideCapabilities, setGuideCapabilities] = useState<EvidenceGuideCapabilities | null>(null);
  const [guideCapabilitiesBusy, setGuideCapabilitiesBusy] = useState(false);
  const [guideQuestion, setGuideQuestion] = useState("");
  const [guideInferenceConsent, setGuideInferenceConsent] = useState(false);
  const [guideContentConsent, setGuideContentConsent] = useState(false);
  const [guideConversation, setGuideConversation] = useState<EvidenceGuideConversation | null>(null);
  const [guideAnswer, setGuideAnswer] = useState<EvidenceGuideAnswer | null>(null);
  const [guideTurnId, setGuideTurnId] = useState<string | null>(null);
  const [guideChecksPassed, setGuideChecksPassed] = useState(false);
  const [guideBusy, setGuideBusy] = useState(false);
  const [guideError, setGuideError] = useState<string | null>(null);
  const [guideFeedback, setGuideFeedback] = useState<string | null>(null);
  const [guideFeedbackBusy, setGuideFeedbackBusy] = useState(false);
  const [guideTraceBusy, setGuideTraceBusy] = useState(false);
  const railRef = useRef<HTMLElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const guideAnchorRef = useRef(item.id);
  const guideRequestSequence = useRef(0);
  const cloudLabel = projectCloudSync ? projectCloudSyncLabel(projectCloudSync) : observabilityLabel(observability);
  const cloudDetail = projectCloudSync ? projectCloudSyncDetail(projectCloudSync) : "No project-level cloud status is available in this interface preview";
  const wandbUrl = projectCloudSync ? projectCloudWandbUrl(projectCloudSync) : observability.wandbUrl ?? null;
  const weaveUrl = projectCloudSync ? projectCloudWeaveUrl(projectCloudSync) : observability.weaveUrl ?? null;
  const localSuggestions = localEvidenceGuideSuggestions(item);
  const guideSuggestions = [
    ...localSuggestions,
    ...(guideCapabilities?.suggestedQuestions ?? []),
  ].filter((suggestion, index, all) => all.findIndex((candidate) => candidate.question === suggestion.question) === index).slice(0, 3);
  const unavailableReason = evidenceGuideUnavailableReason(transport.mode, projectDirectory);
  const guideAvailable = guideCapabilities?.available === true && !unavailableReason;
  const guideHeldBackByReleaseGate = guideCapabilities?.state === "MODEL_NOT_RELEASE_GATED"
    || guideCapabilities?.state === "EVIDENCE_GUIDE_RELEASE_GATE_INVALID"
    || guideCapabilities?.state === "EVIDENCE_GUIDE_RELEASE_GATE_UNAVAILABLE";
  const guideConsentReady = (
    (!guideCapabilities?.requiresConsent || guideCapabilities.inferenceConsent || guideInferenceConsent)
    && (!guideCapabilities?.requiresContentLoggingConsent || guideCapabilities.contentLoggingConsent || guideContentConsent)
  );

  const guideBaseParams = () => ({
    project_directory: projectDirectory,
    project_id: projectId,
    ...(analysisId ? { analysis_id: analysisId } : {}),
    ...(modelRevisionId ? { model_revision_id: modelRevisionId } : {}),
    anchor: evidenceGuideAnchor(item),
  });

  useEffect(() => {
    guideAnchorRef.current = item.id;
    guideRequestSequence.current += 1;
    setExplanationOpen(false);
    setGuideQuestion("");
    setGuideConversation(null);
    setGuideAnswer(null);
    setGuideTurnId(null);
    setGuideChecksPassed(false);
    setGuideError(null);
    setGuideFeedback(null);
    if (open) window.requestAnimationFrame(() => {
      railRef.current?.scrollTo({ top: 0 });
      headingRef.current?.focus({ preventScroll: true });
    });
  }, [item.id, open]);

  useEffect(() => {
    if (!open) return;
    if (unavailableReason) {
      setGuideCapabilities(normalizeEvidenceGuideCapabilities(null, localSuggestions));
      setGuideCapabilitiesBusy(false);
      return;
    }
    let active = true;
    setGuideCapabilitiesBusy(true);
    setGuideCapabilities(null);
    transport.request<unknown>("assistant.get_capabilities", guideBaseParams(), { timeoutMs: 10_000 })
      .then((result) => {
        if (!active) return;
        const capabilities = normalizeEvidenceGuideCapabilities(result, localSuggestions);
        setGuideCapabilities(capabilities);
        setGuideInferenceConsent(capabilities.inferenceConsent || !capabilities.requiresConsent);
        setGuideContentConsent(capabilities.contentLoggingConsent);
      })
      .catch((reason) => {
        if (!active) return;
        setGuideCapabilities(normalizeEvidenceGuideCapabilities({
          available: false,
          state: "UNAVAILABLE",
          reason: reason instanceof Error ? reason.message : "The Evidence Guide capability check failed.",
        }, localSuggestions));
      })
      .finally(() => { if (active) setGuideCapabilitiesBusy(false); });
    return () => { active = false; };
  // The evidence ID and project revision are the semantic boundaries for a conversation.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, item.id, projectId, projectDirectory, analysisId, modelRevisionId, unavailableReason]);

  const askEvidenceGuide = async () => {
    const question = guideQuestion.trim();
    if (!question || !guideAvailable || !guideConsentReady || !projectDirectory) return;
    const anchorId = item.id;
    const requestSequence = ++guideRequestSequence.current;
    setGuideBusy(true);
    setGuideError(null);
    setGuideFeedback(null);
    let activeConversationId = guideConversation?.conversationId ?? "";
    try {
      if (!activeConversationId) {
        const started = await transport.request<unknown>("assistant.start_conversation", {
          ...guideBaseParams(),
          inference_consent: true,
          content_logging_consent: guideContentConsent,
        }, { timeoutMs: 15_000 });
        const conversation = normalizeEvidenceGuideConversation(started);
        if (!conversation.conversationId) throw new Error("The worker did not create an Evidence Guide conversation.");
        if (guideAnchorRef.current !== anchorId) return;
        activeConversationId = conversation.conversationId;
        setGuideConversation(conversation);
      }
      const result = normalizeEvidenceGuideAskResult(await transport.request<unknown>("assistant.ask", {
        project_directory: projectDirectory,
        project_id: projectId,
        conversation_id: activeConversationId,
        question,
      }, { timeoutMs: 90_000 }));
      if (guideAnchorRef.current !== anchorId) return;
      setGuideConversation(result.conversation);
      setGuideAnswer(result.answer);
      setGuideTurnId(result.turnId);
      setGuideChecksPassed(result.evidenceChecksPassed);
      setGuideQuestion("");
      void transport.request<unknown>("assistant.get_conversation", {
        project_directory: projectDirectory,
        project_id: projectId,
        conversation_id: result.conversation.conversationId,
      }, { timeoutMs: 10_000 }).then((latest) => {
        const conversation = normalizeEvidenceGuideConversation(latest);
        if (guideAnchorRef.current === anchorId && conversation.conversationId === result.conversation.conversationId) setGuideConversation(conversation);
      }).catch(() => undefined);
    } catch (reason) {
      if (guideRequestSequence.current === requestSequence) {
        setGuideError(reason instanceof Error ? reason.message : "The Evidence Guide could not create a traced answer.");
        if (activeConversationId) {
          void transport.request<unknown>("assistant.get_conversation", {
            project_directory: projectDirectory,
            project_id: projectId,
            conversation_id: activeConversationId,
          }, { timeoutMs: 10_000 }).then((latest) => {
            const conversation = normalizeEvidenceGuideConversation(latest);
            if (guideAnchorRef.current === anchorId && conversation.conversationId === activeConversationId) setGuideConversation(conversation);
          }).catch(() => undefined);
        }
      }
    } finally {
      if (guideRequestSequence.current === requestSequence) setGuideBusy(false);
    }
  };

  const recordGuideFeedback = async (feedback: "helpful" | "incorrect" | "unsafe") => {
    if (!projectDirectory || !guideConversation || !guideTurnId) return;
    setGuideFeedbackBusy(true);
    setGuideError(null);
    try {
      await transport.request("assistant.feedback", {
        project_directory: projectDirectory,
        project_id: projectId,
        conversation_id: guideConversation.conversationId,
        turn_id: guideTurnId,
        feedback,
      }, { timeoutMs: 15_000 });
      setGuideFeedback(feedback);
    } catch (reason) {
      setGuideError(reason instanceof Error ? reason.message : "Feedback could not be recorded in Weave.");
    } finally {
      setGuideFeedbackBusy(false);
    }
  };

  const openWeaveConversation = async () => {
    if (!projectDirectory || !guideConversation) return;
    setGuideTraceBusy(true);
    setGuideError(null);
    try {
      const result = await transport.request<{ url?: unknown }>("assistant.open_weave_conversation", {
        project_directory: projectDirectory,
        project_id: projectId,
        conversation_id: guideConversation.conversationId,
      }, { timeoutMs: 10_000 });
      const url = typeof result.url === "string" ? result.url : guideAnswer?.weaveUrl;
      if (!url || !url.startsWith("https://")) throw new Error("The worker did not return a safe Weave conversation link.");
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (reason) {
      setGuideError(reason instanceof Error ? reason.message : "The Weave conversation could not be opened.");
    } finally {
      setGuideTraceBusy(false);
    }
  };

  return (
    <aside ref={railRef} className={`evidence-rail ${open ? "is-open" : ""}`} aria-label="Evidence details" aria-hidden={!open} aria-live="polite">
      <div className="evidence-topline">
        <div className="evidence-mark"><Icon name="trace" /></div>
        <span>Evidence rail</span>
        <button className="icon-button" onClick={onClose} aria-label="Close evidence rail"><Icon name="close" /></button>
      </div>

      <div className="evidence-heading">
        <span className={`status-dot status-${item.status}`} />
        <p className="eyebrow">{item.eyebrow}</p>
        <h2 ref={headingRef} tabIndex={-1}>{item.title}</h2>
        <p>{item.subtitle}</p>
        <code className="evidence-id">Evidence ID · {item.id}</code>
      </div>

      {(projectKind === "conceptual" || projectKind === "demo") && (
        <div className="rail-notice">
          <Icon name="warning" />
          <p>{projectKind === "demo" ? "Teaching model — results are for learning and interface evaluation." : notice}</p>
        </div>
      )}

      {item.claim && <section className="rail-claim"><span>What this record supports</span><p>{item.claim}</p></section>}

      <dl className="evidence-fields">
        {item.fields.map((field) => (
          <div key={field.label}>
            <dt>{field.label}</dt>
            <dd className={field.mono ? "mono" : ""}>{field.value}</dd>
          </div>
        ))}
      </dl>

      {item.runRecords && item.runRecords.length > 0 ? (
        <section className="rail-section">
          <div className="section-label"><Icon name="play" /> Storm-specific exact runs</div>
          <div className="run-record-list">
            {item.runRecords.map((run) => (
              <details key={run.runId}>
                <summary><span><strong>{run.storm}</strong><code>{run.runId}</code></span><b>{run.qa}</b></summary>
                <dl>
                  <div><dt>OUT1 event volume</dt><dd className="mono">{run.volume}</dd></div>
                  <div><dt>OUT1 peak</dt><dd className="mono">{run.peak}</dd></div>
                  <div><dt>Baseline peak</dt><dd className="mono">{run.baselinePeak}</dd></div>
                  <div><dt>Peak change</dt><dd className="mono">{run.peakDelta}</dd></div>
                  <div><dt>Input hash</dt><dd className="mono">{run.inputHash}</dd></div>
                  <div><dt>Output hash</dt><dd className="mono">{run.outputHash}</dd></div>
                  <div><dt>Report hash</dt><dd className="mono">{run.reportHash}</dd></div>
                </dl>
              </details>
            ))}
          </div>
        </section>
      ) : item.runIds && item.runIds.length > 0 && (
        <section className="rail-section">
          <div className="section-label"><Icon name="play" /> Exact run IDs</div>
          <ul className="run-id-list">{item.runIds.map((runId) => <li key={runId}><code>{runId}</code><span>recorded</span></li>)}</ul>
        </section>
      )}

      {item.changes && (
        <section className="rail-section">
          <div className="section-label"><Icon name="layers" /> Model changes</div>
          <ul className="change-list">
            {item.changes.map((change) => <li key={change} className="mono">{change}</li>)}
          </ul>
          <p className="rail-static-note"><Icon name="check" /> Complete typed change set shown above</p>
        </section>
      )}

      <section className="rail-section rail-source-chain">
        <div className="section-label"><Icon name="trace" /> Source chain</div>
        {exactEvidence ? (
          <ol>
            <li><span>01</span><div><strong>Materialized input</strong><small>Content-addressed and locally stored</small></div></li>
            <li><span>02</span><div><strong>Exact simulation</strong><small>Executed by the recorded SWMM binary</small></div></li>
            <li><span>03</span><div><strong>QA-classified metric</strong><small>Bound to QA and artifact fingerprints</small></div></li>
          </ol>
        ) : (
          <ol>
            <li><span>01</span><div><strong>Project record</strong><small>Project kind and Model Fitness Passport remain authoritative</small></div></li>
            <li><span>02</span><div><strong>Source or assumption</strong><small>Origin and limitations are shown above</small></div></li>
            <li><span>03</span><div><strong>Review status</strong><small>No hydraulic claim is inferred</small></div></li>
          </ol>
        )}
      </section>

      <section className="rail-section rail-explanation">
        <button className="secondary-button" aria-expanded={explanationOpen} onClick={() => setExplanationOpen((current) => !current)}><Icon name="spark" /> {explanationOpen ? "Hide explanation" : "Explain how to use this"}</button>
        {explanationOpen && <div role="status"><strong>Built-in guided explanation</strong><p>{explanationFor(item)}</p><small>Deterministic local copy · no model or cloud call</small></div>}
      </section>

      <section className="rail-section evidence-guide" aria-labelledby="evidence-guide-title">
        <div className="evidence-guide-heading">
          <div>
            <p className="section-label"><Icon name="spark" /> Read-only evidence guide</p>
            <h3 id="evidence-guide-title">Ask this record a question</h3>
          </div>
          <span className="guide-boundary">
            <Icon name="shield" />
            {guideAnswer?.weaveRecorded
              ? "Trace confirmed"
              : guideAvailable
                ? "Trace required"
                : "Guide off"}
          </span>
        </div>
        <p className="evidence-guide-intro">Ask what this evidence supports, why a rule passes or fails, and what still needs engineering review. The guide cannot change the model or choose an alternative.</p>

        <details className="guide-context-preview">
          <summary>Preview the evidence context</summary>
          <dl>
            <div><dt>Selected record</dt><dd><code>{item.id}</code> · {item.title}</dd></div>
            <div><dt>Worker may resolve</dt><dd>Linked exact runs, metrics, QA, hashes, assumptions, and project limitations</dd></div>
            <div><dt>Never included</dt><dd>Credentials, authorization headers, local paths, or console logs</dd></div>
            <div><dt>Trace content</dt><dd>{guideContentConsent ? "Allowlisted evidence content" : "Identifiers and operation metadata only"}</dd></div>
          </dl>
        </details>

        {guideCapabilitiesBusy ? (
          <div className="guide-state guide-state-checking" role="status">
            <span className="event-spinner" />
            <div><strong>Checking guarded access</strong><p>The worker is verifying the provider, project policy, and Weave tracing path.</p></div>
          </div>
        ) : !guideAvailable ? (
          <div className="guide-state guide-state-unavailable" role="status">
            <Icon name="info" />
            <div>
              <strong>{guideHeldBackByReleaseGate ? "Evidence Guide held back by release checks" : "Evidence Guide unavailable"}</strong>
              <p>{unavailableReason ?? guideCapabilities?.reason ?? "The required traced assistant path is not available."}</p>
              <small>{guideHeldBackByReleaseGate
                ? "No model call will be attempted. Use the built-in explanation and exact evidence while the model evaluation remains below the release threshold."
                : "Required: a worker-backed project, verified Restricted W&B binding, configured model provider, and available Weave tracing. Deterministic evidence remains usable."}</small>
              {onConfigureObservability && !guideHeldBackByReleaseGate && <button className="text-button" type="button" onClick={onConfigureObservability}>Review connection setup<Icon name="arrow" /></button>}
            </div>
          </div>
        ) : (
          <>
            <div className="guide-suggestions" aria-label="Suggested evidence questions">
              {guideSuggestions.map((suggestion) => (
                <button key={suggestion.id} type="button" onClick={() => setGuideQuestion(suggestion.question)}>
                  <span>{suggestion.label}</span><Icon name="arrow" />
                </button>
              ))}
            </div>

            {guideCapabilities?.requiresConsent && !guideCapabilities.inferenceConsent && (
              <label className="guide-consent">
                <input type="checkbox" checked={guideInferenceConsent} onChange={(event) => setGuideInferenceConsent(event.currentTarget.checked)} />
                <span><Icon name="check" /></span>
                <div><strong>Use the configured model for this evidence question</strong><small>The answer is returned only after its evidence checks and required Weave trace succeed.</small></div>
              </label>
            )}
            <label className="guide-content-consent">
              <input
                type="checkbox"
                checked={guideContentConsent}
                onChange={(event) => setGuideContentConsent(event.currentTarget.checked)}
                disabled={Boolean(guideConversation) || (projectKind === "demo" && guideCapabilities?.contentLoggingConsent === true)}
              />
              <span>Record allowlisted evidence content in Weave{guideCapabilities?.requiresContentLoggingConsent ? " (required)" : ""}</span>
              <small>{guideConversation
                ? "Fixed for this conversation."
                : projectKind === "demo" && guideCapabilities?.contentLoggingConsent
                  ? "Enabled for the fully synthetic Cedar Trace fixture."
                  : "Off keeps trace content to identifiers and operation metadata."}</small>
            </label>

            <form className="guide-question-form" onSubmit={(event) => { event.preventDefault(); void askEvidenceGuide(); }}>
              <label htmlFor={`evidence-guide-question-${item.id}`}>Question about {item.id}</label>
              <textarea
                id={`evidence-guide-question-${item.id}`}
                value={guideQuestion}
                onChange={(event) => setGuideQuestion(event.currentTarget.value)}
                placeholder="Ask what this record establishes—or where its limits begin."
                maxLength={500}
                rows={3}
                disabled={guideBusy}
              />
              <div>
                <small>{guideCapabilities?.provider ?? "Configured provider"}{guideCapabilities?.model ? ` · ${guideCapabilities.model}` : ""} · {guideQuestion.length}/500</small>
                <button className="primary-button" type="submit" disabled={!guideQuestion.trim() || !guideConsentReady || guideBusy}>
                  {guideBusy ? "Checking evidence…" : "Ask with evidence"}<Icon name="arrow" />
                </button>
              </div>
            </form>
          </>
        )}

        {guideError && <div className="guide-error" role="alert"><Icon name="warning" /><div><strong>No answer was shown</strong><p>{guideError}</p><small>An untraced or unvalidated response is never displayed.</small>{guideConversation?.weaveUrl && <button className="text-button" type="button" disabled={guideTraceBusy} onClick={() => void openWeaveConversation()}>{guideTraceBusy ? "Opening…" : "Open rejected investigation in Weave"}<Icon name="arrow" /></button>}</div></div>}

        {guideAnswer && (
          <article className="guide-answer" aria-label="Evidence Guide answer">
            <div className="guide-answer-topline">
              <span>Evidence note</span>
              {guideConversation && <code>{Math.max(guideConversation.turnCount, 1)} question{Math.max(guideConversation.turnCount, 1) === 1 ? "" : "s"}</code>}
            </div>
            <p className="guide-answer-summary">{guideAnswer.summary}</p>

            <div className="guide-answer-block">
              <strong>Supported by</strong>
              {guideAnswer.evidenceReferences.length > 0 ? (
                <div className="guide-evidence-chips">
                  {guideAnswer.evidenceReferences.map((reference) => (
                    <button key={reference.evidenceId} type="button" onClick={() => onEvidenceReference?.(reference)} title={`Open ${reference.evidenceId}`}>
                      <Icon name="trace" /><span>{reference.label}</span><code>{reference.evidenceId}</code>
                    </button>
                  ))}
                </div>
              ) : <p className="guide-empty-evidence">No evidence reference was returned. Treat this answer as unavailable.</p>}
            </div>

            <div className="guide-answer-block guide-limitations">
              <strong>Not established</strong>
              {guideAnswer.limitations.length > 0 ? <ul>{guideAnswer.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul> : <p>No explicit limitation was returned.</p>}
            </div>

            <div className="guide-answer-block guide-next-checks">
              <strong>Next useful check</strong>
              {guideAnswer.proposedActions.length > 0 ? <ul>{guideAnswer.proposedActions.map((action) => <li key={`${action.action}:${action.reason}`}><Icon name="arrow" /><span>{action.reason}</span></li>)}</ul> : <p>Review the source record and project limitations before using this result.</p>}
            </div>

            <div className="guide-proof-strip">
              <span className={guideChecksPassed ? "pass" : "fail"}><Icon name={guideChecksPassed ? "check" : "warning"} /> {guideChecksPassed ? "Evidence checks passed" : "Evidence checks not confirmed"}</span>
              <span className={guideAnswer.weaveRecorded ? "pass" : "fail"}><Icon name={guideAnswer.weaveRecorded ? "trace" : "warning"} /> {guideAnswer.weaveRecorded ? "Trace recorded in Weave" : "Trace not confirmed"}</span>
            </div>
            {guideAnswer.releaseLabel && (
              <small className="guide-release-basis">
                <Icon name="shield" /> {guideAnswer.releaseLabel}
              </small>
            )}

            <div className="guide-answer-actions">
              <div className="guide-feedback" aria-label="Rate this answer">
                <span>Was this useful?</span>
                {(["helpful", "incorrect", "unsafe"] as const).map((feedback) => (
                  <button key={feedback} type="button" className={guideFeedback === feedback ? "selected" : ""} disabled={guideFeedbackBusy || !guideTurnId} onClick={() => void recordGuideFeedback(feedback)}>{feedback === "helpful" ? "Helpful" : feedback === "incorrect" ? "Incorrect" : "Unsafe"}</button>
                ))}
              </div>
              {guideAnswer.weaveRecorded && <button className="text-button" type="button" disabled={guideTraceBusy} onClick={() => void openWeaveConversation()}>{guideTraceBusy ? "Opening…" : "Open conversation in Weave"}<Icon name="arrow" /></button>}
            </div>
            {guideFeedback && <small className="guide-feedback-recorded"><Icon name="check" /> {guideFeedback === "helpful" ? "Helpful" : guideFeedback === "incorrect" ? "Incorrect" : "Unsafe"} feedback recorded with this turn.</small>}
          </article>
        )}
      </section>

      <section className="rail-section rail-observability">
        <div className="section-label"><Icon name="trace" /> Cloud lineage mirror</div>
        <p><strong>{cloudLabel}</strong> · {cloudDetail}. W&amp;B/Weave never replaces this local evidence record.</p>
        <div className="button-row">
          {wandbUrl && <a href={wandbUrl} target="_blank" rel="noreferrer">Open W&amp;B project <Icon name="arrow" /></a>}
          {weaveUrl && <a href={weaveUrl} target="_blank" rel="noreferrer">Open Weave project <Icon name="arrow" /></a>}
        </div>
        {(wandbUrl || weaveUrl) && <small>These are project-level links. A claim-specific Run, artifact version, or trace link appears only after the worker resolves and stores that exact reference.</small>}
      </section>

      <section className="rail-section rail-artifacts">
        <div className="section-label"><Icon name="file" /> Retained artifacts</div>
        {item.artifacts && item.artifacts.length > 0 ? <ul>{item.artifacts.map((artifact) => <li key={`${artifact.label}:${artifact.path}`}><div><strong>{artifact.label}</strong><code>{artifact.path}</code></div><span className={artifact.available ? "available" : ""}>{artifact.available ? "Retained" : "Unavailable"}</span></li>)}</ul> : <p>No file artifact is associated with this source or assumption record.</p>}
        {item.artifactsAvailable && <small>Open/export actions are available only in a packaged project with an allowed filesystem root.</small>}
      </section>
    </aside>
  );
}

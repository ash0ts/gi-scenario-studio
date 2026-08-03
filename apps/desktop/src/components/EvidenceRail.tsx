import { useEffect, useRef, useState } from "react";
import {
  observabilityLabel,
  projectCloudSyncDetail,
  projectCloudSyncLabel,
  projectCloudWandbUrl,
  projectCloudWeaveUrl,
  type ProjectCloudSyncState,
} from "../lib/observability";
import type { EvidenceItem, ObservabilityState, ProjectKind } from "../types";
import { Icon } from "./Icons";

const notice = "Conceptual planning model — not for design, permitting, flood-risk determination, or representation of an existing drainage system.";

interface EvidenceRailProps {
  item: EvidenceItem;
  projectKind: ProjectKind;
  observability: ObservabilityState;
  projectCloudSync: ProjectCloudSyncState | null;
  open: boolean;
  onClose: () => void;
}

function explanationFor(item: EvidenceItem): string {
  if (item.status === "exact") {
    return "Use this record when you need to defend a displayed hydraulic value. The run IDs identify the exact storm executions; the hashes bind their inputs and outputs; QA says whether those executions were accepted. It does not prove calibration, regulatory acceptance, or real-world performance.";
  }
  if (item.kind === "assumption") {
    return "This record affects how alternatives are ranked or interpreted, but it is not a hydraulic observation. Confirm its source, units, vintage, and reviewer status before using it in professional work.";
  }
  if (item.kind === "storm") {
    return "This record defines one boundary condition. Compare its source and authority status before treating results as design or compliance evidence; this guided storm is synthetic and has no adopted recurrence interval.";
  }
  return "This record explains where the object or model-fitness claim came from. It helps you decide whether the model is fit for the stated screening use; it does not create a hydraulic result by itself.";
}

export function EvidenceRail({ item, projectKind, observability, projectCloudSync, open, onClose }: EvidenceRailProps) {
  const exactEvidence = item.status === "exact";
  const [explanationOpen, setExplanationOpen] = useState(false);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const cloudLabel = projectCloudSync ? projectCloudSyncLabel(projectCloudSync) : observabilityLabel(observability);
  const cloudDetail = projectCloudSync ? projectCloudSyncDetail(projectCloudSync) : "No project-level cloud status is available in this interface preview";
  const wandbUrl = projectCloudSync ? projectCloudWandbUrl(projectCloudSync) : observability.wandbUrl ?? null;
  const weaveUrl = projectCloudSync ? projectCloudWeaveUrl(projectCloudSync) : observability.weaveUrl ?? null;

  useEffect(() => {
    setExplanationOpen(false);
    if (open) window.requestAnimationFrame(() => headingRef.current?.focus({ preventScroll: true }));
  }, [item.id, open]);

  return (
    <aside className={`evidence-rail ${open ? "is-open" : ""}`} aria-label="Evidence details" aria-hidden={!open} aria-live="polite">
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
          <ul className="run-id-list">{item.runIds.map((runId) => <li key={runId}><code>{runId}</code><span>accepted</span></li>)}</ul>
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
            <li><span>03</span><div><strong>Accepted metric</strong><small>Bound to QA and artifact fingerprints</small></div></li>
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

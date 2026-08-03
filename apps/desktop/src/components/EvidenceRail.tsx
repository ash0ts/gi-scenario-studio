import { Icon } from "./Icons";
import type { EvidenceItem, Readiness } from "../types";

const notice = "Conceptual planning model — not for design, permitting, flood-risk determination, or representation of an existing drainage system.";

interface EvidenceRailProps {
  item: EvidenceItem;
  readiness: Readiness;
  open: boolean;
  onClose: () => void;
}

export function EvidenceRail({ item, readiness, open, onClose }: EvidenceRailProps) {
  const exactEvidence = item.status === "exact";
  return (
    <aside className={`evidence-rail ${open ? "is-open" : ""}`} aria-label="Evidence details" aria-hidden={!open}>
      <div className="evidence-topline">
        <div className="evidence-mark"><Icon name="trace" /></div>
        <span>Evidence rail</span>
        <button className="icon-button" onClick={onClose} aria-label="Close evidence rail"><Icon name="close" /></button>
      </div>

      <div className="evidence-heading">
        <span className={`status-dot status-${item.status}`} />
        <p className="eyebrow">{item.eyebrow}</p>
        <h2>{item.title}</h2>
        <p>{item.subtitle}</p>
      </div>

      {(readiness === "CONCEPTUAL" || readiness === "DEMO") && (
        <div className="rail-notice">
          <Icon name="warning" />
          <p>{readiness === "DEMO" ? "Teaching model — results are for learning and interface evaluation." : notice}</p>
        </div>
      )}

      <dl className="evidence-fields">
        {item.fields.map((field) => (
          <div key={field.label}>
            <dt>{field.label}</dt>
            <dd className={field.mono ? "mono" : ""}>{field.value}</dd>
          </div>
        ))}
      </dl>

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
            <li><span>03</span><div><strong>Extracted metric</strong><small>Bound to accepted QA and artifact hashes</small></div></li>
          </ol>
        ) : (
          <ol>
            <li><span>01</span><div><strong>Project record</strong><small>Readiness and state remain authoritative</small></div></li>
            <li><span>02</span><div><strong>Source or assumption</strong><small>Origin and limitations are shown above</small></div></li>
            <li><span>03</span><div><strong>Review status</strong><small>No hydraulic claim is inferred from this record</small></div></li>
          </ol>
        )}
      </section>

      <div className="rail-footer">
        <div className={`rail-artifact-status ${item.artifactsAvailable ? "available" : ""}`}>
          <Icon name="file" /> {item.artifactsAvailable ? "Artifacts retained in repository evidence set" : "No run artifacts"}
        </div>
      </div>
    </aside>
  );
}

import { useState } from "react";
import {
  aggregateMetric,
  candidateSlotErrors,
  candidateSlotPayload,
  designVectorErrors,
  exactJobArithmetic,
  type AnalysisRunPlan,
  type CandidateSlotDraft,
  type CandidateSlotPayload,
  type DecisionBoard,
  type DecisionDesign,
  type EvidenceExportResult,
  type ExactJobSummary,
  type ExportEligibilityResult,
  type ModelValidationResult,
  type PractitionerStormCase,
} from "../lib/practitioner";
import type { ProjectSummary, StepId } from "../types";
import { Icon } from "./Icons";

function PageHeading({ eyebrow, title, copy, action }: { eyebrow: string; title: string; copy: string; action?: React.ReactNode }) {
  return <header className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1 data-screen-title tabIndex={-1}>{title}</h1><p>{copy}</p></div>{action && <div className="heading-action">{action}</div>}</header>;
}

const errorText = (error?: string | null) => error ? <div className="practitioner-error" role="alert"><Icon name="warning" /><span>{error}</span></div> : null;

export function PractitionerModelScreen({
  project,
  validation,
  passport,
  busy,
  fitnessBusy,
  error,
  fitnessError,
  onValidate,
  onAttest,
  onEvidence,
  onStep,
}: {
  project: ProjectSummary;
  validation: ModelValidationResult | null;
  passport: Record<string, unknown> | null | undefined;
  busy: boolean;
  fitnessBusy: boolean;
  error: string | null;
  fitnessError: string | null;
  onValidate: () => void;
  onAttest: (draft: FitnessAttestationDraft) => void;
  onEvidence: () => void;
  onStep: (step: StepId) => void;
}) {
  const bundle = project.bundle;
  const canContinue = Boolean(validation?.structurally_runnable && validation.blocking_count === 0);
  const issues = validation?.issues ?? [];
  const [attestationOpen, setAttestationOpen] = useState(false);
  const [reviewer, setReviewer] = useState("");
  const [intendedUse, setIntendedUse] = useState("GI alternatives screening for field and feasibility review");
  const [disposition, setDisposition] = useState<FitnessAttestationDraft["disposition"]>("fit_with_limitations");
  const [limitations, setLimitations] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const canAttest = canContinue && Boolean(project.bundle?.currentModelRevisionId && reviewer.trim() && intendedUse.trim() && confirmed && (disposition === "not_fit" || limitations.trim()));
  return <div className="screen-stack practitioner-screen">
    <PageHeading
      eyebrow="Model / authoritative preflight"
      title="Check the imported model before changing it"
      copy="The worker reparses the project-contained INP and reports object-level blocking findings, review findings, and corrective actions. Structural acceptance is not calibration or a fitness attestation."
      action={<button className="secondary-button" onClick={onEvidence}><Icon name="trace" /> Inspect import receipt</button>}
    />
    <section className="model-file-strip">
      <div className="file-glyph"><Icon name="file" /></div>
      <div><strong>{bundle?.baseModel ?? bundle?.models[0]?.modelPath ?? "No selected model"}</strong><span>{bundle?.displayName ?? "No portable bundle"} · {bundle?.fileCount ?? 0} hashed files</span></div>
      <div className="file-hash"><span>Bundle SHA-256</span><code>{bundle ? `${bundle.sha256.slice(0, 16)}…` : "unavailable"}</code></div>
      <button className="text-button" onClick={onValidate} disabled={busy || !bundle?.models.length}>{busy ? "Checking…" : validation ? "Run checks again" : "Run model checks"} <Icon name="chevron" /></button>
    </section>
    {errorText(error)}
    <div className="practitioner-summary-grid">
      <section className="paper-panel practitioner-status-panel">
        <div className="panel-heading"><div><p className="eyebrow">Worker result</p><h2>{validation ? validation.structurally_runnable ? "Structurally runnable" : "Blocking findings remain" : "Checks not run in this session"}</h2></div><span className={`score-ring ${canContinue ? "ready" : ""}`}>{validation ? validation.blocking_count : "—"}</span></div>
        <dl className="practitioner-facts">
          <div><dt>Blocking</dt><dd className="mono">{validation?.blocking_count ?? "Not checked"}</dd></div>
          <div><dt>Engineer review</dt><dd className="mono">{validation?.warning_count ?? "Not checked"}</dd></div>
          <div><dt>Sections parsed</dt><dd className="mono">{validation?.section_names.length ?? "Not checked"}</dd></div>
          <div><dt>External dependency closure</dt><dd>{bundle?.dependencyComplete ? `${bundle.dependencyCount} references resolved` : "Incomplete"}</dd></div>
        </dl>
        <div className="rail-notice"><Icon name="info" /><p>Passing parser checks means the model is structurally runnable. It does not establish calibration, reference matching, intended-use fitness, or regulatory acceptance.</p></div>
      </section>
      <section className="paper-panel practitioner-status-panel">
        <div className="panel-heading"><div><p className="eyebrow">Imported variants</p><h2>{bundle?.models.length ?? 0} project-contained INP file{bundle?.models.length === 1 ? "" : "s"}</h2></div><span className="mini-state review">Immutable source</span></div>
        <ul className="compact-file-list">{bundle?.models.map((model) => <li key={model.projectPath}><Icon name="file" /><div><strong>{model.modelPath}</strong><small>{Object.keys(model.dependencies).length} isolated-run dependenc{Object.keys(model.dependencies).length === 1 ? "y" : "ies"}</small></div></li>)}</ul>
      </section>
    </div>
    <section className="paper-panel practitioner-table-panel">
      <div className="panel-heading"><div><p className="eyebrow">Preflight findings</p><h2>{validation ? `${issues.length} findings` : "Run checks to see object-level evidence"}</h2></div></div>
      <div className="table-wrap"><table><thead><tr><th>Level</th><th>Location</th><th>Finding</th><th>Corrective action</th></tr></thead><tbody>
        {issues.map((issue, index) => <tr key={`${issue.code}-${issue.object_id ?? index}`}><td><span className={`mini-state ${issue.severity === "blocking" ? "notice" : issue.severity === "warning" ? "review" : "pass"}`}>{issue.severity}</span></td><td className="mono">{[issue.section, issue.object_id].filter(Boolean).join(" / ") || "Model"}</td><td>{issue.message}</td><td>{issue.remediation ?? "Review in EPA SWMM."}</td></tr>)}
        {validation && !issues.length && <tr><td colSpan={4}><div className="table-empty"><strong>No parser findings</strong><span>Continue to confirm which imported models represent the storm cases.</span></div></td></tr>}
        {!validation && <tr><td colSpan={4}><div className="table-empty"><strong>No current validation record</strong><span>Run worker checks against the project-contained model.</span></div></td></tr>}
      </tbody></table></div>
    </section>
    <section className="paper-panel practitioner-status-panel fitness-attestation-panel">
      <div className="panel-heading"><div><p className="eyebrow">Independent Model Fitness Passport</p><h2>{passport === undefined ? "Reading repository evidence…" : passport ? String(passport.disposition ?? "Stored") .replaceAll("_", " ") : "No passport stored"}</h2></div><button className="secondary-button" onClick={() => setAttestationOpen((open) => !open)} disabled={!canContinue || !project.bundle?.currentModelRevisionId}>{attestationOpen ? "Close attestation" : "Record engineer attestation"}</button></div>
      <p className="fitness-attestation-copy">Structural validation does not advance fitness automatically. A named engineer may record either fit-with-limitations or not-fit for the active immutable revision. This action does not approve a GI alternative, accept run QA, or create a design recommendation.</p>
      {fitnessError && errorText(fitnessError)}
      {attestationOpen && <div className="fitness-attestation-form">
        <div className="practitioner-form-grid">
          <label>Engineer name<input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label>
          <label>Disposition<select value={disposition} onChange={(event) => setDisposition(event.target.value as FitnessAttestationDraft["disposition"])}><option value="fit_with_limitations">Fit with stated limitations</option><option value="not_fit">Not fit for stated use</option></select></label>
          <label className="form-span">Intended use<textarea value={intendedUse} onChange={(event) => setIntendedUse(event.target.value)} /></label>
          <label className="form-span">Unresolved limitations {disposition === "not_fit" ? <small>recommended</small> : <small>required</small>}<textarea value={limitations} onChange={(event) => setLimitations(event.target.value)} placeholder="One limitation per line: calibration status, reference reproduction, rainfall authority, boundary conditions, sensitivity…" /></label>
        </div>
        <label className="review-check practitioner-attestation"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span><Icon name="check" /></span>I am the identified reviewer and attest this disposition only for the stated use and active model revision.</label>
        <div className="button-row"><button className="primary-button" disabled={!canAttest || fitnessBusy} onClick={() => onAttest({ reviewer: reviewer.trim(), intendedUse: intendedUse.trim(), disposition, limitations: limitations.split(/\r?\n/).map((item) => item.trim()).filter(Boolean) })}>{fitnessBusy ? "Recording…" : "Record versioned attestation"}</button><span className="footer-progress"><Icon name="info" /> Fit-for-stated-use is withheld until a richer evidence capture supports it.</span></div>
      </div>}
    </section>
    <div className="screen-footer-action"><span className="footer-progress"><Icon name={canContinue ? "check" : "info"} /> {canContinue ? "Zero blocking structural findings" : "A zero-blocker worker report is required"}</span><button className="primary-button" disabled={!canContinue} onClick={() => onStep("storms")}>Confirm storm scope <Icon name="arrow" /></button></div>
  </div>;
}

export interface FitnessAttestationDraft {
  reviewer: string;
  intendedUse: string;
  disposition: "fit_with_limitations" | "not_fit";
  limitations: string[];
}

export function PractitionerStormsScreen({
  storms,
  busy,
  error,
  onChange,
  onValidate,
  onStep,
}: {
  storms: PractitionerStormCase[];
  busy: boolean;
  error: string | null;
  onChange: (index: number, patch: Partial<PractitionerStormCase>) => void;
  onValidate: () => void;
  onStep: (step: StepId) => void;
}) {
  const selected = storms.filter((storm) => storm.included);
  const duplicateIds = new Set(selected.filter((storm, index) => selected.findIndex((candidate) => candidate.stormId.toLocaleLowerCase() === storm.stormId.toLocaleLowerCase()) !== index).map((storm) => storm.stormId));
  const ready = selected.length > 0 && !duplicateIds.size && selected.every((storm) => storm.stormId.trim() && storm.validation?.blocking_count === 0);
  return <div className="screen-stack practitioner-screen">
    <PageHeading
      eyebrow={`Storms / ${selected.length} selected`}
      title="Confirm the exact model used for each storm"
      copy="Each checked INP becomes one immutable storm case. The worker copies its resolved dependencies into an isolated run directory and reparses every materialized alternative before execution."
      action={<button className="secondary-button" onClick={onValidate} disabled={busy || !selected.length}><Icon name="shield" /> {busy ? "Validating selected models…" : "Validate selected models"}</button>}
    />
    {errorText(error)}
    <section className="paper-panel practitioner-table-panel">
      <div className="panel-heading"><div><p className="eyebrow">Imported storm variants</p><h2>Select only models that belong in this comparison</h2></div><span className="mini-state notice">Engineer confirmation</span></div>
      <div className="table-wrap"><table><thead><tr><th>Use</th><th>Storm ID</th><th>Project-contained model</th><th>Dependencies</th><th>Authority</th><th>Worker checks</th></tr></thead><tbody>
        {storms.map((storm, index) => <tr key={storm.modelPath} className={storm.included ? "selected-row" : ""}>
          <td><input type="checkbox" checked={storm.included} onChange={(event) => onChange(index, { included: event.target.checked, validation: undefined })} aria-label={`Use ${storm.displayPath} as a storm case`} /></td>
          <td><label className="table-field"><span className="sr-only">Storm ID for {storm.displayPath}</span><input value={storm.stormId} disabled={!storm.included} onChange={(event) => onChange(index, { stormId: event.target.value, validation: undefined })} aria-invalid={duplicateIds.has(storm.stormId)} /></label>{duplicateIds.has(storm.stormId) && <small className="field-error">Use a unique ID</small>}</td>
          <td><strong>{storm.displayPath}</strong><small className="table-subcopy">{storm.source}</small></td>
          <td className="mono">{Object.keys(storm.dependencies).length}</td>
          <td><span className="mini-state review">{storm.authorityStatus.replace("_", " ")}</span></td>
          <td>{!storm.included ? "Excluded" : !storm.validation ? <span className="mini-state review">Not checked</span> : storm.validation.blocking_count ? <span className="mini-state notice">{storm.validation.blocking_count} blocking</span> : <span className="mini-state pass">Accepted structurally</span>}</td>
        </tr>)}
      </tbody></table></div>
      <p className="table-note">Storm IDs label exact jobs; they do not assign recurrence intervals or regulatory authority. Imported source metadata remains user-defined until a richer storm-provenance write contract is available.</p>
    </section>
    <section className="paper-panel adverse-strip"><Icon name="info" /><div><strong>One model can be one valid event case</strong><span>If the INP already contains its rainfall and simulation window, selecting that model creates one exact baseline job plus one job per selected alternative. Multiple INPs should differ only in intended storm inputs and reviewed boundary conditions.</span></div></section>
    <div className="screen-footer-action"><span className="footer-progress"><Icon name={ready ? "check" : "info"} /> {ready ? `${selected.length} structurally accepted storm case${selected.length === 1 ? "" : "s"}` : "Select, label, and validate at least one storm model"}</span><button className="primary-button" disabled={!ready} onClick={() => onStep("opportunities")}>Define feasible GI slots <Icon name="arrow" /></button></div>
  </div>;
}

const candidateField = <K extends keyof CandidateSlotDraft>(
  draft: CandidateSlotDraft,
  key: K,
  onChange: (patch: Partial<CandidateSlotDraft>) => void,
  props: React.InputHTMLAttributes<HTMLInputElement> = {},
) => <input {...props} value={String(draft[key])} onChange={(event) => onChange({ [key]: event.target.value } as Partial<CandidateSlotDraft>)} />;

export function PractitionerOpportunitiesScreen({
  candidates,
  validation,
  onAdd,
  onChange,
  onRemove,
  onStep,
}: {
  candidates: CandidateSlotDraft[];
  validation: ModelValidationResult | null;
  onAdd: () => void;
  onChange: (index: number, patch: Partial<CandidateSlotDraft>) => void;
  onRemove: (index: number) => void;
  onStep: (step: StepId) => void;
}) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const selected = candidates[selectedIndex];
  const errors = candidateSlotErrors(candidates);
  const setupOptions = validation?.setup_options;
  const updateIdentity = (patch: Partial<CandidateSlotDraft>) => {
    if (!selected) return;
    const subcatchmentId = patch.subcatchmentId ?? selected.subcatchmentId;
    const lidControl = patch.lidControl ?? selected.lidControl;
    const usage = setupOptions?.existing_lid_usage.find((item) => item.subcatchment_id === subcatchmentId && item.lid_control === lidControl);
    onChange(selectedIndex, {
      ...patch,
      ...(usage?.unit_count !== null && usage?.unit_count !== undefined ? { baseUnits: String(usage.unit_count) } : {}),
    });
  };
  return <div className="screen-stack practitioner-screen">
    <PageHeading
      eyebrow={`Opportunities / ${candidates.length} candidate slots`}
      title="Approve only GI changes already defined in the model"
      copy="The worker never invents LID layers or hydraulic parameters. Enter an existing subcatchment and immutable LID control, then bound only the number of additional identical units."
      action={<button className="secondary-button" onClick={() => { onAdd(); setSelectedIndex(candidates.length); }}><Icon name="plus" /> Add candidate slot</button>}
    />
    <div className="practitioner-editor-grid">
      <section className="paper-panel candidate-ledger-panel">
        <div className="panel-heading"><div><p className="eyebrow">Candidate ledger</p><h2>{candidates.length ? "Select a slot to review" : "No GI changes approved"}</h2></div></div>
        <div className="candidate-list">{candidates.map((candidate, index) => <button key={`${candidate.slotId}-${index}`} className={selectedIndex === index ? "active" : ""} onClick={() => setSelectedIndex(index)}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{candidate.slotId || "Unnamed slot"}</strong><small>{candidate.subcatchmentId || "Subcatchment required"} · {candidate.lidControl || "LID control required"}</small></div><i className={candidate.engineerApproved ? "approved" : ""} /></button>)}</div>
        {!candidates.length && <div className="table-empty"><strong>Add the first candidate</strong><span>Use only locations and controls reviewed for this screening question.</span></div>}
      </section>
      <section className="paper-panel candidate-form-panel">
        {selected ? <>
          <div className="panel-heading"><div><p className="eyebrow">Selected candidate</p><h2>{selected.slotId || "Unnamed slot"}</h2></div><button className="text-button caution-text" onClick={() => { onRemove(selectedIndex); setSelectedIndex(Math.max(0, selectedIndex - 1)); }}>Remove</button></div>
          <div className="practitioner-form-grid">
            <label>Slot ID{candidateField(selected, "slotId", (patch) => onChange(selectedIndex, patch))}</label>
            <label>Subcatchment ID{setupOptions?.subcatchment_ids.length ? <select value={selected.subcatchmentId} onChange={(event) => updateIdentity({ subcatchmentId: event.target.value })}><option value="">Select parsed object…</option>{setupOptions.subcatchment_ids.map((id) => <option key={id} value={id}>{id}</option>)}</select> : candidateField(selected, "subcatchmentId", (patch) => onChange(selectedIndex, patch))}</label>
            <label>Existing LID control ID{setupOptions?.lid_control_ids.length ? <select value={selected.lidControl} onChange={(event) => updateIdentity({ lidControl: event.target.value })}><option value="">Select parsed control…</option>{setupOptions.lid_control_ids.map((id) => <option key={id} value={id}>{id}</option>)}</select> : candidateField(selected, "lidControl", (patch) => onChange(selectedIndex, patch))}</label>
            <label>LID type <small>optional classification</small><select value={selected.lidType} onChange={(event) => onChange(selectedIndex, { lidType: event.target.value as CandidateSlotDraft["lidType"] })}><option value="">Select only if confirmed…</option><option value="BC">Bioretention cell</option><option value="RG">Rain garden</option><option value="GR">Green roof</option><option value="IT">Infiltration trench</option><option value="PP">Permeable pavement</option><option value="RB">Rain barrel / cistern</option><option value="RD">Rooftop disconnection</option><option value="VS">Vegetative swale</option></select></label>
            <label>Existing units{candidateField(selected, "baseUnits", (patch) => onChange(selectedIndex, patch), { inputMode: "numeric" })}</label>
            <label>Additional-unit range<div className="inline-number-pair">{candidateField(selected, "minimumAdditionalUnits", (patch) => onChange(selectedIndex, patch), { inputMode: "numeric", "aria-label": "Minimum additional units" })}<span>to</span>{candidateField(selected, "maximumAdditionalUnits", (patch) => onChange(selectedIndex, patch), { inputMode: "numeric", "aria-label": "Maximum additional units" })}</div></label>
            <label>Unit area <small>model area units</small>{candidateField(selected, "unitArea", (patch) => onChange(selectedIndex, patch), { inputMode: "decimal" })}</label>
            <label>Surface width <small>model length units</small>{candidateField(selected, "surfaceWidth", (patch) => onChange(selectedIndex, patch), { inputMode: "decimal" })}</label>
            <label>From impervious{candidateField(selected, "fromImperviousPercent", (patch) => onChange(selectedIndex, patch), { inputMode: "decimal" })}<small>%</small></label>
            <label>Initial saturation{candidateField(selected, "initialSaturationPercent", (patch) => onChange(selectedIndex, patch), { inputMode: "decimal" })}<small>%</small></label>
            <label>Drain-to object <small>optional</small>{candidateField(selected, "drainTo", (patch) => onChange(selectedIndex, patch))}</label>
            <label>Displaced surface area{candidateField(selected, "displacedSurfaceArea", (patch) => onChange(selectedIndex, patch), { inputMode: "decimal" })}</label>
            <label className="plain-checkbox"><input type="checkbox" checked={selected.fromPervious} onChange={(event) => onChange(selectedIndex, { fromPervious: event.target.checked })} />Receive runoff from the pervious area</label>
            <label className="plain-checkbox"><input type="checkbox" checked={selected.routeToPervious} onChange={(event) => onChange(selectedIndex, { routeToPervious: event.target.checked })} />Route treated outflow to the pervious area</label>
            <label className="form-span">Feasibility basis<textarea value={selected.feasibilityBasis} onChange={(event) => onChange(selectedIndex, { feasibilityBasis: event.target.value })} placeholder="Record the reviewed footprint, source, known conflicts, and conditions." /></label>
          </div>
          <label className="review-check practitioner-attestation"><input type="checkbox" checked={selected.engineerApproved} onChange={(event) => onChange(selectedIndex, { engineerApproved: event.target.checked })} /><span><Icon name="check" /></span>I confirm this exact subcatchment/control pair and its bounds are approved for screening.</label>
        </> : <div className="table-empty"><Icon name="layers" /><strong>No candidate selected</strong><span>Add a reviewed slot to expose its exact engineering fields.</span></div>}
      </section>
    </div>
    {errors.length > 0 && <section className="paper-panel practitioner-blockers" aria-live="polite"><p className="eyebrow">Setup findings</p><ul>{errors.map((message) => <li key={message}>{message}</li>)}</ul></section>}
    <div className="screen-footer-action"><span className="footer-progress"><Icon name={errors.length ? "info" : "check"} /> {errors.length ? `${errors.length} candidate finding${errors.length === 1 ? "" : "s"}` : `${candidates.length} engineer-approved exact mutation slot${candidates.length === 1 ? "" : "s"}`}</span><button className="primary-button" disabled={errors.length > 0} onClick={() => onStep("goals")}>Define exact alternatives <Icon name="arrow" /></button></div>
  </div>;
}

export function PractitionerGoalsScreen({
  slots,
  vectors,
  budget,
  adverseTolerance,
  onVectors,
  onBudget,
  onAdverseTolerance,
  onStep,
}: {
  slots: CandidateSlotPayload[];
  vectors: number[][];
  budget: number;
  adverseTolerance: number;
  onVectors: (vectors: number[][]) => void;
  onBudget: (value: number) => void;
  onAdverseTolerance: (value: number) => void;
  onStep: (step: StepId) => void;
}) {
  const errors = designVectorErrors(vectors, slots);
  const arithmetic = exactJobArithmetic(0, vectors);
  const addVector = () => onVectors([...vectors, slots.map((slot) => slot.min_additional_units)]);
  return <div className="screen-stack practitioner-screen">
    <PageHeading
      eyebrow="Goals / exact test set"
      title="Choose the alternatives SWMM will actually execute"
      copy="Each row is a complete integer deployment vector. The untouched zero vector is always added as the baseline; no surrogate value appears in the decision board."
      action={<button className="secondary-button" onClick={addVector}><Icon name="plus" /> Add exact alternative</button>}
    />
    <section className="decision-rule-banner paper-panel"><span className="rule-number">AVAILABLE NOW</span><div><strong>Direct hydraulics and full-network adverse peak screening</strong><p>The worker reports exact event metrics and adverse peak changes. Cost contracts, threshold evaluation, and an accepted Decision Brief do not yet have a complete practitioner write path, so this release will not name a preferred option.</p></div><span className="mini-state review">No ranking claim</span></section>
    <section className="paper-panel practitioner-table-panel">
      <div className="panel-heading"><div><p className="eyebrow">Exact design vectors</p><h2>{vectors.length} proposed alternative{vectors.length === 1 ? "" : "s"}</h2></div><span className="mini-state notice">Baseline added automatically</span></div>
      <div className="table-wrap"><table><thead><tr><th>Alternative</th>{slots.map((slot) => <th key={slot.slot_id}>{slot.slot_id}<small className="table-subcopy">{slot.min_additional_units}–{slot.max_additional_units}</small></th>)}<th><span className="sr-only">Actions</span></th></tr></thead><tbody>
        {vectors.map((vector, vectorIndex) => <tr key={vectorIndex}><td><strong>ALT {String(vectorIndex + 1).padStart(2, "0")}</strong></td>{slots.map((slot, slotIndex) => <td key={slot.slot_id}><input className="vector-input" type="number" min={slot.min_additional_units} max={slot.max_additional_units} step="1" value={vector[slotIndex]} aria-label={`ALT ${vectorIndex + 1} ${slot.slot_id} additional units`} onChange={(event) => onVectors(vectors.map((row, rowIndex) => rowIndex === vectorIndex ? row.map((value, columnIndex) => columnIndex === slotIndex ? Number(event.target.value) : value) : row))} /></td>)}<td><button className="icon-button" aria-label={`Remove alternative ${vectorIndex + 1}`} onClick={() => onVectors(vectors.filter((_, index) => index !== vectorIndex))}>×</button></td></tr>)}
        {!vectors.length && <tr><td colSpan={slots.length + 2}><div className="table-empty"><strong>No alternatives selected</strong><span>Add one row and enter an approved unit count for each slot.</span></div></td></tr>}
      </tbody></table></div>
    </section>
    <div className="practitioner-summary-grid">
      <section className="paper-panel practitioner-status-panel"><p className="eyebrow">Execution guardrails</p><h2>Bound the computation before planning</h2><div className="practitioner-form-grid compact"><label>Maximum direct SWMM jobs<input type="number" min="1" max="5000" step="1" value={budget} onChange={(event) => onBudget(Number(event.target.value))} /></label><label>Adverse peak tolerance <small>model flow units</small><input type="number" min="0" step="0.001" value={adverseTolerance} onChange={(event) => onAdverseTolerance(Number(event.target.value))} /></label></div><p className="fine-print">The run planner enforces the approved solver-job cap. The adverse tolerance affects only which exact peak increases are flagged on Compare.</p></section>
      <section className="paper-panel practitioner-status-panel"><p className="eyebrow">Candidate accounting</p><h2>{arithmetic.alternativeDesigns} unique non-baseline vector{arithmetic.alternativeDesigns === 1 ? "" : "s"}</h2><p>Storm multiplication is shown on the next screen after the selected models and vectors are combined.</p><div className="rail-notice"><Icon name="info" /><p>Seed 42 and one worker are recorded. Search method: engineer-selected exact set.</p></div></section>
    </div>
    {errors.length > 0 && <section className="paper-panel practitioner-blockers"><p className="eyebrow">Vector findings</p><ul>{errors.map((message) => <li key={message}>{message}</li>)}</ul></section>}
    <div className="screen-footer-action"><span className="footer-progress"><Icon name={errors.length ? "info" : "check"} /> {errors.length ? "Resolve the exact-vector findings" : "Every proposed alternative is bounded and unique"}</span><button className="primary-button" disabled={errors.length > 0 || !Number.isInteger(budget) || budget < 1 || budget > 5000 || !Number.isFinite(adverseTolerance) || adverseTolerance < 0} onClick={() => onStep("run")}>Review exact run arithmetic <Icon name="arrow" /></button></div>
  </div>;
}

export type PractitionerRunPhase = "unplanned" | "planning" | "ready" | "starting" | "running" | "pause_requested" | "paused" | "cancel_requested" | "cancelled" | "completed" | "failed";

export interface PractitionerRunRuntime extends ExactJobSummary {
  phase: PractitionerRunPhase;
  analysisId?: string;
  runPlan?: AnalysisRunPlan;
  message: string;
  error?: string;
}

export function PractitionerRunScreen({
  stormCount,
  vectorCount,
  budget,
  runtime,
  busy,
  onPlan,
  onStart,
  onPause,
  onResume,
  onCancel,
  onStep,
}: {
  stormCount: number;
  vectorCount: number;
  budget: number;
  runtime: PractitionerRunRuntime;
  busy: boolean;
  onPlan: () => void;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onStep: (step: StepId) => void;
}) {
  const arithmetic = runtime.runPlan ?? { baseline_jobs: stormCount, candidate_designs: vectorCount, storm_count: stormCount, total_exact_jobs: stormCount + vectorCount * stormCount };
  const progress = runtime.totalJobs ? Math.min(1, runtime.completedJobs / runtime.totalJobs) : 0;
  const active = ["starting", "running", "pause_requested", "cancel_requested"].includes(runtime.phase);
  const mayCompare = Boolean(runtime.analysisId && (runtime.succeededJobs > 0 || runtime.phase === "completed"));
  return <div className="screen-stack practitioner-screen">
    <PageHeading eyebrow="Run / direct SWMM jobs" title="Approve the arithmetic, then execute the durable plan" copy="Planning writes every design, storm, job, cache key, and immutable mutation to the local repository before one solver process starts." />
    <section className="run-hero" style={{ "--progress": `${Math.round(progress * 100)}%` } as React.CSSProperties}>
      <div className="run-dial"><div><strong>{runtime.phase === "unplanned" ? "PLAN" : `${Math.round(progress * 100)}%`}</strong><span>{runtime.phase.replaceAll("_", " ")}</span></div></div>
      <div className="run-copy"><p className="eyebrow">Authoritative local state</p><h2>{runtime.runPlan ? <>{runtime.runPlan.total_exact_jobs} <span>direct jobs</span></> : "No durable plan yet"}</h2><p>{runtime.message}</p>{runtime.error && <p className="run-error" role="alert">{runtime.error}</p>}</div>
      <div className="run-controls">
        {runtime.phase === "unplanned" && <button className="primary-button" onClick={onPlan} disabled={busy || arithmetic.total_exact_jobs > budget}>{busy ? "Creating plan…" : "Create durable run plan"}</button>}
        {runtime.phase === "failed" && !runtime.analysisId && <button className="primary-button" onClick={onPlan} disabled={busy || arithmetic.total_exact_jobs > budget}>{busy ? "Creating plan…" : "Retry durable planning"}</button>}
        {runtime.phase === "ready" && <button className="primary-button" onClick={onStart} disabled={busy}>{busy ? "Starting…" : "Run exact jobs"}</button>}
        {(runtime.phase === "running" || runtime.phase === "pause_requested") && <div className="run-control-row"><button className="secondary-button" onClick={onPause} disabled={busy || runtime.phase === "pause_requested"}>Pause</button><button className="secondary-button" onClick={onCancel} disabled={busy}>Cancel</button></div>}
        {runtime.phase === "paused" && <div className="run-control-row"><button className="primary-button" onClick={onResume} disabled={busy}>Resume</button><button className="secondary-button" onClick={onCancel} disabled={busy}>Cancel</button></div>}
        {runtime.phase === "cancel_requested" && <button className="secondary-button" disabled>Cancellation requested</button>}
        {(runtime.phase === "completed" || runtime.phase === "failed" || runtime.phase === "cancelled") && <button className="primary-button" onClick={() => onStep("compare")} disabled={!mayCompare}>Inspect exact evidence</button>}
        <small>W&amp;B synchronization is isolated from candidate order, solver output, QA, and local evidence.</small>
      </div>
    </section>
    <section className="run-stats"><div><span>Untouched baselines</span><strong className="mono">{arithmetic.baseline_jobs}</strong><small>one per storm</small></div><div><span>Alternatives</span><strong className="mono">{arithmetic.candidate_designs}</strong><small>explicit vectors</small></div><div><span>Storms per alternative</span><strong className="mono">{arithmetic.storm_count}</strong><small>complete ensembles</small></div><div><span>Exact jobs</span><strong className="mono">{arithmetic.total_exact_jobs}</strong><small>cap {budget}</small></div></section>
    <div className="practitioner-summary-grid">
      <section className="paper-panel practitioner-status-panel"><div className="panel-heading"><div><p className="eyebrow">Job ledger</p><h2>{runtime.completedJobs} of {runtime.totalJobs || arithmetic.total_exact_jobs} terminal</h2></div><span className={`live-indicator ${active ? "active" : ""}`}>{active ? "Worker active" : runtime.phase}</span></div><dl className="practitioner-facts"><div><dt>Succeeded</dt><dd className="mono">{runtime.succeededJobs}</dd></div><div><dt>QA rejected</dt><dd className="mono">{runtime.rejectedJobs}</dd></div><div><dt>Failed</dt><dd className="mono">{runtime.failedJobs}</dd></div><div><dt>Cancelled</dt><dd className="mono">{runtime.cancelledJobs}</dd></div></dl></section>
      <section className="paper-panel practitioner-status-panel"><p className="eyebrow">Reportable outputs</p><h2>What Compare may claim</h2><ul className="plain-check-list">{(runtime.runPlan?.reportable_outputs ?? ["Exact direct-report hydraulic metrics", "Full-network adverse peak scan", "INP, RPT, OUT, hashes, and QA evidence"]).map((item) => <li key={item}><Icon name="check" />{item}</li>)}</ul></section>
    </div>
    {arithmetic.total_exact_jobs > budget && <section className="paper-panel practitioner-blockers"><p className="eyebrow">Run-plan blocker</p><ul><li>{arithmetic.total_exact_jobs} jobs exceed the approved cap of {budget}. Reduce storms or exact alternatives.</li></ul></section>}
  </div>;
}

const formatMetric = (metric?: { value: number | null; units: string }) => metric?.value === null || metric?.value === undefined ? "—" : `${metric.value.toLocaleString(undefined, { maximumFractionDigits: 5 })} ${metric.units}`;
const deltaFrom = (baseline?: { value: number | null }, alternative?: { value: number | null }) => {
  if (baseline?.value === null || baseline?.value === undefined || alternative?.value === null || alternative?.value === undefined || baseline.value === 0) return "—";
  const value = ((alternative.value - baseline.value) / baseline.value) * 100;
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
};

export function PractitionerCompareScreen({
  board,
  loading,
  error,
  onRefresh,
  onEvidence,
  onStep,
}: {
  board: DecisionBoard | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onEvidence: (jobId: string) => void;
  onStep: (step: StepId) => void;
}) {
  const baselineVolume = board?.baseline ? aggregateMetric(board.baseline, "outfall_volume") : undefined;
  const baselinePeak = board?.baseline ? aggregateMetric(board.baseline, "outfall_peak") : undefined;
  const traceButtons = (design: DecisionDesign) => Object.entries(design.job_ids).map(([stormId, jobId]) => <button key={jobId} className="job-chip" onClick={() => onEvidence(jobId)}>{stormId} <Icon name="trace" /></button>);
  return <div className="screen-stack practitioner-screen compare-screen">
    <PageHeading eyebrow="Compare / exact evidence" title="Review complete alternatives without a ranking claim" copy="The board includes only complete exact storm ensembles in its main table. It reports direct SWMM summaries and adverse peak changes; it does not yet apply cost or decision-brief criteria." action={<button className="secondary-button" onClick={onRefresh} disabled={loading}>{loading ? "Loading…" : "Refresh exact board"}</button>} />
    {errorText(error)}
    {board?.blockers.map((blocker) => <div className="practitioner-error" key={blocker}><Icon name="warning" /><span>{blocker}</span></div>)}
    {!board && !loading && !error && <section className="paper-panel table-empty"><strong>No decision board loaded</strong><span>Refresh after at least one exact job succeeds.</span></section>}
    {board && <>
      <section className="decision-summary paper-panel"><div className="decision-summary-copy"><p className="eyebrow">Exact-result disposition</p><h2>{board.alternatives.length} complete alternative{board.alternatives.length === 1 ? "" : "s"} available for engineering review</h2><p>These are exact SWMM results under the documented model inputs. They are not a preferred option, a nondominated subset, a calibrated truth claim, or approval to advance.</p></div><div className="decision-summary-metrics"><div><span>Storms</span><strong className="mono">{board.storm_ids.length}</strong></div><div><span>Baseline volume</span><strong className="mono">{formatMetric(baselineVolume)}</strong></div><div><span>Baseline peak</span><strong className="mono">{formatMetric(baselinePeak)}</strong></div></div></section>
      <section className="paper-panel practitioner-table-panel"><div className="panel-heading"><div><p className="eyebrow">Alternative-by-alternative</p><h2>Exact hydraulic summaries</h2></div><span className="exact-stamp"><Icon name="shield" /> Direct runs</span></div><div className="table-wrap"><table><thead><tr><th>Design vector</th><th>Aggregate outfall volume</th><th>Δ from baseline</th><th>Maximum outfall peak</th><th>Δ from baseline</th><th>Adverse peak findings</th><th>Exact run evidence</th></tr></thead><tbody>
        {board.alternatives.map((design) => { const volume = aggregateMetric(design, "outfall_volume"); const peak = aggregateMetric(design, "outfall_peak"); return <tr key={design.design_id}><td><strong className="mono">[{design.vector.join(", ")}]</strong><small className="table-subcopy">{design.design_id}</small></td><td className="mono">{formatMetric(volume)}</td><td className="mono">{deltaFrom(baselineVolume, volume)}</td><td className="mono">{formatMetric(peak)}</td><td className="mono">{deltaFrom(baselinePeak, peak)}</td><td>{design.adverse_peak_changes?.length ? <span className="mini-state notice">{design.adverse_peak_changes.length} increases</span> : <span className="mini-state pass">None above tolerance</span>}</td><td><div className="job-chip-list">{traceButtons(design)}</div></td></tr>; })}
        {!board.alternatives.length && <tr><td colSpan={7}><div className="table-empty"><strong>No complete alternative ensembles</strong><span>Inspect excluded designs and exact job states below.</span></div></td></tr>}
      </tbody></table></div><p className="table-note">Volume sums and maximum peaks are calculated by the worker from the selected exact storm runs. Percent deltas shown here are arithmetic comparisons of those worker-returned values.</p></section>
      {board.baseline && <section className="paper-panel practitioner-status-panel"><div className="panel-heading"><div><p className="eyebrow">Untouched baseline</p><h2>Trace every storm before interpreting deltas</h2></div><div className="job-chip-list">{traceButtons(board.baseline)}</div></div></section>}
      {board.excluded_designs.length > 0 && <section className="paper-panel practitioner-table-panel"><div className="panel-heading"><div><p className="eyebrow">Incomplete evidence</p><h2>{board.excluded_designs.length} designs excluded from the main comparison</h2></div></div><div className="table-wrap"><table><thead><tr><th>Design</th><th>Reason</th><th>Storm job states</th></tr></thead><tbody>{board.excluded_designs.map((design) => <tr key={design.design_id}><td className="mono">{design.design_id}</td><td>{design.reason.replaceAll("_", " ")}</td><td className="mono">{Object.entries(design.job_states).map(([storm, state]) => `${storm}: ${state}`).join(" · ")}</td></tr>)}</tbody></table></div></section>}
      <div className="screen-footer-action"><span className="footer-progress"><Icon name="info" /> Export eligibility remains repository-derived</span><button className="primary-button" onClick={() => onStep("export")}>Check evidence-package gates <Icon name="arrow" /></button></div>
    </>}
  </div>;
}

export function PractitionerExportScreen({
  passport,
  eligibility,
  created,
  mode,
  includeArtifacts,
  busy,
  error,
  exactJobCount,
  onMode,
  onIncludeArtifacts,
  onValidate,
  onCreate,
}: {
  passport: Record<string, unknown> | null | undefined;
  eligibility: ExportEligibilityResult | null;
  created: EvidenceExportResult | null;
  mode: "learning" | "screening" | "verified_alternatives";
  includeArtifacts: boolean;
  busy: boolean;
  error: string | null;
  exactJobCount: number;
  onMode: (mode: "learning" | "screening" | "verified_alternatives") => void;
  onIncludeArtifacts: (value: boolean) => void;
  onValidate: () => void;
  onCreate: () => void;
}) {
  const passportMissing = passport === null;
  return <div className="screen-stack practitioner-screen">
    <PageHeading eyebrow="Export / repository gate" title="Let the evidence decide whether a package can be created" copy="The worker reconstructs selected runs, rehashes artifacts, checks model fitness and adverse-review evidence, then creates the package inside the authoritative project. The renderer cannot supply hydraulic values or hashes." />
    {errorText(error)}
    <div className="export-grid">
      <section className="paper-panel practitioner-status-panel"><div className="panel-heading"><div><p className="eyebrow">Requested package</p><h2>Choose the claim boundary</h2></div></div><div className="practitioner-form-grid compact"><label>Package type<select value={mode} onChange={(event) => onMode(event.target.value as typeof mode)}><option value="learning">Learning evidence</option><option value="screening">Alternatives screening</option><option value="verified_alternatives">Verified alternatives</option></select></label><label className="plain-checkbox"><input type="checkbox" checked={includeArtifacts} onChange={(event) => onIncludeArtifacts(event.target.checked)} />Include selected INP, RPT, and OUT files</label></div><dl className="practitioner-facts"><div><dt>Selected exact jobs</dt><dd className="mono">{exactJobCount}</dd></div><div><dt>Model Fitness Passport</dt><dd>{passport === undefined ? "Checking…" : passportMissing ? "Not stored" : String(passport.disposition ?? "Stored")}</dd></div><div><dt>Full-network review</dt><dd>{passportMissing ? "Cannot attest through current UI" : "Derived from Decision Brief"}</dd></div></dl><button className="secondary-button" onClick={onValidate} disabled={busy || passportMissing || exactJobCount === 0}>{busy ? "Checking…" : "Validate export eligibility"}</button></section>
      <section className="paper-panel practitioner-status-panel"><div className="panel-heading"><div><p className="eyebrow">Repository decision</p><h2>{eligibility ? eligibility.allowed ? "Eligible" : "Blocked" : passportMissing ? "Fitness evidence missing" : "Not checked"}</h2></div><span className={`score-ring ${eligibility?.allowed ? "ready" : ""}`}>{eligibility?.allowed ? "OK" : "—"}</span></div>{passportMissing && <div className="rail-notice caution"><Icon name="warning" /><p>No Model Fitness Passport is stored for the active revision. Reopen a single-model import or return to Model and record an explicit engineer attestation before validating an export.</p></div>}{eligibility && <><ul className="gate-list">{eligibility.blockers.map((item) => <li className="block" key={item}><Icon name="warning" />{item}</li>)}{eligibility.warnings.map((item) => <li key={item}><Icon name="info" />{item}</li>)}{eligibility.allowed && <li className="pass"><Icon name="check" />Every repository-derived gate passed.</li>}</ul><button className="primary-button" onClick={onCreate} disabled={busy || !eligibility.allowed}>{busy ? "Creating package…" : "Create evidence package"}</button></>}</section>
    </div>
    {created && <section className="paper-panel bundle-preview"><div className="panel-heading"><div><p className="eyebrow">Created locally</p><h2>{created.manifest.export_id}</h2></div><span className="mini-state pass">{created.cloud_sync?.state ?? "local"}</span></div><ul>{Object.keys(created.files).map((name) => <li key={name}><span className="file-type">{name.split(".").at(-1)?.toUpperCase()}</span><div><strong>{name}</strong><small>Worker-created and hashed</small></div><Icon name="check" /></li>)}</ul></section>}
  </div>;
}

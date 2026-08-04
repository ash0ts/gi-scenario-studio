import { useMemo, useState } from "react";

import { AssistantStatusSchema, createAssistantCallId, IntakeConfirmResponseSchema, IntakeExtractResponseSchema, type AssistantStatus, type IntakeDraft } from "../lib/assistant";
import { hostErrorMessage, hostRequest, rpcData } from "../lib/host";
import { evaluateIntakeAssetEligibility } from "../lib/intakeEligibility";
import type { ProfessionalAssetRecord, ProfessionalProjectSession, RpcPayload } from "../types";
import { AssistantProviderSetup } from "./AssistantProviderSetup";
import { Icon } from "./Icon";

interface DraftPage {
  key: string;
  pageNumber: string;
  text: string;
}

interface IntakeAssistantPanelProps {
  projectSession: ProfessionalProjectSession | null;
  deployment: "DEDICATED" | "SELF_MANAGED" | null;
  documents: ProfessionalAssetRecord[];
  defaultReviewer: string;
}

function newPage(index: number): DraftPage {
  return { key: `page-${Date.now()}-${index}`, pageNumber: String(index), text: "" };
}

export function IntakeAssistantPanel({ projectSession, deployment, documents, defaultReviewer }: IntakeAssistantPanelProps) {
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [documentId, setDocumentId] = useState("");
  const [instruction, setInstruction] = useState("Extract draft decision purpose, explicit constraints, safeguards, cost limits, and assumptions. Preserve page/span evidence and do not infer ecological values.");
  const [pages, setPages] = useState<DraftPage[]>(() => [newPage(1)]);
  const [prohibitedTerms, setProhibitedTerms] = useState("");
  const [draft, setDraft] = useState<IntakeDraft | null>(null);
  const [selectedFields, setSelectedFields] = useState<string[]>([]);
  const [reviewer, setReviewer] = useState(defaultReviewer);
  const [state, setState] = useState<"idle" | "extracting" | "confirming" | "error">("idle");
  const [message, setMessage] = useState("");
  const selectedDocument = documents.find((asset) => asset.assetId === documentId) ?? null;
  const bindingReady = projectSession?.bindingState === "CONNECTED";
  const assetEligibility = evaluateIntakeAssetEligibility(selectedDocument, deployment);
  const assetAuthorized = assetEligibility.eligible;
  const parsedPages = useMemo(() => pages.map((page) => ({
    document_id: documentId,
    page_number: Number(page.pageNumber),
    text: page.text,
  })), [documentId, pages]);
  const pagesValid = parsedPages.length > 0
    && parsedPages.every((page) => Number.isInteger(page.page_number) && page.page_number > 0 && page.text.trim())
    && new Set(parsedPages.map((page) => page.page_number)).size === parsedPages.length;

  const updatePage = (key: string, field: "pageNumber" | "text", value: string) => {
    setPages((current) => current.map((page) => page.key === key ? { ...page, [field]: value } : page));
    setDraft(null);
    setSelectedFields([]);
  };

  const extract = async () => {
    if (!projectSession || !bindingReady || !status?.configured || !assetAuthorized || !pagesValid || !instruction.trim()) return;
    const callId = createAssistantCallId("intake");
    setState("extracting");
    setMessage("");
    setDraft(null);
    setSelectedFields([]);
    try {
      const response = await hostRequest<RpcPayload<unknown>>("assistant.intake.extract", {
        project_handle: projectSession.projectHandle,
        call_id: callId,
        instruction: instruction.trim(),
        pages: parsedPages,
        sensitive_policy: {
          prohibited_terms: prohibitedTerms.split("\n").map((term) => term.trim()).filter(Boolean),
          exact_location_authorized: false,
        },
      });
      const parsed = IntakeExtractResponseSchema.parse(rpcData(response));
      if (parsed.draft.call_id !== callId || parsed.draft.fields.some((field) => field.citations.some((citation) => citation.document_id !== documentId))) {
        throw new Error("The intake draft does not resolve the selected document identity.");
      }
      setDraft(parsed.draft);
      setState("idle");
      setMessage("Draft fields were returned only after trace read-back and citation validation. Nothing was applied to the model.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "No intake draft was returned because authorization, provider, or trace validation failed."));
    }
  };

  const confirm = async () => {
    if (!projectSession || !draft || !selectedFields.length || !reviewer.trim()) return;
    setState("confirming");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("assistant.intake.confirm", {
        project_handle: projectSession.projectHandle,
        draft_id: draft.draft_id,
        field_ids: selectedFields,
        reviewer: reviewer.trim(),
      });
      const parsed = IntakeConfirmResponseSchema.parse(rpcData(response));
      if (parsed.draft.draft_id !== draft.draft_id || parsed.applied_to_model !== false) throw new Error("The confirmation receipt differs from the reviewed draft boundary.");
      setDraft(parsed.draft);
      setSelectedFields([]);
      setState("idle");
      setMessage(parsed.ready_for_model
        ? "All fields are human-confirmed evidence, but remain unapplied. Copy only the relevant values into the decision form and save them deliberately."
        : "Selected fields were human-confirmed. Unconfirmed fields remain drafts and nothing was applied to the model.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The worker did not confirm the selected intake fields."));
    }
  };

  const allowConfiguration = Boolean(bindingReady && projectSession);
  return (
    <section className="paper-panel intake-assistant" aria-labelledby="intake-assistant-heading">
      <div className="panel-heading"><div><span className="section-kicker">Optional evidence intake</span><h2 id="intake-assistant-heading">Extract a cited draft, then review every field.</h2></div><span className="trace-required"><Icon name="shield" /> Read-only · traced</span></div>
      <p>The assistant may draft purposes, constraints, safeguards, cost limits, and assumptions from page text. It cannot derive resistance, focal areas, action effects, approvals, or apply any field to the model.</p>
      <AssistantProviderSetup projectHandle={projectSession?.projectHandle ?? null} allowConfiguration={allowConfiguration} unavailableReason={projectSession ? "A connected, exact-scope Restricted W&B binding is required." : "Create or import the authoritative project first."} onStatus={setStatus} />
      <div className="intake-document-grid">
        <label><span>Retained supporting document *</span><select value={documentId} onChange={(event) => { setDocumentId(event.target.value); setDraft(null); setSelectedFields([]); }}><option value="">Choose a DOCUMENT asset…</option>{documents.map((asset) => <option key={asset.assetId} value={asset.assetId}>{asset.title} · {asset.displayName}</option>)}</select></label>
        <label><span>Reviewer *</span><input value={reviewer} onChange={(event) => setReviewer(event.target.value)} /></label>
      </div>
      {selectedDocument && (
        <div className={`intake-authorization ${assetAuthorized ? "authorized" : "blocked"}`} role={assetAuthorized ? "note" : "alert"}>
          <Icon name={assetAuthorized ? "check" : "lock"} />
          <p><strong>{assetEligibility.title}</strong><span>{assetEligibility.detail}</span><small>{selectedDocument.cloudDisposition} · {selectedDocument.sensitivity} · {selectedDocument.mediaType} · reviewed by {selectedDocument.reviewer}</small></p>
        </div>
      )}
      <label><span>Extraction instruction *</span><textarea rows={3} value={instruction} onChange={(event) => setInstruction(event.target.value)} /></label>
      <label><span>Sensitive terms to generalize before tracing · one per line</span><textarea rows={2} value={prohibitedTerms} onChange={(event) => setProhibitedTerms(event.target.value)} placeholder="Sensitive place, property, or species-location terms" /></label>
      <div className="intake-pages">
        <div className="intake-pages-heading"><strong>Page text supplied for this request</strong><button className="text-action" type="button" disabled={!assetAuthorized || pages.length >= 12} onClick={() => setPages((current) => [...current, newPage(current.length + 1)])}>Add page</button></div>
        {pages.map((page, index) => <div className="intake-page" key={page.key}><label><span>Page number *</span><input inputMode="numeric" disabled={!assetAuthorized} value={page.pageNumber} onChange={(event) => updatePage(page.key, "pageNumber", event.target.value.replace(/[^0-9]/g, ""))} /></label><label><span>Exact retained page text *</span><textarea rows={5} disabled={!assetAuthorized} value={page.text} onChange={(event) => updatePage(page.key, "text", event.target.value)} /></label>{pages.length > 1 && <button className="icon-button" type="button" disabled={!assetAuthorized} aria-label={`Remove page ${index + 1}`} onClick={() => setPages((current) => current.filter((item) => item.key !== page.key))}>×</button>}</div>)}
      </div>
      <div className="intake-action"><div><strong>Minimization boundary</strong><span>Exact locations are never authorized by this screen. Listed terms are additional minimization—not permission to send raw sensitive documents. The worker must verify the selected retained text before any trace or provider call.</span></div><button className="primary-button" type="button" disabled={!bindingReady || !status?.configured || !assetAuthorized || !pagesValid || !instruction.trim() || state === "extracting"} onClick={() => void extract()}>{state === "extracting" ? "Tracing and validating…" : "Extract cited draft"}</button></div>
      {draft && (
        <div className="intake-draft">
          <div className="assistant-answer-state"><Icon name="check" /><strong>Trace read back · deterministic citation validation passed</strong></div>
          <ul className="intake-field-list">{draft.fields.map((field) => <li key={field.field_id} className={field.confirmed ? "is-confirmed" : ""}><label className="confirmation-row"><input type="checkbox" disabled={field.confirmed} checked={field.confirmed || selectedFields.includes(field.field_id)} onChange={(event) => setSelectedFields((current) => event.target.checked ? [...current, field.field_id] : current.filter((id) => id !== field.field_id))} /><span><strong>{field.field_name.replace("_", " ")}{field.confirmed ? " · human confirmed" : " · draft"}</strong>{field.value}</span></label><div className="intake-citations">{field.citations.map((citation) => <figure key={`${citation.document_id}-${citation.page_number}-${citation.start}-${citation.end}`}><figcaption>Page {citation.page_number} · characters {citation.start}–{citation.end}</figcaption><blockquote>{citation.quote}</blockquote></figure>)}</div></li>)}</ul>
          <div className="assistant-limitations"><strong>Draft limitations</strong><ul>{draft.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></div>
          <button className="secondary-button" type="button" disabled={!selectedFields.length || !reviewer.trim() || state === "confirming"} onClick={() => void confirm()}>{state === "confirming" ? "Recording human review…" : `Confirm ${selectedFields.length || "selected"} field${selectedFields.length === 1 ? "" : "s"}`}</button>
          <small>Confirmation creates reviewed evidence only. It does not modify the decision brief or any ecological model object.</small>
        </div>
      )}
      {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
    </section>
  );
}

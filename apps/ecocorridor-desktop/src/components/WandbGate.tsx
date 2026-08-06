import { useCallback, useEffect, useMemo, useState } from "react";
import type { EntryPath, ObservabilityPreflight, RpcPayload } from "../types";
import { credentialStatus, hasNativeHost, hostErrorMessage, hostRequest, rpcData, storeCredential, wandbCredentialId } from "../lib/host";
import { Icon } from "./Icon";
import { useDialogFocus } from "./useDialogFocus";

interface WandbGateProps {
  path: EntryPath | null;
  projectHandle: string | null;
  onClose: () => void;
  onVerified: (preflight: ObservabilityPreflight) => void;
}

type Deployment = "WANDB_CLOUD" | "DEDICATED" | "SELF_MANAGED";
type GateState = "idle" | "checking" | "verified" | "blocked";

export const RIDGEWAY_SYNTHETIC_TRACE_CONSENT = "I authorize alias-only agent interaction, immutable fixture and run hashes, and trace receipts for the exact CC0 Ridgeway fixture to be sent to this W&B project. This does not authorize professional project evidence, raw locations, full sync, artifacts, model approval, or any claim about W&B project visibility.";

export function WandbGate({ path, projectHandle, onClose, onVerified }: WandbGateProps) {
  const syntheticOnly = path === "guided";
  const [deployment, setDeployment] = useState<Deployment>("DEDICATED");
  const [baseUrl, setBaseUrl] = useState("");
  const [webBaseUrl, setWebBaseUrl] = useState("");
  const [entity, setEntity] = useState("");
  const [project, setProject] = useState("");
  const [policyAccepted, setPolicyAccepted] = useState(false);
  const [state, setState] = useState<GateState>("idle");
  const [message, setMessage] = useState("");
  const [credentialStored, setCredentialStored] = useState(false);
  const stableClose = useCallback(onClose, [onClose]);
  const dialogRef = useDialogFocus(Boolean(path), stableClose);
  const credentialId = useMemo(() => wandbCredentialId(baseUrl, entity, project), [baseUrl, entity, project]);
  const nativeHost = hasNativeHost();
  const canRequest = useMemo(() => Boolean(
    nativeHost
    && credentialStored
    && baseUrl.trim()
    && entity.trim()
    && project.trim()
    && policyAccepted
    && (!syntheticOnly || projectHandle),
  ), [baseUrl, credentialStored, entity, nativeHost, policyAccepted, project, projectHandle, syntheticOnly]);

  useEffect(() => {
    if (!path) return;
    setDeployment(path === "guided" ? "WANDB_CLOUD" : "DEDICATED");
    setPolicyAccepted(false);
    setState("idle");
    setMessage("");
  }, [path, projectHandle]);

  useEffect(() => {
    if (path !== "guided" || deployment !== "WANDB_CLOUD") return;
    setBaseUrl((current) => current.trim() || "https://api.wandb.ai");
    setWebBaseUrl((current) => current.trim() || "https://wandb.ai");
  }, [deployment, path]);

  useEffect(() => {
    if (!path || !nativeHost || credentialId.length < 8) {
      setCredentialStored(false);
      return;
    }
    void credentialStatus(credentialId, "wandb").then(setCredentialStored).catch(() => setCredentialStored(false));
  }, [credentialId, nativeHost, path]);

  if (!path) return null;
  const verify = async () => {
    setState("checking");
    setMessage("");
    if (!nativeHost) {
      setState("blocked");
      setMessage("The secure desktop host is not available in this browser preview. No credential or project request was sent.");
      return;
    }
    try {
      if (syntheticOnly && !projectHandle) {
        setState("blocked");
        setMessage("The exact opened Ridgeway project handle is unavailable. Close this dialog and reopen it from the selected portfolio.");
        return;
      }
      const method = syntheticOnly ? "observability.synthetic_trace.preflight" : "observability.preflight";
      const result = await hostRequest<RpcPayload<{
        preflight_id: string;
        scope: string;
        allowed: boolean;
        message?: string;
        visibility_claim?: string;
        restricted_verified?: boolean;
        weave_capability?: string;
        binding_preview?: { entity?: string; project?: string; deployment?: string };
      }>>(method, {
        ...(syntheticOnly ? { project_handle: projectHandle, consent: RIDGEWAY_SYNTHETIC_TRACE_CONSENT } : {}),
        credential_id: credentialId,
        deployment,
        api_base_url: baseUrl.trim(),
        web_base_url: webBaseUrl.trim() || undefined,
        entity: entity.trim(),
        project: project.trim(),
      });
      const data = rpcData(result);
      if (
        syntheticOnly
        && /^synthetic_trace_preflight_[A-Za-z0-9_-]{8,128}$/.test(data.preflight_id)
        && data.allowed
        && data.scope === "CC0_SYNTHETIC_TRACE_ONLY"
        && data.visibility_claim === "NONE"
        && data.restricted_verified === false
        && data.weave_capability === "PENDING_NATIVE_AGENT_PREFLIGHT"
        && data.binding_preview?.entity === entity.trim()
        && data.binding_preview?.project === project.trim()
        && data.binding_preview?.deployment === deployment
      ) {
        setState("verified");
        setMessage("Trace-only project identity verified. Visibility was not asserted, full synchronization stays disabled, and Weave write/read-back remains a separate provider preflight.");
        onVerified({
          preflightId: data.preflight_id,
          entity: entity.trim(),
          project: project.trim(),
          deployment,
          purpose: "RIDGEWAY_SYNTHETIC_TRACE_ONLY",
          projectHandle,
        });
      } else if (!syntheticOnly && typeof data.preflight_id === "string" && data.preflight_id.length > 0 && data.allowed && data.scope === "RESTRICTED" && deployment !== "WANDB_CLOUD") {
        setState("verified");
        setMessage("Restricted scope plus W&B Models and Weave access were verified. The destination binds after the local project is created.");
        onVerified({
          preflightId: data.preflight_id,
          entity: entity.trim(),
          project: project.trim(),
          deployment,
          purpose: "PROFESSIONAL_FULL_SYNC",
          projectHandle: null,
        });
      } else {
        setState("blocked");
        setMessage(data.message ?? (syntheticOnly
          ? "The target did not return the exact CC0 trace-only identity receipt. No trace capability was enabled."
          : "The target did not return an authoritative RESTRICTED scope. Professional uploads remain blocked."));
      }
    } catch (reason) {
      setState("blocked");
      setMessage(hostErrorMessage(reason, "The destination preflight could not be verified."));
    }
  };

  const enterKey = async () => {
    setMessage("");
    try {
      await storeCredential(credentialId, "wandb");
      setCredentialStored(true);
      setMessage("The API key is stored in the operating-system keychain. It was not exposed to this interface.");
    } catch (reason) {
      setState("blocked");
      setMessage(hostErrorMessage(reason, "The API key could not be stored."));
    }
  };

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div ref={dialogRef} className="wandb-dialog" role="dialog" aria-modal="true" aria-labelledby="wandb-title" aria-describedby="wandb-description">
        <header className="dialog-header">
          <div><span className="section-kicker">{syntheticOnly ? "Optional synthetic observability" : "Professional boundary"}</span><h2 id="wandb-title">Connect the evidence destination</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close W&B connection"><Icon name="close" /></button>
        </header>
        <div className="gate-scroll-body">
          <p id="wandb-description" className="dialog-lead">{syntheticOnly ? "Ridgeway stays fully usable without an account. This optional lane verifies an existing W&B project only for alias-only Review Brief Agent traces over the exact CC0 fixture. It does not assert project visibility, enable full evidence sync, or authorize professional data." : "A professional EcoCorridor project mirrors complete authorized evidence only after a documented exact-scope resolver confirms a Restricted W&B project. This build does not assume that Dedicated or Self-Managed is sufficient by itself; without a configured resolver, verification stays blocked. The local database and content-addressed artifacts remain authoritative."}</p>

        <section className="gate-section" aria-labelledby="deployment-heading">
          <div className="gate-number">01</div>
          <div>
            <h3 id="deployment-heading">Choose a deployment</h3>
            <div className="deployment-options">
              <label className={deployment === "DEDICATED" ? "selected" : ""}><input type="radio" name="deployment" checked={deployment === "DEDICATED"} onChange={() => setDeployment("DEDICATED")} /><span><strong>Dedicated Cloud</strong><small>{syntheticOnly ? "Existing project identity only; no visibility or full-sync claim." : "Selectable for configuration; still blocked unless an approved admin resolver is installed."}</small></span></label>
              <label className={deployment === "SELF_MANAGED" ? "selected" : ""}><input type="radio" name="deployment" checked={deployment === "SELF_MANAGED"} onChange={() => setDeployment("SELF_MANAGED")} /><span><strong>Self-Managed</strong><small>{syntheticOnly ? "Existing project identity only; no visibility or full-sync claim." : "Selectable for configuration; still blocked unless an approved admin resolver is installed."}</small></span></label>
              {syntheticOnly ? <label className={deployment === "WANDB_CLOUD" ? "selected" : ""}><input type="radio" name="deployment" checked={deployment === "WANDB_CLOUD"} onChange={() => setDeployment("WANDB_CLOUD")} /><span><strong>W&B Cloud</strong><small>Allowed only for the explicit CC0 trace-only lane; project visibility is not asserted.</small></span></label> : <label className="blocked-option"><input type="radio" name="deployment" disabled /><span><strong>W&B Cloud</strong><small>Blocked until a supported API exposes exact Restricted scope.</small></span><Icon name="lock" /></label>}
            </div>
          </div>
        </section>

        <section className="gate-section" aria-labelledby="binding-heading">
          <div className="gate-number">02</div>
          <div>
            <h3 id="binding-heading">{syntheticOnly ? "Identify the existing trace project" : "Identify the Restricted project"}</h3>
            <div className="field-grid two-up">
              <label><span>API base URL</span><input value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.wandb.example.org" autoComplete="url" /></label>
              <label><span>Web base URL <small>(optional)</small></span><input value={webBaseUrl} onChange={(event) => setWebBaseUrl(event.target.value)} placeholder="https://wandb.example.org" autoComplete="url" /></label>
              <label><span>Entity or team</span><input value={entity} onChange={(event) => setEntity(event.target.value)} placeholder="wildlife-program" /></label>
              <label><span>Project</span><input value={project} onChange={(event) => setProject(event.target.value)} placeholder="ridgeway-screening" /></label>
            </div>
            <p className="field-help"><Icon name="lock" /> The renderer never receives an API key. The desktop host opens the OS credential manager and injects credentials in memory.</p>
            <button className="secondary-button credential-button" type="button" disabled={!nativeHost || !baseUrl.trim() || !entity.trim() || !project.trim()} onClick={() => void enterKey()}>
              <Icon name="lock" /> {credentialStored ? "Replace stored API key" : "Enter API key securely"}
            </button>
          </div>
        </section>

        <section className="gate-section" aria-labelledby="policy-heading">
          <div className="gate-number">03</div>
          <div>
            <h3 id="policy-heading">Preview the data boundary</h3>
            <ul className="upload-categories">
              <li><Icon name="map" /><span><strong>{syntheticOnly ? "Alias-only agent turns" : "Spatial evidence"}</strong><small>{syntheticOnly ? "Closed finding aliases and reviewer-discipline enums; no raw locations or professional evidence" : "Authorized resistance, focal, intervention, and review-mask assets"}</small></span></li>
              <li><Icon name="grid" /><span><strong>{syntheticOnly ? "Immutable synthetic identities" : "Direct results"}</strong><small>{syntheticOnly ? "Exact Ridgeway fixture and retained direct-run hashes; no matrices, rasters, or artifacts" : "Matrices, permitted current rasters, QA, search traces, and hashes"}</small></span></li>
              <li><Icon name="document" /><span><strong>{syntheticOnly ? "Agents trace receipt" : "Decision record"}</strong><small>{syntheticOnly ? "Bounded model/tool activity and the exact Weave read-back receipt; no full synchronization" : "Rules, costs, assumptions, evidence claims, and exports"}</small></span></li>
            </ul>
            <label className="policy-check"><input type="checkbox" checked={policyAccepted} onChange={(event) => setPolicyAccepted(event.target.checked)} /><span>{syntheticOnly ? RIDGEWAY_SYNTHETIC_TRACE_CONSENT : "I will classify each asset and understand that Restricted visibility is not itself permission to upload."}</span></label>
          </div>
        </section>

          {message && <div className={`gate-message ${state}`} role={state === "blocked" ? "alert" : "status"}><Icon name={state === "verified" ? "check" : "warning"} /><span>{message}</span></div>}
        </div>

        <footer className="dialog-actions">
          <small>{syntheticOnly ? "The offline Ridgeway replay remains available if this optional connection is cancelled or blocked. Trace-only connection never enables project sync." : "Professional workflows remain unavailable until exact Restricted scope and asset policy are verified."}</small>
          <button className="primary-button" type="button" disabled={!canRequest || state === "checking" || state === "verified"} onClick={() => void verify()}>{state === "checking" ? "Verifying…" : state === "verified" ? (syntheticOnly ? "Connecting trace target…" : "Opening project…") : syntheticOnly ? "Verify trace-only target" : "Verify Restricted project"} <Icon name="shield" /></button>
        </footer>
      </div>
    </div>
  );
}

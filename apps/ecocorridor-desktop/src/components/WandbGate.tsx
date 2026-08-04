import { useCallback, useEffect, useMemo, useState } from "react";
import type { EntryPath, ObservabilityPreflight, RpcPayload } from "../types";
import { credentialStatus, hasNativeHost, hostErrorMessage, hostRequest, rpcData, storeCredential, wandbCredentialId } from "../lib/host";
import { Icon } from "./Icon";
import { useDialogFocus } from "./useDialogFocus";

interface WandbGateProps {
  path: Exclude<EntryPath, "guided"> | null;
  onClose: () => void;
  onVerified: (preflight: ObservabilityPreflight) => void;
}

type Deployment = "DEDICATED" | "SELF_MANAGED";
type GateState = "idle" | "checking" | "verified" | "blocked";

export function WandbGate({ path, onClose, onVerified }: WandbGateProps) {
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
  const canRequest = useMemo(() => Boolean(nativeHost && credentialStored && baseUrl.trim() && entity.trim() && project.trim() && policyAccepted), [baseUrl, credentialStored, entity, nativeHost, policyAccepted, project]);

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
      const result = await hostRequest<RpcPayload<{ preflight_id: string; scope: string; allowed: boolean; message?: string }>>("observability.preflight", {
        credential_id: credentialId,
        deployment,
        api_base_url: baseUrl.trim(),
        web_base_url: webBaseUrl.trim() || undefined,
        entity: entity.trim(),
        project: project.trim(),
      });
      const data = rpcData(result);
      if (data.allowed && data.scope === "RESTRICTED") {
        setState("verified");
        setMessage("Restricted scope plus W&B Models and Weave access were verified. The destination binds after the local project is created.");
        onVerified({
          preflightId: data.preflight_id,
          entity: entity.trim(),
          project: project.trim(),
          deployment,
        });
      } else {
        setState("blocked");
        setMessage(data.message ?? "The target did not return an authoritative RESTRICTED scope. Professional uploads remain blocked.");
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
          <div><span className="section-kicker">Professional boundary</span><h2 id="wandb-title">Connect the evidence destination</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="Close W&B connection"><Icon name="close" /></button>
        </header>
        <div className="gate-scroll-body">
          <p id="wandb-description" className="dialog-lead">A professional EcoCorridor project mirrors complete authorized evidence only after a documented exact-scope resolver confirms a Restricted W&B project. This build does not assume that Dedicated or Self-Managed is sufficient by itself; without a configured resolver, verification stays blocked. The local database and content-addressed artifacts remain authoritative.</p>

        <section className="gate-section" aria-labelledby="deployment-heading">
          <div className="gate-number">01</div>
          <div>
            <h3 id="deployment-heading">Choose a deployment</h3>
            <div className="deployment-options">
              <label className={deployment === "DEDICATED" ? "selected" : ""}><input type="radio" name="deployment" checked={deployment === "DEDICATED"} onChange={() => setDeployment("DEDICATED")} /><span><strong>Dedicated Cloud</strong><small>Selectable for configuration; still blocked unless an approved admin resolver is installed.</small></span></label>
              <label className={deployment === "SELF_MANAGED" ? "selected" : ""}><input type="radio" name="deployment" checked={deployment === "SELF_MANAGED"} onChange={() => setDeployment("SELF_MANAGED")} /><span><strong>Self-Managed</strong><small>Selectable for configuration; still blocked unless an approved admin resolver is installed.</small></span></label>
              <label className="blocked-option"><input type="radio" name="deployment" disabled /><span><strong>W&B Cloud</strong><small>Blocked until a supported API exposes exact Restricted scope.</small></span><Icon name="lock" /></label>
            </div>
          </div>
        </section>

        <section className="gate-section" aria-labelledby="binding-heading">
          <div className="gate-number">02</div>
          <div>
            <h3 id="binding-heading">Identify the Restricted project</h3>
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
              <li><Icon name="map" /><span><strong>Spatial evidence</strong><small>Authorized resistance, focal, intervention, and review-mask assets</small></span></li>
              <li><Icon name="grid" /><span><strong>Direct results</strong><small>Matrices, permitted current rasters, QA, search traces, and hashes</small></span></li>
              <li><Icon name="document" /><span><strong>Decision record</strong><small>Rules, costs, assumptions, evidence claims, and exports</small></span></li>
            </ul>
            <label className="policy-check"><input type="checkbox" checked={policyAccepted} onChange={(event) => setPolicyAccepted(event.target.checked)} /><span>I will classify each asset and understand that Restricted visibility is not itself permission to upload.</span></label>
          </div>
        </section>

          {message && <div className={`gate-message ${state}`} role={state === "blocked" ? "alert" : "status"}><Icon name={state === "verified" ? "check" : "warning"} /><span>{message}</span></div>}
        </div>

        <footer className="dialog-actions">
          <small>Professional workflows remain unavailable until exact Restricted scope and asset policy are verified.</small>
          <button className="primary-button" type="button" disabled={!canRequest || state === "checking" || state === "verified"} onClick={() => void verify()}>{state === "checking" ? "Verifying…" : state === "verified" ? "Opening project…" : "Verify Restricted project"} <Icon name="shield" /></button>
        </footer>
      </div>
    </div>
  );
}

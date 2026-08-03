import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { localObservabilityState, observabilityDetail, observabilityLabel, parseObservabilityAccountState, wandbProjectSettingsUrl } from "../lib/observability";
import { transport } from "../lib/transport";
import type { ObservabilityState } from "../types";
import { Icon } from "./Icons";

type SetupChoice = "preview" | "cloud";
type Deployment = "wandb_cloud" | "dedicated_cloud" | "self_managed";

const credentialIdFor = (deployment: Deployment, baseUrl: string) => {
  try {
    const endpoint = new URL(baseUrl);
    return `wandb-${deployment}-${endpoint.host}`.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 128);
  } catch {
    return `wandb-${deployment}-invalid`;
  }
};

interface CredentialStatus {
  credentialId: string;
  stored: boolean;
  storage: "operating-system-keychain";
}

interface ObservabilityWizardProps {
  open: boolean;
  state: ObservabilityState;
  required?: boolean;
  onClose: () => void;
  onChange: (state: ObservabilityState) => void;
}

export function ObservabilityWizard({ open, state, required = false, onClose, onChange }: ObservabilityWizardProps) {
  const [choice, setChoice] = useState<SetupChoice>(window.__TAURI_INTERNALS__ ? "cloud" : "preview");
  const [entity, setEntity] = useState(state.entity ?? "");
  const [project, setProject] = useState(state.project ?? "");
  const [deployment, setDeployment] = useState<Deployment>(state.deployment ?? "wandb_cloud");
  const [apiBaseUrl, setApiBaseUrl] = useState(state.apiBaseUrl ?? "https://api.wandb.ai");
  const [webBaseUrl, setWebBaseUrl] = useState(state.webBaseUrl ?? "https://wandb.ai");
  const [credentialStored, setCredentialStored] = useState(false);
  const [disclosureAccepted, setDisclosureAccepted] = useState(state.disclosureAccepted);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const isDesktop = Boolean(window.__TAURI_INTERNALS__);
  const credentialId = credentialIdFor(deployment, apiBaseUrl);
  const projectSettingsUrl = wandbProjectSettingsUrl(webBaseUrl, entity, project);

  useEffect(() => {
    if (!open) {
      previousFocus.current?.focus();
      return;
    }
    setChoice(isDesktop ? "cloud" : "preview");
    setEntity(state.entity ?? "");
    setProject(state.project ?? "");
    setDisclosureAccepted(state.disclosureAccepted);
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    titleRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !required) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])') ?? [])];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isDesktop, onClose, open, required, state]);

  useEffect(() => {
    if (!open || !isDesktop) return;
    invoke<CredentialStatus>("credential_status", { credentialId })
      .then((result) => setCredentialStored(result.stored))
      .catch(() => setCredentialStored(false));
  }, [credentialId, isDesktop, open]);

  if (!open) return null;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      let next: ObservabilityState;
      if (choice === "cloud" && isDesktop) {
        if (!credentialStored) {
          const stored = await invoke<CredentialStatus>("credential_prompt_and_store", { credentialId });
          setCredentialStored(stored.stored);
          if (!stored.stored) throw new Error("The operating-system keychain did not confirm credential storage.");
        }
        const connected = parseObservabilityAccountState(await transport.request<unknown>("observability.connect", {
          scope: "account",
          credential_id: credentialId,
          deployment,
          api_base_url: apiBaseUrl,
          web_base_url: webBaseUrl,
          entity,
          project,
          weave_project: project,
          project_visibility: "Restricted",
          data_policy_acknowledged: true,
          acknowledged_by: entity,
          classification: "standard_engineering",
        }, { timeoutMs: 120_000 }));
        if (connected.state !== "CONNECTED" || connected.mode !== "connected" || !connected.configured) {
          throw new Error("The worker did not confirm both Restricted W&B Models and Weave access.");
        }
        next = connected;
      } else {
        if (isDesktop) {
          throw new Error("A verified Restricted W&B project is required before desktop use.");
        }
        next = await transport.request<ObservabilityState>("observability.preview_interface", {
          entity,
          project,
          weaveProject: project,
          disclosureAccepted,
        });
      }
      onChange(next);
      onClose();
    } catch (reason) {
      if (choice === "preview" && !isDesktop) {
        onChange({
          mode: "demo",
          configured: true,
          provider: "wandb-weave",
          disclosureAccepted: true,
          message: "Interface-only Cedar Trace preview; no project is bound and no upload occurs.",
        });
        onClose();
      } else {
        setError(reason instanceof Error ? reason.message : "The observability choice could not be saved.");
      }
    } finally {
      setBusy(false);
    }
  };

  const deleteCredential = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await invoke<CredentialStatus>("credential_delete", { credentialId });
      setCredentialStored(result.stored);
      if (!result.stored) {
        onChange({
          ...localObservabilityState(false),
          state: "AUTHENTICATION_REQUIRED",
          scope: "account",
          schemaVersion: "2.0",
          message: "The W&B credential was removed from the operating-system keychain. Reconnect before creating another project.",
        });
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The stored credential could not be removed.");
    } finally {
      setBusy(false);
    }
  };

  const connectionReady = entity.trim() !== "" && project.trim() !== "" && apiBaseUrl.trim() !== "" && webBaseUrl.trim() !== "" && disclosureAccepted;
  const canSave = choice === "preview" ? !isDesktop : connectionReady;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (!required && event.currentTarget === event.target) onClose();
    }}>
      <section ref={dialogRef} className="observability-wizard" role="dialog" aria-modal="true" aria-labelledby="observability-title" aria-describedby="observability-description">
        <header className="wizard-header">
          <div>
            <p className="eyebrow">Required project connection</p>
            <h2 id="observability-title" ref={titleRef} tabIndex={-1}>Connect a Restricted W&amp;B project</h2>
            <p id="observability-description">W&amp;B is the collaboration mirror. This source MVP projects imported bundles, an open-time guided snapshot, completed analyses, and worker-created exports; the local SQLite ledger and hashed artifacts remain authoritative.</p>
          </div>
          {!required && <button className="icon-button" onClick={onClose} aria-label="Close observability setup"><Icon name="close" /></button>}
        </header>

        <div className="wizard-current" role="status">
          <span className={`observability-dot mode-${state.mode}`} />
          <div><strong>{observabilityLabel(state)}</strong><small>{observabilityDetail(state)}</small></div>
        </div>

        <fieldset className="connection-choices">
          <legend>Connection path</legend>
          <label className={choice === "cloud" ? "selected" : ""}>
            <input type="radio" name="observability-mode" checked={choice === "cloud"} onChange={() => setChoice("cloud")} disabled={!isDesktop} />
            <span><Icon name="trace" /></span>
            <div><strong>W&amp;B Models + Weave</strong><small>Required in the desktop application. The target project must be verified Restricted.</small></div>
          </label>
          {!isDesktop && <label className={choice === "preview" ? "selected" : ""}>
            <input type="radio" name="observability-mode" checked={choice === "preview"} onChange={() => setChoice("preview")} />
            <span><Icon name="spark" /></span>
            <div><strong>Interface preview</strong><small>Explore the public Cedar Trace UI only; this is not a bound engineering project.</small></div>
          </label>}
        </fieldset>

        {choice === "preview" && (
          <div className="wizard-disclosure">
            <Icon name="info" />
            <div><strong>Browser preview only</strong><p>No credential, project binding, upload, or assistant provider is available. Install the desktop build to create or open a project.</p></div>
          </div>
        )}

        {choice === "cloud" && (
          <div className="development-fields">
            <div className="form-row">
              <label>W&B entity<input value={entity} onChange={(event) => setEntity(event.target.value)} placeholder="team-or-user" autoComplete="off" /></label>
              <label>Restricted W&amp;B project<input value={project} onChange={(event) => setProject(event.target.value)} placeholder="gi-scenario-project-hash" autoComplete="off" /></label>
            </div>
            <label>Deployment <select value={deployment} onChange={(event) => setDeployment(event.target.value as Deployment)}><option value="wandb_cloud">W&amp;B Cloud</option><option value="dedicated_cloud">Dedicated Cloud</option><option value="self_managed">Self-Managed</option></select></label>
            <div className="form-row"><label>API base URL<input value={apiBaseUrl} onChange={(event) => setApiBaseUrl(event.target.value)} inputMode="url" autoComplete="off" /></label><label>Web base URL<input value={webBaseUrl} onChange={(event) => setWebBaseUrl(event.target.value)} inputMode="url" autoComplete="off" /></label></div>
            {isDesktop ? <>
              <div className="credential-boundary"><Icon name="shield" /><p><strong>{credentialStored ? "API key stored securely." : "API key required."}</strong> When you continue, the native host opens a masked operating-system dialog and stores the key under <code>{credentialId}</code>. Plaintext never enters React state or the worker process environment.</p>{credentialStored && <button type="button" className="text-button" onClick={deleteCredential} disabled={busy}>Remove stored key</button>}</div>
            </> : <div className="credential-boundary"><Icon name="shield" /><p><strong>No API key in browser preview.</strong> This path validates labels and W&B/Weave links locally; it cannot authenticate or send data.</p></div>}
            <div className="wizard-disclosure"><Icon name="info" /><div><strong>Authorized evidence scope</strong><p>The release contract covers models, GIS/data assets, storms, INP/RPT/OUT files, exact results, QA, search traces, decisions, and exports. Conceptual-data projection, guided interaction events, and browser-downloaded learning summaries remain unfinished. Secrets, environment contents, local absolute paths, and raw console logs are excluded.</p></div></div>
            <div className="wizard-disclosure"><Icon name="warning" /><div><strong>Exact-scope gate</strong><p>The application accepts only an authoritative <code>Restricted</code> visibility value. The pinned W&amp;B Public API does not currently expose that scope, so this source build fails closed before probes or uploads unless the deployment supplies a supported exact-scope resolver.</p></div></div>
            {projectSettingsUrl && <div className="credential-boundary"><Icon name="trace" /><p><strong>Set visibility in W&amp;B.</strong> Open the project settings, select <code>Restricted</code>, then return here to verify. W&amp;B currently exposes this control in its application rather than its supported Models SDK.</p><a className="text-button" href={projectSettingsUrl} target="_blank" rel="noreferrer">Open W&amp;B project settings</a></div>}
            <label className="disclosure-check"><input type="checkbox" checked={disclosureAccepted} onChange={(event) => setDisclosureAccepted(event.target.checked)} /><span><Icon name="check" /></span><p>I authorize full engineering-evidence synchronization to this user-owned Restricted project and understand its storage and deletion policy.</p></label>
          </div>
        )}

        {error && <div className="wizard-error" role="alert"><Icon name="warning" /> {error}</div>}

        <footer className="wizard-footer">
          <p>{choice === "preview" ? "This opens only the public interface preview." : "The binding becomes immutable after the first successful artifact upload. Credentials are never written to browser storage."}</p>
          <button className="primary-button" disabled={!canSave || busy} onClick={save}>
            {busy ? "Checking…" : choice === "preview" ? "Open interface preview" : !credentialStored ? "Enter key and verify project" : "Verify Restricted project"}
            <Icon name="arrow" />
          </button>
        </footer>
      </section>
    </div>
  );
}

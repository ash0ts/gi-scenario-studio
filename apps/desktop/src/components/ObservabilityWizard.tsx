import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import {
  bindOpenProject,
  chooseLocalWorkspace,
  localObservabilityState,
  observabilityDetail,
  observabilityLabel,
  parseObservabilityAccountState,
  wandbProjectSettingsUrl,
  type ActiveObservabilityProject,
  type ObservabilityRpcRequest,
  type ProjectCloudSyncState,
} from "../lib/observability";
import { transport } from "../lib/transport";
import type { ObservabilityState } from "../types";
import { Icon } from "./Icons";

type SetupChoice = "local" | "cloud";
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
  activeProject?: ActiveObservabilityProject | null;
  onClose: () => void;
  onChange: (state: ObservabilityState) => void;
  onProjectCloudChange?: (state: ProjectCloudSyncState | null) => void;
}

export function ObservabilityWizard({ open, state, required = false, activeProject = null, onClose, onChange, onProjectCloudChange }: ObservabilityWizardProps) {
  const [choice, setChoice] = useState<SetupChoice>(state.mode === "connected" ? "cloud" : "local");
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
  const request: ObservabilityRpcRequest = (method, params, options) => transport.request<unknown>(method, params, options);

  useEffect(() => {
    if (!open) {
      previousFocus.current?.focus();
      return;
    }
    setChoice(state.mode === "connected" ? "cloud" : "local");
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
        if (activeProject) {
          const projectState = await bindOpenProject(request, activeProject);
          onProjectCloudChange?.(projectState);
        }
        next = connected;
      } else if (choice === "local" && isDesktop) {
        const local = await chooseLocalWorkspace(request, activeProject);
        onProjectCloudChange?.(local.project);
        next = local.account;
      } else next = localObservabilityState();
      onChange(next);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The observability choice could not be saved.");
    } finally {
      setBusy(false);
    }
  };

  const deleteCredential = async () => {
    setBusy(true);
    setError(null);
    try {
      const local = await chooseLocalWorkspace(request, activeProject);
      onProjectCloudChange?.(local.project);
      onChange(local.account);
      const result = await invoke<CredentialStatus>("credential_delete", { credentialId });
      setCredentialStored(result.stored);
      if (result.stored) throw new Error("The operating-system keychain still reports this credential as stored.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The stored credential could not be removed.");
    } finally {
      setBusy(false);
    }
  };

  const connectionReady = entity.trim() !== "" && project.trim() !== "" && apiBaseUrl.trim() !== "" && webBaseUrl.trim() !== "" && disclosureAccepted;
  const canSave = choice === "local" || (isDesktop && connectionReady);

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
      if (!required && event.currentTarget === event.target) onClose();
    }}>
      <section ref={dialogRef} className="observability-wizard" role="dialog" aria-modal="true" aria-labelledby="observability-title" aria-describedby="observability-description">
        <header className="wizard-header">
          <div>
            <p className="eyebrow">Evidence location</p>
            <h2 id="observability-title" ref={titleRef} tabIndex={-1}>Choose where project evidence lives</h2>
            <p id="observability-description">Local files and SQLite are always the authoritative engineering record. Add W&amp;B only when you want a verified collaboration and observability mirror.</p>
          </div>
          {!required && <button className="icon-button" onClick={onClose} aria-label="Close observability setup"><Icon name="close" /></button>}
        </header>

        <div className="wizard-current" role="status">
          <span className={`observability-dot mode-${state.mode}`} />
          <div><strong>{observabilityLabel(state)}</strong><small>{observabilityDetail(state)}</small></div>
        </div>

        <fieldset className="connection-choices">
          <legend>Workspace mode</legend>
          <label className={choice === "local" ? "selected" : ""}>
            <input type="radio" name="observability-mode" checked={choice === "local"} onChange={() => setChoice("local")} />
            <span><Icon name="file" /></span>
            <div><strong>Work locally</strong><small>Import, validate, plan, run, compare, and export on this device. Cloud sync and the Evidence Assistant stay off.</small></div>
          </label>
          <label className={choice === "cloud" ? "selected" : ""}>
            <input type="radio" name="observability-mode" checked={choice === "cloud"} onChange={() => setChoice("cloud")} disabled={!isDesktop} />
            <span><Icon name="trace" /></span>
            <div><strong>W&amp;B Models + Weave</strong><small>{isDesktop ? "Optional cloud mirror. Sync and assistant access remain off until the target is exactly verified Restricted." : "Desktop app required. Browser preview cannot access the keychain, verify a Restricted project, or upload evidence."}</small></div>
          </label>
        </fieldset>

        {choice === "local" && (
          <div className="wizard-disclosure">
            <Icon name="shield" />
            <div><strong>Keep new engineering evidence on this device</strong><p>Continuing locally stops new W&amp;B uploads and turns off the Evidence Assistant for this session. Versions already uploaded remain in W&amp;B under that deployment's storage and deletion policy. Local models, runs, QA records, and exports remain usable and authoritative.</p></div>
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
              <div className="credential-boundary"><Icon name="shield" /><p><strong>{credentialStored ? "API key stored securely." : "API key required."}</strong> The native host collects the key in a masked dialog and stores it under <code>{credentialId}</code>. React never receives it. The host transfers it to the worker in memory, and the worker exposes it only temporarily to the W&amp;B SDK during authenticated calls.</p>{credentialStored && <button type="button" className="text-button" onClick={deleteCredential} disabled={busy}>Remove stored key</button>}</div>
            </> : <div className="credential-boundary"><Icon name="shield" /><p><strong>No API key in browser preview.</strong> This path validates labels and W&B/Weave links locally; it cannot authenticate or send data.</p></div>}
            <div className="wizard-disclosure"><Icon name="info" /><div><strong>Authorized evidence scope</strong><p>The release contract covers models, GIS/data assets, storms, INP/RPT/OUT files, exact results, QA, search traces, decisions, and exports. Conceptual-data projection, guided interaction events, and browser-downloaded learning summaries remain unfinished. Secrets, environment contents, local absolute paths, and raw console logs are excluded.</p></div></div>
            <div className="wizard-disclosure"><Icon name="warning" /><div><strong>Exact-scope gate</strong><p>The application accepts only an authoritative <code>Restricted</code> visibility result. A private label or user attestation is not enough. If the deployment cannot prove the exact scope, the project stays local and no upload or assistant call is permitted.</p></div></div>
            {projectSettingsUrl && <div className="credential-boundary"><Icon name="trace" /><p><strong>Set visibility in W&amp;B.</strong> Open the project settings, select <code>Restricted</code>, then return here to verify. W&amp;B currently exposes this control in its application rather than its supported Models SDK.</p><a className="text-button" href={projectSettingsUrl} target="_blank" rel="noreferrer">Open W&amp;B project settings</a></div>}
            <label className="disclosure-check"><input type="checkbox" checked={disclosureAccepted} onChange={(event) => setDisclosureAccepted(event.target.checked)} /><span><Icon name="check" /></span><p>I authorize full engineering-evidence synchronization to this user-owned Restricted project and understand its storage and deletion policy.</p></label>
          </div>
        )}

        {error && <div className="wizard-error" role="alert"><Icon name="warning" /> {error}</div>}

        <footer className="wizard-footer">
          <p>{choice === "local" ? "You can add a verified cloud mirror later without changing local engineering evidence." : "The binding becomes immutable after the first successful artifact upload. Credentials are never written to browser storage, SQLite, logs, or exports."}</p>
          <button className="primary-button" disabled={!canSave || busy} onClick={save}>
            {busy ? "Checking…" : choice === "local" ? "Continue locally" : !credentialStored ? "Enter key and verify project" : activeProject ? "Verify and bind project" : "Verify Restricted project"}
            <Icon name="arrow" />
          </button>
        </footer>
      </section>
    </div>
  );
}

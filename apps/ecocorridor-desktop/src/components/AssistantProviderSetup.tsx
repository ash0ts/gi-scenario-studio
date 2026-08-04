import { useEffect, useId, useRef, useState } from "react";

import { AssistantStatusSchema, type AssistantStatus, validProviderConfiguration } from "../lib/assistant";
import { credentialStatus, hasNativeCredentialHost, hostErrorMessage, hostRequest, modelProviderCredentialId, rpcData, storeCredential } from "../lib/host";
import type { RpcPayload } from "../types";
import { Icon } from "./Icon";

interface AssistantProviderSetupProps {
  projectHandle: string | null;
  allowConfiguration: boolean;
  unavailableReason: string;
  onStatus: (status: AssistantStatus | null) => void;
}

export function AssistantProviderSetup({ projectHandle, allowConfiguration, unavailableReason, onStatus }: AssistantProviderSetupProps) {
  const id = useId();
  const statusCallback = useRef(onStatus);
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [state, setState] = useState<"checking" | "idle" | "storing" | "configuring" | "error">("checking");
  const [message, setMessage] = useState("");
  const [editing, setEditing] = useState(false);
  const [baseUrl, setBaseUrl] = useState("https://api.openai.com/v1");
  const [model, setModel] = useState("");
  const [credentialStored, setCredentialStored] = useState(false);
  const credentialHost = hasNativeCredentialHost();
  const configurationValid = validProviderConfiguration(baseUrl.trim(), model.trim());
  const credentialId = projectHandle ? modelProviderCredentialId(projectHandle, baseUrl.trim(), model.trim()) : "";

  useEffect(() => { statusCallback.current = onStatus; }, [onStatus]);

  useEffect(() => {
    if (!projectHandle) {
      setStatus(null);
      setState("idle");
      statusCallback.current(null);
      return;
    }
    let active = true;
    setState("checking");
    setMessage("");
    void hostRequest<RpcPayload<unknown>>("assistant.status", { project_handle: projectHandle })
      .then((response) => {
        if (!active) return;
        const next = AssistantStatusSchema.parse(rpcData(response));
        setStatus(next);
        if (next.provider) {
          setBaseUrl(next.provider.base_url);
          setModel(next.provider.model);
        }
        setEditing(!next.configured);
        setState("idle");
        statusCallback.current(next);
      })
      .catch((reason: unknown) => {
        if (!active) return;
        setStatus(null);
        setState("error");
        setMessage(hostErrorMessage(reason, "Assistant status could not be read from the scientific worker."));
        statusCallback.current(null);
      });
    return () => { active = false; };
  }, [projectHandle]);

  useEffect(() => {
    setCredentialStored(false);
    if (!credentialHost || !credentialId || !configurationValid) return;
    let active = true;
    void credentialStatus(credentialId, "model_provider").then((available) => {
      if (active) setCredentialStored(available);
    }).catch(() => {
      if (active) setCredentialStored(false);
    });
    return () => { active = false; };
  }, [credentialHost, credentialId, configurationValid]);

  const storeKey = async () => {
    if (!configurationValid || !credentialId) return;
    setState("storing");
    setMessage("");
    try {
      await storeCredential(credentialId, "model_provider");
      const available = await credentialStatus(credentialId, "model_provider");
      if (!available) throw new Error("The operating-system keychain did not confirm the model-provider credential.");
      setCredentialStored(true);
      setState("idle");
      setMessage("Provider key stored in the operating-system keychain. It was not returned to React.");
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The model-provider key could not be stored securely."));
    }
  };

  const configure = async () => {
    if (!projectHandle || !configurationValid || !credentialStored) return;
    setState("configuring");
    setMessage("");
    try {
      const response = await hostRequest<RpcPayload<unknown>>("assistant.configure", {
        project_handle: projectHandle,
        credential_id: credentialId,
        configuration: { base_url: baseUrl.trim().replace(/\/$/, ""), model: model.trim(), timeout_seconds: 60 },
      });
      const next = AssistantStatusSchema.parse(rpcData(response));
      if (!next.configured || next.trace_required !== true || !next.provider) {
        throw new Error("The worker did not confirm a trace-required provider session.");
      }
      setStatus(next);
      setEditing(false);
      setState("idle");
      setMessage("Trace-required assistant session configured for this project. No untraced fallback is enabled.");
      statusCallback.current(next);
    } catch (reason) {
      setState("error");
      setMessage(hostErrorMessage(reason, "The trace-required assistant session could not be configured."));
    }
  };

  if (status?.configured && !editing) {
    return (
      <div className="assistant-provider-state assistant-provider-ready">
        <Icon name="check" />
        <div><strong>Traced provider ready</strong><span>{status.provider?.model} · Weave read-back required for every answer</span></div>
        {allowConfiguration && <button className="text-action" type="button" onClick={() => setEditing(true)}>Change provider</button>}
      </div>
    );
  }

  return (
    <div className="assistant-provider-setup">
      <div className="assistant-provider-state">
        <Icon name={allowConfiguration ? "shield" : "lock"} />
        <div>
          <strong>{state === "checking" ? "Checking traced assistant…" : "Traced assistant is off"}</strong>
          <span>{allowConfiguration ? "Configure an OpenAI-compatible provider; Weave trace flush and read-back remain mandatory." : unavailableReason}</span>
        </div>
      </div>
      {allowConfiguration && editing && (
        <div className="assistant-config-grid">
          <label htmlFor={`${id}-base`}><span>Provider base URL *</span><input id={`${id}-base`} type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} /></label>
          <label htmlFor={`${id}-model`}><span>Explicit model ID *</span><input id={`${id}-model`} value={model} onChange={(event) => setModel(event.target.value)} placeholder="Provider model identifier" /></label>
          <div className="assistant-key-action">
            <button className="secondary-button" type="button" disabled={!credentialHost || !configurationValid || state === "storing" || state === "configuring"} onClick={() => void storeKey()}>{state === "storing" ? "Opening secure prompt…" : credentialStored ? "Replace key in keychain" : "Store key in keychain"}</button>
            <small>{credentialHost ? "The native host injects the key in memory; the renderer, SQLite, logs, exports, and command arguments never receive it." : "API-key entry is available only in the signed desktop host."}</small>
          </div>
          <button className="primary-button" type="button" disabled={!configurationValid || !credentialStored || state === "configuring"} onClick={() => void configure()}>{state === "configuring" ? "Verifying W&B + Weave…" : "Configure traced assistant"}</button>
        </div>
      )}
      {message && <p className={`operation-message ${state === "error" ? "error" : "success"}`} role={state === "error" ? "alert" : "status"}>{message}</p>}
    </div>
  );
}

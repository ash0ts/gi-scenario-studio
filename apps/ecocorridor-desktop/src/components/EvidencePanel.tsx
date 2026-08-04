import { useCallback, useEffect, useMemo, useState } from "react";
import { z } from "zod";

import { hasNativeExternalLinkHost, hostErrorMessage, hostRequest, openObservabilityReference, rpcData } from "../lib/host";
import type { EvidenceSelection, RidgewayFixture, RpcPayload } from "../types";
import { EvidenceAssistantPanel } from "./EvidenceAssistantPanel";
import { Icon } from "./Icon";
import { useDialogFocus } from "./useDialogFocus";

const resolvedEvidenceSchema = z.object({
  claim: z.object({
    claim_id: z.string().min(1),
    portfolio_id: z.string().min(1),
    scenario_id: z.string().min(1).nullable().optional(),
    pair_id: z.string().min(1).nullable().optional(),
    value: z.union([z.number().finite(), z.boolean(), z.string()]),
    units: z.string().min(1),
    reducer: z.string().min(1).optional(),
    text: z.string().min(1).optional(),
    direct_run_ids: z.array(z.string().min(1)).min(1),
    nodes: z.array(z.object({ node_id: z.string().min(1), node_type: z.string().min(1), label: z.string().min(1), role: z.string().optional(), sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(), artifact_id: z.string().optional() }).passthrough()).optional(),
    edges: z.array(z.object({ from_node_id: z.string().min(1), to_node_id: z.string().min(1), relation: z.string().min(1) }).passthrough()).optional(),
    limitations: z.array(z.string().min(1)).min(1).optional(),
  }).passthrough(),
  direct_runs: z.array(z.object({ direct_run_id: z.string().min(1), role: z.string().optional(), scenario_id: z.string().optional(), portfolio_id: z.string().optional(), qa_status: z.string().optional(), engine_manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional() }).passthrough()).optional(),
  artifact_records: z.array(z.record(z.string(), z.unknown())).optional(),
  baseline_runs: z.array(z.object({ direct_run_id: z.string().min(1) }).passthrough()).optional(),
  portfolio_runs: z.array(z.object({ direct_run_id: z.string().min(1) }).passthrough()).optional(),
  map_descriptors: z.array(z.object({ descriptor_id: z.string().min(1), label: z.string().min(1), artifact_id: z.string().optional(), sha256: z.string().regex(/^[a-f0-9]{64}$/).optional() }).passthrough()).optional(),
  engine_manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  workflow_manifest_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  evidence_index_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  qa: z.literal("ACCEPTED_DIRECT_CIRCUITSCAPE").optional(),
  cloud_references: z.object({
    state: z.literal("SYNCED"),
    analysis_run_id: z.string().min(1),
    artifacts: z.array(z.object({
      local_artifact_id: z.string().min(1),
      provider_artifact_id: z.string().min(1),
      provider_artifact_ref: z.string().min(1),
      sha256: z.string().regex(/^[a-f0-9]{64}$/),
    })),
  }).nullable().optional(),
  limitations: z.array(z.string().min(1)).min(1).optional(),
}).passthrough();

type ResolvedEvidence = z.infer<typeof resolvedEvidenceSchema>;

interface EvidencePanelProps {
  open: boolean;
  selection: EvidenceSelection;
  fixture: RidgewayFixture | null;
  verified: boolean;
  publicMode: boolean;
  projectHandle: string | null;
  allowAssistantConfiguration?: boolean;
  assistantUnavailableReason?: string;
  onClose: () => void;
}

export function EvidencePanel({ open, selection, fixture, verified, publicMode, projectHandle, allowAssistantConfiguration = false, assistantUnavailableReason = "A verified Restricted professional-project binding is required before a provider-backed traced assistant can be configured.", onClose }: EvidencePanelProps) {
  const [compact, setCompact] = useState(() => typeof window !== "undefined" && window.matchMedia("(max-width: 980px)").matches);
  const [resolutionState, setResolutionState] = useState<"idle" | "resolving" | "resolved" | "failed">("idle");
  const [resolved, setResolved] = useState<ResolvedEvidence | null>(null);
  const [resolutionError, setResolutionError] = useState("");
  const [linkState, setLinkState] = useState<"idle" | "opening" | "error">("idle");
  const [linkMessage, setLinkMessage] = useState("");
  useEffect(() => {
    const query = window.matchMedia("(max-width: 980px)");
    const update = () => setCompact(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  const stableClose = useCallback(onClose, [onClose]);
  const dialogRef = useDialogFocus(open, stableClose, compact);

  useEffect(() => {
    setResolved(null);
    setResolutionError("");
    if (!open || publicMode || !verified) {
      setResolutionState("idle");
      return;
    }
    if (!projectHandle || selection.status !== "verified") {
      setResolutionState("failed");
      setResolutionError("The authoritative project handle or verified claim identity is unavailable.");
      return;
    }
    let active = true;
    setResolutionState("resolving");
    void hostRequest<RpcPayload<unknown>>("connectivity.evidence.resolve", {
      project_handle: projectHandle,
      claim_id: selection.claimId,
    }).then((result) => {
      if (!active) return;
      const parsed = resolvedEvidenceSchema.parse(rpcData(result));
      const professional = selection.source === "PROFESSIONAL_CLAIM";
      const numericSelection = Number(selection.value);
      const projectionMismatch = !professional && (
        parsed.claim.scenario_id !== selection.scenarioId
        || !parsed.claim.direct_run_ids.includes(selection.runId)
        || parsed.claim.units !== selection.units
        || typeof parsed.claim.value !== "number"
        || !Number.isFinite(numericSelection)
        || Math.abs(parsed.claim.value - numericSelection) > 0.051
      );
      const referenceToleranceMismatch = professional && typeof selection.value === "boolean" && (
        parsed.claim.scenario_id !== selection.scenarioId
        || !parsed.claim.direct_run_ids.includes(selection.runId)
        || parsed.claim.units !== selection.units
        || parsed.claim.value !== selection.value
      );
      const professionalGraphMissing = professional
        && (!parsed.claim.nodes?.length || !parsed.claim.edges?.length || !(parsed.claim.limitations?.length || parsed.limitations?.length));
      const guidedPackageMissing = !professional && (
        !parsed.direct_runs || parsed.direct_runs.length < 2
        || !parsed.artifact_records
        || !parsed.engine_manifest_sha256
        || !parsed.workflow_manifest_sha256
        || !parsed.evidence_index_sha256
        || parsed.qa !== "ACCEPTED_DIRECT_CIRCUITSCAPE"
        || !parsed.limitations?.length
      );
      if (parsed.claim.claim_id !== selection.claimId || parsed.claim.portfolio_id !== selection.portfolioId || projectionMismatch || referenceToleranceMismatch || professionalGraphMissing || guidedPackageMissing) {
        throw new Error("The resolved claim does not match the selected projection value and direct-run identity.");
      }
      setResolved(parsed);
      setResolutionState("resolved");
    }).catch((reason: unknown) => {
      if (!active) return;
      setResolved(null);
      setResolutionState("failed");
      setResolutionError(hostErrorMessage(reason, "The local evidence chain could not be resolved."));
    });
    return () => { active = false; };
  }, [open, projectHandle, publicMode, selection, verified]);

  const claimReady = publicMode ? verified : resolutionState === "resolved";
  const toleranceDisposition = (resolved?.claim.units ?? selection.units) === "within_tolerance"
    && typeof (resolved?.claim.value ?? selection.value) === "boolean";
  const toleranceValue = resolved?.claim.value ?? selection.value;
  const claimValue = toleranceDisposition
    ? toleranceValue ? "Within tolerance" : "Outside tolerance"
    : resolved
      ? typeof resolved.claim.value === "number" ? resolved.claim.value.toFixed(1) : String(resolved.claim.value)
      : String(selection.value);
  const claimUnits = toleranceDisposition ? "reviewed Boolean tolerance disposition" : resolved?.claim.units ?? selection.units;
  const directRunIds = useMemo(
    () => resolved?.claim.direct_run_ids ?? [selection.runId, selection.baselineRunId].filter((item): item is string => Boolean(item)),
    [resolved, selection.baselineRunId, selection.runId],
  );
  const evidenceNodeLabels = useMemo(
    () => new Map((resolved?.claim.nodes ?? []).map((node) => [node.node_id, node.label])),
    [resolved],
  );
  const openCloudReference = async (kind: "run" | "artifact", referenceId: string) => {
    if (!projectHandle) return;
    setLinkState("opening");
    setLinkMessage("");
    try {
      await openObservabilityReference(projectHandle, kind, referenceId);
      setLinkState("idle");
      setLinkMessage(`Opened the confirmed W&B ${kind} reference in the system browser.`);
    } catch (reason) {
      setLinkState("error");
      setLinkMessage(hostErrorMessage(reason, "The confirmed W&B reference could not be opened."));
    }
  };
  if (!open) return null;

  const content = (
    <div className="evidence-content">
      <div className="evidence-topline">
        <span className="evidence-mark"><Icon name="evidence" /></span>
        <span>Evidence Rail</span>
        <button className="icon-button" type="button" onClick={onClose} aria-label="Close evidence"><Icon name="close" /></button>
      </div>
      <div className="evidence-title">
        <span className={`claim-state claim-${claimReady ? "verified" : "projection"}`}>{claimReady ? (publicMode ? "Pinned public manifest" : "Worker evidence resolved") : resolutionState === "resolving" ? "Resolving direct evidence" : "Evidence unavailable"}</span>
        <h2 tabIndex={-1}>{selection.title}</h2>
        {claimReady ? <p className="claim-value"><strong>{claimValue}</strong> <span>{claimUnits}</span></p> : <p className="claim-value"><strong>Withheld</strong> <span>until the exact evidence chain resolves</span></p>}
      </div>
      {!claimReady && (
        <div className="rail-warning" role={resolutionState === "failed" ? "alert" : "note"}>
          <Icon name="warning" />
          <p><strong>{resolutionState === "resolving" ? "Resolving against the authoritative worker…" : "Not a displayable engineering claim."}</strong> {resolutionError || "The projection and exact retained evidence have not both passed verification."}</p>
        </div>
      )}
      {claimReady && <>
        <dl className="evidence-fields">
          <div><dt>Claim ID</dt><dd className="mono">{selection.claimId}</dd></div>
          <div><dt>Portfolio</dt><dd className="mono">{selection.portfolioId}</dd></div>
          <div><dt>Scenario</dt><dd className="mono">{(resolved?.claim.scenario_id ?? selection.scenarioId) || "Portfolio-level rule"}</dd></div>
          <div><dt>Direct-run reference{directRunIds.length > 1 ? "s" : ""}</dt><dd className="mono">{directRunIds.join(" · ")}</dd></div>
          <div><dt>Reducer / claim</dt><dd>{resolved?.claim.reducer ?? resolved?.claim.text ?? selection.reducer}</dd></div>
          {resolved?.claim.pair_id && <div><dt>Controlling pair</dt><dd className="mono">{resolved.claim.pair_id}</dd></div>}
        </dl>
        <section className="rail-section" aria-labelledby="source-chain-heading">
          <h3 id="source-chain-heading">Resolved source chain</h3>
          <ol className="source-chain">
            <li><span>01</span><div><strong>Claim and decision rule</strong><small>{selection.claimId}</small></div></li>
            <li><span>02</span><div><strong>Direct Circuitscape runs</strong><small>{directRunIds.join(" · ")}</small></div></li>
            <li><span>03</span><div><strong>Scenario + reviewed portfolio mutation</strong><small>{(resolved?.claim.scenario_id ?? selection.scenarioId) || "Portfolio-level reducer"} · exact graph below</small></div></li>
            <li><span>04</span><div><strong>Resistance and focal assets</strong><small>{resolved?.claim.nodes ? `${resolved.claim.nodes.filter((node) => node.node_type === "ASSET" || node.node_type === "RASTER_DIFF").length} resolved asset/diff nodes` : resolved ? `${resolved.artifact_records?.length ?? 0} retained artifact records` : "Pinned synthetic public evidence package"}</small></div></li>
            <li><span>05</span><div><strong>Engine, QA, assumptions</strong><small>{resolved?.engine_manifest_sha256 ? `${resolved.qa} · ${resolved.engine_manifest_sha256.slice(0, 12)}…` : resolved?.claim.nodes ? `${resolved.claim.nodes.length} evidence nodes · ${resolved.claim.edges?.length ?? 0} links` : `Manifest ${fixture?.manifestSha256?.slice(0, 12)}…`}</small></div></li>
          </ol>
        </section>
        {resolved?.claim.nodes?.length && <section className="rail-section" aria-labelledby="evidence-graph-heading">
          <h3 id="evidence-graph-heading">Evidence graph inventory</h3>
          <p className="rail-section-intro">Typed nodes and directed relationships show exactly how this claim reaches retained runs, mutations, source assets, QA, and authorizations.</p>
          {compact ? <ul className="rail-node-cards" aria-label="Retained evidence nodes">{resolved.claim.nodes.map((node) => {
            const identity = node.sha256 ? `sha256:${node.sha256}` : node.artifact_id ?? node.node_id;
            return <li key={node.node_id}>
              <div className="node-card-heading"><span>{node.node_type.replaceAll("_", " ")}</span><strong>{node.label}</strong></div>
              <dl>{node.role && <div><dt>Role</dt><dd>{node.role.replaceAll("_", " ")}</dd></div>}<div><dt>Exact identity</dt><dd className="mono">{identity}</dd></div></dl>
            </li>;
          })}</ul> : <div className="rail-table-scroll" tabIndex={0}><table><thead><tr><th>Type</th><th>Retained node</th><th>Identity</th></tr></thead><tbody>{resolved.claim.nodes.map((node) => <tr key={node.node_id}><td>{node.node_type.replaceAll("_", " ")}</td><td><strong>{node.label}</strong>{node.role && <span>{node.role.replaceAll("_", " ")}</span>}</td><td className="mono">{node.sha256 ? `sha256:${node.sha256.slice(0, 12)}…` : node.artifact_id ?? node.node_id}</td></tr>)}</tbody></table></div>}
          <small className="graph-count">{resolved.claim.nodes.length} nodes · {resolved.claim.edges?.length ?? 0} directed evidence links</small>
          {resolved.claim.edges?.length ? <details className="evidence-relations">
            <summary>Inspect {resolved.claim.edges.length} directed relationship{resolved.claim.edges.length === 1 ? "" : "s"}</summary>
            <ol>{resolved.claim.edges.map((edge, index) => <li key={`${edge.from_node_id}-${edge.relation}-${edge.to_node_id}-${index}`}>
              <span className="relation-node"><strong>{evidenceNodeLabels.get(edge.from_node_id) ?? edge.from_node_id}</strong><small className="mono">{edge.from_node_id}</small></span>
              <span className="relation-arrow"><b>{edge.relation.replaceAll("_", " ")}</b><i aria-hidden="true">→</i></span>
              <span className="relation-node"><strong>{evidenceNodeLabels.get(edge.to_node_id) ?? edge.to_node_id}</strong><small className="mono">{edge.to_node_id}</small></span>
            </li>)}</ol>
          </details> : null}
        </section>}
        {resolved?.direct_runs?.length && <section className="rail-section" aria-labelledby="direct-run-inventory-heading">
          <h3 id="direct-run-inventory-heading">Baseline and portfolio runs</h3>
          <div className="rail-table-scroll" tabIndex={0}><table><thead><tr><th>Role</th><th>Scenario</th><th>Run / QA</th></tr></thead><tbody>{resolved.direct_runs.map((run) => <tr key={run.direct_run_id}><td>{run.role?.replaceAll("_", " ") ?? "Direct run"}</td><td className="mono">{run.scenario_id ?? resolved.claim.scenario_id ?? "—"}</td><td><strong className="mono">{run.direct_run_id}</strong><span>{run.qa_status ?? "Retained exact result"}</span></td></tr>)}</tbody></table></div>
        </section>}
        {resolved?.map_descriptors?.length && <section className="rail-section" aria-labelledby="diagnostic-map-heading">
          <h3 id="diagnostic-map-heading">Diagnostic map evidence</h3>
          <ul className="map-descriptor-list">{resolved.map_descriptors.map((descriptor) => <li key={descriptor.descriptor_id}><Icon name="map" /><span><strong>{descriptor.label}</strong><small className="mono">{descriptor.sha256 ? `sha256:${descriptor.sha256.slice(0, 12)}…` : descriptor.artifact_id ?? descriptor.descriptor_id}</small></span></li>)}</ul>
          <p>Current-density maps are diagnostic visualizations, not objectives, observed use, movement predictions, or collision evidence.</p>
        </section>}
      </>}
      <section className="rail-section">
        <h3>Assumption boundary</h3>
        <p>{selection.assumption}</p>
        {(resolved?.claim.limitations ?? resolved?.limitations ?? (fixture ? [fixture.disclaimer] : [])).map((limitation) => <p key={limitation}>{limitation}</p>)}
        <p>A direct Circuitscape result is not observed animal movement, crossing use, collision reduction, population viability, biodiversity benefit, or an approved corridor.</p>
      </section>
      {!publicMode && <EvidenceAssistantPanel projectHandle={projectHandle} evidenceId={selection.claimId} claimReady={claimReady} allowConfiguration={allowAssistantConfiguration} unavailableReason={assistantUnavailableReason} />}
      <div className="rail-footer">
        {resolved?.cloud_references && hasNativeExternalLinkHost() ? <>
          <button className="secondary-button" type="button" disabled={linkState === "opening"} onClick={() => void openCloudReference("run", resolved.cloud_references!.analysis_run_id)}>
            <Icon name="external" /> Open analysis run
          </button>
          <button className="secondary-button" type="button" disabled={linkState === "opening" || !resolved.cloud_references.artifacts.length} title={resolved.cloud_references.artifacts.length ? undefined : "This synchronized analysis receipt has no confirmed output artifact reference."} onClick={() => { const artifact = resolved.cloud_references?.artifacts[0]; if (artifact) void openCloudReference("artifact", artifact.provider_artifact_id); }}>
            <Icon name="external" /> Open artifact lineage
          </button>
        </> : <button className="secondary-button" type="button" disabled title={publicMode ? "The public replay makes no cloud calls." : "Cloud navigation requires a confirmed synchronized W&B receipt and the signed native host."}>
          <Icon name="external" /> Open artifact lineage
        </button>}
        <small>{publicMode ? "The public replay makes no cloud calls." : resolved?.cloud_references ? "Links are resolved from the worker's retained W&B receipt; local evidence remains authoritative." : "Local evidence is authoritative; cloud lineage unlocks only after a confirmed synchronized artifact reference exists."}</small>
        {linkMessage && <small className={linkState === "error" ? "operation-message error" : "operation-message success"} role={linkState === "error" ? "alert" : "status"}>{linkMessage}</small>}
      </div>
    </div>
  );

  if (compact) {
    return (
      <div className="dialog-backdrop evidence-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
        <div ref={dialogRef} className="evidence-dialog" role="dialog" aria-modal="true" aria-label="Evidence details">
          {content}
        </div>
      </div>
    );
  }
  return <aside className="evidence-rail" aria-label="Evidence details">{content}</aside>;
}

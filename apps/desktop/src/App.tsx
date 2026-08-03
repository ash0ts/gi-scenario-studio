import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { Home } from "./components/Home";
import { ObservabilityWizard } from "./components/ObservabilityWizard";
import { buildGuidedData, type GuidedData, type GuidedFixture } from "./data/demo";
import { localObservabilityState, parseObservabilityAccountState, parseProjectCloudSyncState, type ProjectCloudSyncState } from "./lib/observability";
import { importedModelSummaries } from "./lib/practitioner";
import { transport } from "./lib/transport";
import type { BundleInspection, ObservabilityState, ProjectKind, ProjectSummary, StagedSwmmBundle } from "./types";

const Workspace = lazy(() => import("./components/Workspace").then((module) => ({ default: module.Workspace })));

export function App() {
  const [project, setProject] = useState<ProjectSummary | null>(null);
  const [guidedData, setGuidedData] = useState<GuidedData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [observability, setObservability] = useState<ObservabilityState>(localObservabilityState());
  const [observabilityReady, setObservabilityReady] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [projectCloudSync, setProjectCloudSync] = useState<ProjectCloudSyncState | null>(null);
  const [projectCloudSyncBusy, setProjectCloudSyncBusy] = useState(false);
  const [projectCloudSyncError, setProjectCloudSyncError] = useState<string | null>(null);
  const openWizard = useCallback(() => setWizardOpen(true), []);
  const closeWizard = useCallback(() => setWizardOpen(false), []);

  useEffect(() => {
    let active = true;
    transport.request<unknown>("observability.get_status", { scope: "account" })
      .then((raw) => {
        if (!active) return;
        const status = transport.mode === "tauri" ? parseObservabilityAccountState(raw) : raw as ObservabilityState;
        setObservability(status);
        setWizardOpen(false);
      })
      .catch(() => {
        if (!active) return;
        setObservability(localObservabilityState());
        setWizardOpen(false);
      })
      .finally(() => active && setObservabilityReady(true));
    return () => { active = false; };
  }, []);

  const readProjectCloudSync = useCallback(async (target: ProjectSummary): Promise<ProjectCloudSyncState | null> => {
    if (transport.mode === "browser-demo") return null;
    if (!target.projectDirectory) throw new Error("The worker did not return an authoritative project directory.");
    const raw = await transport.request<unknown>("observability.get_status", {
      scope: "project",
      project_directory: target.projectDirectory,
      project_id: target.id,
    });
    const next = parseProjectCloudSyncState(raw);
    if (next.project_id !== target.id) throw new Error("The cloud status belongs to a different project revision.");
    return next;
  }, []);

  const refreshProjectCloudSync = useCallback(async (showBusy = true) => {
    if (!project || transport.mode === "browser-demo") return;
    if (showBusy) setProjectCloudSyncBusy(true);
    try {
      const next = await readProjectCloudSync(project);
      setProjectCloudSync(next);
      setProjectCloudSyncError(null);
    } catch (reason) {
      setProjectCloudSyncError(reason instanceof Error ? reason.message : "Project sync status could not be refreshed.");
    } finally {
      if (showBusy) setProjectCloudSyncBusy(false);
    }
  }, [project, readProjectCloudSync]);

  const retryProjectCloudSync = useCallback(async () => {
    if (!project || !project.projectDirectory || transport.mode === "browser-demo") return;
    setProjectCloudSyncBusy(true);
    try {
      await transport.request("observability.retry", {
        project_directory: project.projectDirectory,
        project_id: project.id,
      });
      const next = await readProjectCloudSync(project);
      setProjectCloudSync(next);
      setProjectCloudSyncError(null);
    } catch (reason) {
      setProjectCloudSyncError(reason instanceof Error ? reason.message : "Failed evidence uploads could not be retried.");
    } finally {
      setProjectCloudSyncBusy(false);
    }
  }, [project, readProjectCloudSync]);

  const handleProjectCloudAction = useCallback(() => {
    if (projectCloudSync?.state === "FAILED_RETRYING") {
      void retryProjectCloudSync();
    } else {
      void refreshProjectCloudSync();
    }
  }, [projectCloudSync?.state, refreshProjectCloudSync, retryProjectCloudSync]);

  useEffect(() => {
    if (!project || transport.mode === "browser-demo") return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void refreshProjectCloudSync(false);
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [project, refreshProjectCloudSync]);

  const handleObservabilityChange = useCallback((next: ObservabilityState) => {
    setObservability(next);
    if (project && transport.mode === "tauri") void refreshProjectCloudSync(false);
  }, [project, refreshProjectCloudSync]);

  const start = async (kind: ProjectKind) => {
    setError(null);
    try {
      if (kind === "demo") {
        const created = await transport.request<ProjectSummary>("project.create", { kind });
        const cloudStatus = await readProjectCloudSync(created);
        const opened = await transport.request<{
          fixture: GuidedFixture;
          cloud_sync_state?: string;
          cloud_error_code?: string;
        }>("guided.open", {
          project_directory: created.projectDirectory ?? created.location,
          project_id: created.id,
        });
        if (!opened.fixture) {
          throw new Error("The guided worker response did not include its hash-verified evidence projection.");
        }
        const openedCloudStatus = await readProjectCloudSync(created);
        setGuidedData(buildGuidedData(opened.fixture));
        setProjectCloudSync(openedCloudStatus);
        setProjectCloudSyncError(opened.cloud_error_code
          ? `Guided observability projection failed (${opened.cloud_error_code}); the local verified replay remains available.`
          : null);
        setProject(created);
        return;
      }
      let stagedBundle: StagedSwmmBundle | null = null;
      let inspectedBundle: BundleInspection | null = null;
      if (kind === "existing" && transport.mode === "tauri") {
        stagedBundle = await invoke<StagedSwmmBundle | null>("select_swmm_bundle");
        if (!stagedBundle) return;
        inspectedBundle = await transport.request<BundleInspection>("bundle.inspect", {
          archive_path: stagedBundle.stagedPath,
          expected_sha256: stagedBundle.sha256,
          expected_bytes: stagedBundle.bytes,
        });
        if (!inspectedBundle.valid) {
          const missing = inspectedBundle.models.flatMap((model) => model.missing);
          throw new Error(missing.length
            ? `The bundle is missing referenced files: ${missing.slice(0, 5).join(", ")}. Add them beside the INP and choose the ZIP again.`
            : "The ZIP does not contain a SWMM INP model. Add the model and every referenced file, then choose it again.");
        }
      }
      const created = await transport.request<ProjectSummary>("project.create", { kind });
      let openedProject = created;
      if (kind === "existing" && stagedBundle && inspectedBundle) {
        if (!created.projectDirectory) throw new Error("The worker did not return an authoritative project directory.");
        const imported = await transport.request<BundleInspection>("bundle.import", {
          archive_path: stagedBundle.stagedPath,
          expected_sha256: stagedBundle.sha256,
          expected_bytes: stagedBundle.bytes,
          destination: `${created.projectDirectory.replace(/[\\/]+$/, "")}/sources/imported`,
          project_directory: created.projectDirectory,
          project_id: created.id,
        });
        if (imported.archive_sha256 !== stagedBundle.sha256 || imported.archive_bytes !== stagedBundle.bytes) {
          throw new Error("The staged SWMM bundle changed before import. Select the ZIP again.");
        }
        const selectedProjectPath = imported.project_state?.selected_base_model;
        const models = importedModelSummaries(imported.models).map((model) =>
          selectedProjectPath === model.projectPath && imported.project_state?.selected_model_dependencies
            ? { ...model, dependencies: imported.project_state.selected_model_dependencies }
            : model,
        );
        const baseModel = imported.project_state?.selected_base_model
          ? imported.project_state.selected_base_model.replace(/^sources\/imported\//, "")
          : imported.models.length === 1 ? imported.models[0].model_path : undefined;
        const validation = imported.model_validation;
        openedProject = {
          ...created,
          location: imported.models.length === 1
            ? `Imported · ${imported.files.length} files · dependency-complete`
            : `Imported · ${imported.models.length} models · base selection required`,
          modelFile: baseModel ?? `${imported.models.length} SWMM models require selection`,
          bundle: {
            displayName: stagedBundle.displayName,
            sha256: imported.archive_sha256,
            fileCount: imported.files.length,
            modelCount: imported.models.length,
            dependencyCount: imported.models.reduce((total, model) => total + model.resolved.length, 0),
            dependencyComplete: imported.valid,
            baseModel,
            blockingCount: validation?.blocking_count,
            warningCount: validation?.warning_count,
            structurallyRunnable: validation?.structurally_runnable,
            models,
            baseModelSelectionRequired: imported.project_state?.base_model_selection_required ?? false,
            currentModelRevisionId: imported.project_state?.current_model_revision_id,
            modelFitnessPassportId: imported.project_state?.model_fitness_passport_id,
          },
        };
      }
      const cloudStatus = await readProjectCloudSync(openedProject);
      setGuidedData(null);
      setProjectCloudSync(cloudStatus);
      setProjectCloudSyncError(null);
      setProject(openedProject);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The project could not be opened.");
    }
  };

  return (
    <>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      {error && <div className="global-error" role="alert">{error}</div>}
      {project
        ? <Suspense fallback={<main className="workspace-loading" id="main-content" aria-busy="true">Opening the evidence workspace…</main>}><Workspace project={project} onProjectUpdate={setProject} guidedData={guidedData} observability={observability} projectCloudSync={projectCloudSync} projectCloudSyncBusy={projectCloudSyncBusy} projectCloudSyncError={projectCloudSyncError} onRefreshProjectCloudSync={handleProjectCloudAction} onObservability={openWizard} onHome={() => { setProject(null); setGuidedData(null); setProjectCloudSync(null); setProjectCloudSyncError(null); }} /></Suspense>
        : <Home observability={observability} desktopAvailable={transport.mode === "tauri"} onObservability={openWizard} onStart={start} />}
      {observabilityReady && (
        <ObservabilityWizard
          open={wizardOpen}
          state={observability}
          required={false}
          activeProject={project?.projectDirectory ? {
            id: project.id,
            projectDirectory: project.projectDirectory,
          } : null}
          onClose={closeWizard}
          onChange={handleObservabilityChange}
          onProjectCloudChange={(next) => {
            setProjectCloudSync(next);
            setProjectCloudSyncError(null);
          }}
        />
      )}
    </>
  );
}

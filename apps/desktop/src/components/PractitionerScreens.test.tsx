import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ProjectSummary } from "../types";
import { initialStormCases, importedModelSummaries } from "../lib/practitioner";
import { PractitionerModelScreen, PractitionerStormsScreen } from "./PractitionerScreens";

const multiModelProject = (): ProjectSummary => ({
  id: "project-1",
  name: "Imported alternatives",
  kind: "existing",
  fitnessDisposition: "not_assessed",
  location: "Imported",
  projectDirectory: "/project",
  modelFile: "2 SWMM models require selection",
  updatedAt: "2026-08-03T00:00:00Z",
  bundle: {
    displayName: "alternatives.zip",
    sha256: "a".repeat(64),
    fileCount: 2,
    modelCount: 2,
    dependencyCount: 0,
    dependencyComplete: true,
    baseModelSelectionRequired: true,
    models: [
      { modelPath: "baseline.inp", projectPath: "sources/imported/baseline.inp", references: [], resolved: [], dependencies: {} },
      { modelPath: "storm-10yr.inp", projectPath: "sources/imported/storm-10yr.inp", references: [], resolved: [], dependencies: {} },
    ],
  },
});

describe("PractitionerModelScreen base-model gate", () => {
  it("requires an explicit imported-model selection before checks or continuation", () => {
    const markup = renderToStaticMarkup(<PractitionerModelScreen
      project={multiModelProject()}
      validation={null}
      passport={null}
      busy={false}
      fitnessBusy={false}
      error={null}
      fitnessError={null}
      onSelectBaseModel={vi.fn()}
      onValidate={vi.fn()}
      onAttest={vi.fn()}
      onEvidence={vi.fn()}
      onStep={vi.fn()}
    />);

    expect(markup).toContain("Choose the unchanged reference model");
    expect(markup.match(/type="radio"/g)).toHaveLength(2);
    expect(markup).toContain("Use selected model and run checks");
    expect(markup).toContain("Select the authoritative base model before continuing");
    expect(markup).toContain("unclassified companion model");
    expect(markup).toContain("Run model checks");
    expect(markup.match(/disabled=""/g)?.length).toBeGreaterThanOrEqual(3);
  });

  it("shows companion INPs without offering them as storm variants", () => {
    const models = importedModelSummaries([
      { model_path: "baseline.inp", references: [], resolved: [], missing: [] },
      { model_path: "storm-10yr.inp", references: [], resolved: [], missing: [] },
    ]);
    const storms = initialStormCases(models, models[0].projectPath);
    storms[0].validation = {
      issues: [],
      checks: [],
      section_names: ["OPTIONS"],
      object_counts: {},
      blocking_count: 0,
      warning_count: 0,
      structurally_runnable: true,
    };

    const markup = renderToStaticMarkup(<PractitionerStormsScreen
      storms={storms}
      busy={false}
      error={null}
      onChange={vi.fn()}
      onValidate={vi.fn()}
      onStep={vi.fn()}
    />);

    expect(markup).toContain("One authoritative case; companions remain blocked");
    expect(markup).toContain("unclassified companion");
    expect(markup).toContain("Semantic check required");
    expect(markup).not.toContain("Imported storm variants");
    expect(markup).not.toContain("type=\"checkbox\"");
  });
});

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { evidenceGuideUnavailableReason } from "../lib/evidenceGuide";
import { localObservabilityState } from "../lib/observability";
import type { EvidenceItem } from "../types";
import { EvidenceRail, explanationFor } from "./EvidenceRail";

const item: EvidenceItem = {
  id: "ALT-017",
  kind: "alternative",
  eyebrow: "Exact alternative",
  title: "Low-cost caution",
  subtitle: "Nondominated in the selected projection, but not acceptable",
  status: "exact",
  claim: "This alternative violates the selected-storm peak guardrail.",
  fields: [{ label: "Worst peak change", value: "+0.07 cfs", mono: true }],
};

describe("EvidenceRail contextual Evidence Guide", () => {
  it("keeps the two peak failures and ecological boundary available without AI", () => {
    expect(explanationFor(item)).toContain("3.14 to 3.21 cfs");
    expect(explanationFor(item)).toContain("13.33 to 13.34 cfs");
    const carryForward = explanationFor({ ...item, id: "ALT-207" });
    expect(carryForward).toContain("does not establish water-quality improvement");
    expect(carryForward).toContain("groundwater recharge");
    expect(carryForward).toContain("lifecycle impact");
  });

  it("keeps deterministic help and makes the traced guide contextual and read-only", () => {
    const markup = renderToStaticMarkup(
      <EvidenceRail
        item={item}
        projectId="cedar-trace"
        projectKind="demo"
        observability={localObservabilityState()}
        projectCloudSync={null}
        open
        onClose={vi.fn()}
        onEvidenceReference={vi.fn()}
      />,
    );

    expect(markup).toContain("Explain how to use this");
    expect(markup).toContain("Read-only evidence guide");
    expect(markup).toContain("Ask this record a question");
    expect(markup).toContain("cannot change the model or choose an alternative");
    expect(markup).toContain("Preview the evidence context");
    expect(markup).toContain("Guide off");
    expect(markup).not.toContain("> Traced<");
    expect(markup).toContain("Evidence Guide unavailable");
    expect(markup).toContain("Deterministic evidence remains usable");
    expect(evidenceGuideUnavailableReason("browser-demo")).toBeNull();
  });
});

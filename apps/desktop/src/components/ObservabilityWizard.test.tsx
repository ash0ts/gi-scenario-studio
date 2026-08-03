import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { localObservabilityState } from "../lib/observability";
import { ObservabilityWizard } from "./ObservabilityWizard";

describe("ObservabilityWizard local-first choice", () => {
  it("offers useful local work before the optional Restricted cloud mirror", () => {
    vi.stubGlobal("window", { __TAURI_INTERNALS__: undefined });
    const markup = renderToStaticMarkup(
      <ObservabilityWizard
        open
        state={localObservabilityState()}
        onClose={vi.fn()}
        onChange={vi.fn()}
      />,
    );

    expect(markup).toContain("Choose where project evidence lives");
    expect(markup).toContain("Work locally");
    expect(markup).toContain("Import, validate, plan, run, compare, and export on this device");
    expect(markup).toContain("Continue locally");
    expect(markup).toContain("Add W&amp;B only when you want a verified collaboration and observability mirror");
    expect(markup).toContain("Desktop app required");
    expect(markup).toContain("Browser preview cannot access the keychain, verify a Restricted project, or upload evidence");
    expect(markup).toContain('name="observability-mode" checked=""');
  });
});

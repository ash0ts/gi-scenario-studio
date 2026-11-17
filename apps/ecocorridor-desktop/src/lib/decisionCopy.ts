import type { RidgewayPortfolio } from "../types";

export function guidedDispositionLabel(
  role: RidgewayPortfolio["role"],
  ruleStatus: RidgewayPortfolio["ruleStatus"],
): string {
  if (role === "carry-forward") {
    return ruleStatus === "pass" ? "Carry-forward candidate" : "Review before carry-forward";
  }
  if (role === "over-budget") return "Over budget";
  if (role === "brittle") return "Hypothesis-sensitive";
  return "Caution";
}

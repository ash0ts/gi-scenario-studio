import { guidedDispositionLabel } from "./decisionCopy";

test("never presents the teaching carry-forward role as an imperative", () => {
  expect(guidedDispositionLabel("carry-forward", "review")).toBe("Review before carry-forward");
  expect(guidedDispositionLabel("carry-forward", "fail")).toBe("Review before carry-forward");
  expect(guidedDispositionLabel("carry-forward", "pass")).toBe("Carry-forward candidate");
  expect(guidedDispositionLabel("carry-forward", "pass")).not.toBe("Carry forward");
});

import { describe, expect, it } from "vitest";
import { parseFocalPairCsv, parsePositiveIntegerCsv } from "./focalInput";

describe("strict focal input parsing", () => {
  it("rejects every malformed focal-ID token instead of filtering it out", () => {
    expect(parsePositiveIntegerCsv("1, 2x, 3", { itemName: "Focal ID", minimumItems: 2 })).toEqual({
      ok: false,
      error: 'Focal ID "2x" must be a positive whole number.',
    });
    expect(parsePositiveIntegerCsv("1, , 3", { itemName: "Focal ID", minimumItems: 2 })).toMatchObject({ ok: false });
    expect(parsePositiveIntegerCsv("1, 0, 3", { itemName: "Focal ID", minimumItems: 2 })).toMatchObject({ ok: false });
    expect(parsePositiveIntegerCsv("1, 2, 2", { itemName: "Focal ID", minimumItems: 2 })).toMatchObject({ ok: false });
  });

  it("preserves the complete, explicitly corrected focal-ID order", () => {
    expect(parsePositiveIntegerCsv("1, 2, 3", { itemName: "Focal ID", minimumItems: 2 })).toEqual({
      ok: true,
      value: [1, 2, 3],
    });
  });

  it("rejects malformed, missing, reversed, and duplicate focal pairs without repair", () => {
    expect(parseFocalPairCsv("1:2", [1, 2, 3])).toMatchObject({ ok: false });
    expect(parseFocalPairCsv("1-4", [1, 2, 3])).toEqual({
      ok: false,
      error: 'Pair "1-4" uses focal ID 4, which is not in the focal-ID list.',
    });
    expect(parseFocalPairCsv("3-1", [1, 2, 3])).toEqual({
      ok: false,
      error: 'Pair "3-1" must list the lower focal ID first (1-3); it will not be reordered silently.',
    });
    expect(parseFocalPairCsv("1-2, 1-2", [1, 2, 3])).toMatchObject({ ok: false });
    expect(parseFocalPairCsv("1-2, 2-3", [1, 2, 3])).toEqual({
      ok: true,
      value: [
        { fromFocalId: 1, toFocalId: 2 },
        { fromFocalId: 2, toFocalId: 3 },
      ],
    });
  });
});

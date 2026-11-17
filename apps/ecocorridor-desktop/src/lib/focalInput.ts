export type StrictParseResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: string };

export interface PositiveIntegerCsvOptions {
  itemName?: string;
  minimumItems?: number;
}

export interface ParsedFocalPair {
  fromFocalId: number;
  toFocalId: number;
}

const POSITIVE_INTEGER_TOKEN = /^[1-9]\d*$/;

/**
 * Parses reviewer-entered identifiers without repairing or dropping any token.
 * Callers receive either the complete ordered list or one actionable error.
 */
export function parsePositiveIntegerCsv(
  input: string,
  { itemName = "ID", minimumItems = 1 }: PositiveIntegerCsvOptions = {},
): StrictParseResult<number[]> {
  if (!input.trim()) {
    return { ok: false, error: `Enter at least ${minimumItems} ${itemName}${minimumItems === 1 ? "" : "s"}.` };
  }

  const tokens = input.split(",");
  const values: number[] = [];
  const seen = new Set<number>();

  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index].trim();
    if (!token) {
      return { ok: false, error: `${itemName} ${index + 1} is empty. Remove the extra comma or enter a positive whole number.` };
    }
    if (!POSITIVE_INTEGER_TOKEN.test(token)) {
      return { ok: false, error: `${itemName} "${token}" must be a positive whole number.` };
    }
    const value = Number(token);
    if (!Number.isSafeInteger(value)) {
      return { ok: false, error: `${itemName} "${token}" is outside the supported whole-number range.` };
    }
    if (seen.has(value)) {
      return { ok: false, error: `${itemName} "${token}" is duplicated. Each identifier must appear once.` };
    }
    seen.add(value);
    values.push(value);
  }

  if (values.length < minimumItems) {
    return { ok: false, error: `Enter at least ${minimumItems} ${itemName}${minimumItems === 1 ? "" : "s"}.` };
  }
  return { ok: true, value: values };
}

/**
 * Parses explicit ascending focal pairs. It never reorders endpoints or ignores
 * malformed pairs, because either behavior would change the modeled question.
 */
export function parseFocalPairCsv(input: string, focalIds: readonly number[]): StrictParseResult<ParsedFocalPair[]> {
  if (!input.trim()) return { ok: false, error: "Enter at least one required focal pair." };

  const focalIdSet = new Set(focalIds);
  const seen = new Set<string>();
  const pairs: ParsedFocalPair[] = [];
  const tokens = input.split(",");

  for (let index = 0; index < tokens.length; index += 1) {
    const token = tokens[index].trim();
    if (!token) {
      return { ok: false, error: `Pair ${index + 1} is empty. Remove the extra comma or enter a pair such as 1-2.` };
    }
    const match = /^([1-9]\d*)\s*-\s*([1-9]\d*)$/.exec(token);
    if (!match) {
      return { ok: false, error: `Pair "${token}" must use two positive whole-number IDs in the form 1-2.` };
    }
    const fromFocalId = Number(match[1]);
    const toFocalId = Number(match[2]);
    if (!Number.isSafeInteger(fromFocalId) || !Number.isSafeInteger(toFocalId)) {
      return { ok: false, error: `Pair "${token}" contains an ID outside the supported whole-number range.` };
    }
    if (fromFocalId === toFocalId) {
      return { ok: false, error: `Pair "${token}" must connect two different focal IDs.` };
    }
    if (fromFocalId > toFocalId) {
      return { ok: false, error: `Pair "${token}" must list the lower focal ID first (${toFocalId}-${fromFocalId}); it will not be reordered silently.` };
    }
    const missingIds = [fromFocalId, toFocalId].filter((value) => !focalIdSet.has(value));
    if (missingIds.length) {
      return { ok: false, error: `Pair "${token}" uses focal ID${missingIds.length === 1 ? "" : "s"} ${missingIds.join(" and ")}, which ${missingIds.length === 1 ? "is" : "are"} not in the focal-ID list.` };
    }
    const key = `${fromFocalId}-${toFocalId}`;
    if (seen.has(key)) {
      return { ok: false, error: `Pair "${token}" is duplicated. Each required pair must appear once.` };
    }
    seen.add(key);
    pairs.push({ fromFocalId, toFocalId });
  }

  return { ok: true, value: pairs };
}

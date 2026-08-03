export function calculateDesignCapacity(solverJobCap: number, stormCount: number): number {
  if (!Number.isFinite(solverJobCap) || !Number.isInteger(stormCount) || stormCount <= 0) return 0;
  const availableAfterBaselines = Math.max(0, Math.floor(solverJobCap) - stormCount);
  return Math.floor(availableAfterBaselines / stormCount);
}

export function reviewChecksComplete(checks: readonly boolean[]): boolean {
  return checks.length > 0 && checks.every(Boolean);
}

export type AcceptanceStatus = "meets" | "near" | "does-not-meet";

export function acceptanceStatus(value: number, threshold = 0.7, nearThreshold = 0.72): AcceptanceStatus {
  if (value <= threshold) return "meets";
  if (value <= nearThreshold) return "near";
  return "does-not-meet";
}

export function controllingStormIndex(values: readonly number[]): number {
  if (values.length === 0) return -1;
  return values.reduce((controlling, value, index) => value > values[controlling] ? index : controlling, 0);
}

export function selectDecisionReference<T extends { cost: number; volume: number }>(
  items: readonly T[],
  threshold = 0.7,
): T | undefined {
  return items
    .filter((item) => item.volume <= threshold)
    .sort((left, right) => left.cost - right.cost || left.volume - right.volume)[0];
}

export function nondominatedIds<T extends { id: string }>(
  items: readonly T[],
  objectives: (item: T) => readonly [number, number],
): Set<string> {
  return new Set(
    items
      .filter((candidate) => {
        const [candidateFirst, candidateSecond] = objectives(candidate);
        return !items.some((other) => {
          if (other.id === candidate.id) return false;
          const [otherFirst, otherSecond] = objectives(other);
          return otherFirst <= candidateFirst
            && otherSecond <= candidateSecond
            && (otherFirst < candidateFirst || otherSecond < candidateSecond);
        });
      })
      .map((item) => item.id),
  );
}

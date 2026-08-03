export function calculateDesignCapacity(solverJobCap: number, stormCount: number): number {
  if (!Number.isFinite(solverJobCap) || !Number.isInteger(stormCount) || stormCount <= 0) return 0;
  const availableAfterBaselines = Math.max(0, Math.floor(solverJobCap) - stormCount);
  return Math.floor(availableAfterBaselines / stormCount);
}

export function attestationsComplete(attestations: readonly boolean[]): boolean {
  return attestations.length > 0 && attestations.every(Boolean);
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

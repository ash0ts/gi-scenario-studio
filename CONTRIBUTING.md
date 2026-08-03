# Contributing

Contributions are welcome under Apache-2.0. By contributing, you certify that
you have the right to submit the work and agree to the Developer Certificate of
Origin.

Engineering changes must preserve these invariants:

1. Hydraulic values exposed as results originate from completed exact SWMM jobs.
2. Conceptual models remain visibly conceptual until a human attests otherwise.
3. Unknown assumptions are never silently upgraded to confirmed data.
4. Search predictions never enter engineering result serializers.
5. Every model mutation is typed, reversible, and represented in a semantic diff.
6. Dataset visibility is not treated as redistribution permission.

Run the Python and frontend test suites before proposing a change. Add a golden
or property test for parser, mutation, cache-key, budget, or export behavior.

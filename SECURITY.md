# Security policy

GI Scenario Studio processes untrusted model bundles and invokes a hydraulic
solver. Security defects can be reported privately to the maintainers rather
than through a public issue containing a client model.

The application:

- extracts archives only after path, symlink, file-count, and expanded-size checks;
- invokes the pinned SWMM binary without a shell;
- uses isolated per-run directories and content-addressed immutable artifacts;
- performs no network access for existing-model analyses;
- requires explicit consent before a concept-model data provider is contacted;
- excludes model content from diagnostics unless the user opts in.

Do not include client models, credentials, exact asset coordinates, or permit
materials in a public vulnerability report.

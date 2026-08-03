#!/usr/bin/env python3
"""Fail CI when tracked source contains credentials or private workstation paths.

Only rule identifiers and file/line locations are printed. Matched text is
never echoed, which keeps CI logs safe even when this check catches a real key.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_SCANNED_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES = (
    Rule(
        "wandb-api-key",
        re.compile(r"(?i)WANDB_API_KEY\s*[=:]\s*['\"]?[0-9a-f]{40}(?![0-9a-f])"),
    ),
    Rule(
        "openai-api-key",
        re.compile(r"(?i)(?:OPENAI_API_KEY\s*[=:]\s*['\"]?)?sk-[A-Za-z0-9_-]{24,}"),
    ),
    Rule(
        "anthropic-api-key",
        re.compile(r"(?i)(?:ANTHROPIC_API_KEY\s*[=:]\s*['\"]?)?sk-ant-[A-Za-z0-9_-]{20,}"),
    ),
    Rule("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    Rule(
        "workspace-secret-path",
        re.compile(r"/Users/[^/]+/(?:Documents/)?common_tools/\.env"),
    ),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        name = path.name
        if name.startswith(".env") and name != ".env.example":
            findings.append((relative, 1, "tracked-env-file"))
            continue
        if not path.is_file() or path.stat().st_size > MAX_SCANNED_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            for rule in RULES:
                if rule.pattern.search(line):
                    findings.append((relative, line_number, rule.name))
    if findings:
        for relative, line_number, rule in findings:
            print(f"{relative}:{line_number}: blocked by {rule}")
        print(f"Repository secret boundary failed with {len(findings)} finding(s).")
        return 1
    print("Repository secret boundary passed; no tracked credentials or private env paths found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

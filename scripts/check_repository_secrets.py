#!/usr/bin/env python3
"""Fail CI when tracked source contains credentials or private workstation paths.

Only rule identifiers and file/line locations are printed. Matched text is
never echoed, which keeps CI logs safe even when this check catches a real key.
"""

from __future__ import annotations

import re
import subprocess
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_SCANNED_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBER_SCANNED_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_EXPANDED_BYTES = 256 * 1024 * 1024


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
    Rule(
        "github-token", re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})")
    ),
    Rule("aws-access-key", re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    Rule("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    Rule(
        "workspace-secret-path",
        re.compile(r"/Users/[^/]+/(?:Documents/)?common_tools/\.env"),
    ),
)


def repository_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def credential_container(name: str) -> bool:
    path = PurePosixPath(name)
    return path.name.casefold() in {
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    } or path.suffix.casefold() in {
        ".key",
        ".p12",
        ".pem",
        ".pfx",
    }


def unsafe_archive_member(name: str) -> bool:
    pure = PurePosixPath(name)
    return "\\" in name or pure.is_absolute() or ".." in pure.parts or not pure.name


def scan_text(identifier: str, text: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            if rule.pattern.search(line):
                findings.append((identifier, line_number, rule.name))
    return findings


def scan_archive(path: Path, relative: str) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    records: list[tuple[str, int, object]] = []
    try:
        if relative.casefold().endswith((".tar", ".tar.gz", ".tgz")):
            with tarfile.open(path, "r:*") as archive:
                members = archive.getmembers()
                for member in members:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        findings.append((f"{relative}!{member.name}", 1, "unsafe-archive-member"))
                        continue
                    records.append((member.name, member.size, member))
                findings.extend(_scan_archive_records(relative, archive.extractfile, records))
        elif relative.casefold().endswith(".zip"):
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                for member in members:
                    if member.is_dir():
                        continue
                    unix_mode = member.external_attr >> 16
                    if unix_mode and (unix_mode & 0o170000) == 0o120000:
                        findings.append(
                            (f"{relative}!{member.filename}", 1, "unsafe-archive-member")
                        )
                        continue
                    records.append((member.filename, member.file_size, member))
                findings.extend(_scan_archive_records(relative, archive.open, records))
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        findings.append((relative, 1, "invalid-archive"))
    return findings


def _scan_archive_records(
    relative: str,
    opener: object,
    records: list[tuple[str, int, object]],
) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    if len(records) > MAX_ARCHIVE_MEMBERS or sum(size for _, size, _ in records) > (
        MAX_ARCHIVE_EXPANDED_BYTES
    ):
        return [(relative, 1, "archive-limits-exceeded")]
    names: set[str] = set()
    for name, size, member in records:
        identifier = f"{relative}!{name}"
        if name in names or unsafe_archive_member(name):
            findings.append((identifier, 1, "unsafe-archive-member"))
            continue
        names.add(name)
        if PurePosixPath(name).name.startswith(".env") or credential_container(name):
            findings.append((identifier, 1, "credential-container"))
            continue
        if size > MAX_ARCHIVE_MEMBER_SCANNED_BYTES:
            continue
        try:
            # Both TarFile.extractfile and ZipFile.open accept their native
            # member record and return a binary stream.
            stream = opener(member)  # type: ignore[operator]
            if stream is None:
                findings.append((identifier, 1, "invalid-archive-member"))
                continue
            with stream:
                payload = stream.read(MAX_ARCHIVE_MEMBER_SCANNED_BYTES + 1)
            if len(payload) != size:
                findings.append((identifier, 1, "invalid-archive-member"))
                continue
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError, zipfile.BadZipFile):
            continue
        findings.extend(scan_text(identifier, text))
    return findings


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in repository_files():
        relative = path.relative_to(ROOT).as_posix()
        name = path.name
        if name.startswith(".env") and name != ".env.example":
            findings.append((relative, 1, "tracked-env-file"))
            continue
        if credential_container(name):
            findings.append((relative, 1, "credential-container"))
            continue
        if relative.casefold().endswith((".tar", ".tar.gz", ".tgz", ".zip")):
            findings.extend(scan_archive(path, relative))
            continue
        if not path.is_file() or path.stat().st_size > MAX_SCANNED_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(relative, text))
    if findings:
        for relative, line_number, rule in findings:
            print(f"{relative}:{line_number}: blocked by {rule}")
        print(f"Repository secret boundary failed with {len(findings)} finding(s).")
        return 1
    print("Repository secret boundary passed; no tracked credentials or private env paths found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

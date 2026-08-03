"""Isolated EPA SWMM subprocess execution and conservative report QA parsing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import ContractModel, QAPolicy


class SolverExecutionError(RuntimeError):
    pass


class QAFinding(ContractModel):
    code: str
    severity: str
    message: str
    value: float | None = None
    units: str | None = None


class SolverOutcome(ContractModel):
    return_code: int
    input_sha256: str
    solver_sha256: str
    report_sha256: str | None = None
    output_sha256: str | None = None
    stdout: str = ""
    stderr: str = ""
    qa_status: str
    findings: list[QAFinding]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def physics_cache_key(
    *,
    model_bytes: bytes,
    dependency_hashes: dict[str, str],
    solver_sha256: str,
    solver_options: dict[str, Any] | None = None,
) -> str:
    payload = {
        "model_sha256": sha256_bytes(model_bytes),
        "dependencies": dict(sorted(dependency_hashes.items())),
        "solver_sha256": solver_sha256,
        "solver_options": solver_options or {},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


CONTINUITY_RE = re.compile(
    r"Continuity Error\s*\(%\)\s*[.:= ]+\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE
)
SWMM_ERROR_RE = re.compile(r"^(?:\*+\s*)?ERROR(?:\s+\d+|\s*:)", re.IGNORECASE)


def parse_report_qa(
    report_text: str, policy: QAPolicy | dict[str, Any] | None = None
) -> list[QAFinding]:
    policy = QAPolicy.model_validate(policy or {})
    findings: list[QAFinding] = []
    section = "unknown"
    for line in report_text.splitlines():
        normalized = line.strip()
        upper = normalized.upper()
        if "RUNOFF QUANTITY CONTINUITY" in upper:
            section = "runoff"
        elif "FLOW ROUTING CONTINUITY" in upper:
            section = "flow"
        elif "QUALITY ROUTING CONTINUITY" in upper:
            section = "quality"
        match = CONTINUITY_RE.search(normalized)
        if match:
            value = abs(float(match.group(1)))
            threshold = (
                policy.runoff_continuity_warning_percent
                if section == "runoff"
                else policy.flow_continuity_warning_percent
            )
            findings.append(
                QAFinding(
                    code=f"{section}_continuity_error",
                    severity="warning" if value > threshold else "info",
                    message=f"{section.title()} continuity error is {value:g}%.",
                    value=value,
                    units="percent",
                )
            )
        if upper.startswith("WARNING"):
            findings.append(QAFinding(code="swmm_warning", severity="warning", message=normalized))
        # SWMM report tables legitimately contain columns named "Error".  Only
        # explicit solver error records (for example, ``ERROR 103: ...``) are
        # blocking; continuity is classified separately above.
        if SWMM_ERROR_RE.match(normalized):
            findings.append(QAFinding(code="swmm_error", severity="blocking", message=normalized))
    return findings


class SolverRunner:
    """Run the pinned console engine without a shell in an isolated directory."""

    def __init__(self, executable: str | Path, *, timeout_seconds: int = 3_600) -> None:
        self.executable = Path(executable).resolve()
        if not self.executable.is_file():
            raise SolverExecutionError(f"SWMM executable was not found: {self.executable}")
        self.timeout_seconds = timeout_seconds
        self.solver_sha256 = sha256_file(self.executable)

    def run(
        self,
        *,
        model_bytes: bytes,
        work_directory: str | Path,
        dependencies: dict[str, str | Path] | None = None,
        qa_policy: QAPolicy | dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SolverOutcome:
        work = Path(work_directory).resolve()
        work.mkdir(parents=True, exist_ok=True)
        input_path = work / "input.inp"
        report_path = work / "report.rpt"
        output_path = work / "output.out"
        temporary = work / ".input.inp.partial"
        temporary.write_bytes(model_bytes)
        os.replace(temporary, input_path)
        for relative, source_like in (dependencies or {}).items():
            relative_path = PurePosixPath(relative.replace("\\", "/"))
            if relative_path.is_absolute() or any(
                part in {"", ".", ".."} for part in relative_path.parts
            ):
                raise SolverExecutionError(f"unsafe dependency target: {relative!r}")
            source = Path(source_like).resolve()
            if not source.is_file():
                raise SolverExecutionError(f"dependency was not found: {source}")
            target = (work / relative_path.as_posix()).resolve()
            if work not in target.parents:
                raise SolverExecutionError(f"dependency escapes run directory: {relative!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        allowed_environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LANG", "LC_ALL")
            if key in os.environ
        }
        allowed_environment["OMP_NUM_THREADS"] = "1"
        process = subprocess.Popen(
            [str(self.executable), input_path.name, report_path.name, output_path.name],
            cwd=work,
            env=allowed_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
        deadline = time.monotonic() + self.timeout_seconds
        cancelled = False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.communicate()
                raise SolverExecutionError(
                    f"SWMM exceeded the {self.timeout_seconds}-second job timeout"
                )
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        report_text = (
            report_path.read_text(encoding="utf-8", errors="replace")
            if report_path.exists()
            else ""
        )
        findings = parse_report_qa(report_text, qa_policy)
        if cancelled:
            findings.append(
                QAFinding(
                    code="solver_cancelled",
                    severity="blocking",
                    message="SWMM execution was cancelled by the user.",
                )
            )
        elif process.returncode != 0:
            findings.append(
                QAFinding(
                    code="solver_exit_code",
                    severity="blocking",
                    message=f"SWMM exited with status {process.returncode}.",
                    value=float(process.returncode),
                )
            )
        if not report_path.exists():
            findings.append(
                QAFinding(
                    code="missing_report",
                    severity="blocking",
                    message="SWMM did not create a report file.",
                )
            )
        if not output_path.exists():
            findings.append(
                QAFinding(
                    code="missing_output",
                    severity="blocking",
                    message="SWMM did not create the binary output file.",
                )
            )
        status = (
            "rejected"
            if any(item.severity == "blocking" for item in findings)
            else "warning"
            if any(item.severity == "warning" for item in findings)
            else "accepted"
        )
        return SolverOutcome(
            return_code=process.returncode,
            input_sha256=sha256_bytes(model_bytes),
            solver_sha256=self.solver_sha256,
            report_sha256=sha256_file(report_path) if report_path.exists() else None,
            output_sha256=sha256_file(output_path) if output_path.exists() else None,
            stdout=stdout.decode("utf-8", errors="replace")[-16_384:],
            stderr=stderr.decode("utf-8", errors="replace")[-16_384:],
            qa_status=status,
            findings=findings,
        )

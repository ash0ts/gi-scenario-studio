#!/usr/bin/env python3
"""Verify and revise the explicitly identified EcoCorridor W&B article.

``verify`` is read-only and writes a canonical plan outside the repository.
``publish`` rereads every local and remote source, creates or reuses one
deterministic article-media run and immutable artifact, then updates the same
Report ID. Its prior body is archived; other Reports are never saved.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from append_ecocorridor_usefulness_narrative import require_publishable_source
except ModuleNotFoundError:
    from scripts.append_ecocorridor_usefulness_narrative import require_publishable_source

try:
    from publish_ecocorridor_human_readable_report import _source_reader
except ModuleNotFoundError:
    from scripts.publish_ecocorridor_human_readable_report import _source_reader

from ecocorridor_core.human_readable_report import HumanReadableSourceSnapshot
from ecocorridor_core.visual_story_report import (
    PREVIOUS_READER_REPORT_URL,
    SOURCE_PROJECTION_RELATIVE_PATH,
    TARGET_REPORT_URL,
    ArticleRevisionSnapshot,
    ProtectedReportSnapshot,
    VerifiedVisualStoryReportPublisher,
    VisualStoryMediaReceipt,
    VisualStoryReportPlan,
    VisualStoryReportReceipt,
    canonical_plan_bytes,
    expected_artifact_metadata,
    expected_run_config,
    load_visual_story_assets,
    publication_artifact_manifest_sha256,
    revision_artifact_payloads,
    safe_https,
    validate_artifact_download,
    validate_plan_bytes,
)

EXPECTED_ENTITY = "wandb"
EXPECTED_PROJECT = "ecocorridor-scenario-studio"
MAX_PLAN_BYTES = 4 * 1024 * 1024
RUN_READBACK_TIMEOUT_SECONDS = 30.0
RUN_READBACK_INTERVAL_SECONDS = 1.0


def _outside_repository(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return False


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _load_plan(path: Path) -> VisualStoryReportPlan:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PLAN_BYTES:
        raise ValueError("visual-story Report plan is missing, symlinked, or oversized")
    return validate_plan_bytes(path.read_bytes())


def _protected_report_snapshots(
    *,
    reports_module: Any,
    urls: Sequence[str],
    web_base_url: str,
) -> tuple[ProtectedReportSnapshot, ...]:
    unique = list(dict.fromkeys(urls))
    if not unique:
        raise ValueError("visual-story publication requires protected source Reports")
    return tuple(
        ProtectedReportSnapshot.from_report(
            reports_module.Report.from_url(url),
            web_base_url=web_base_url,
            label="protected W&B Report URL",
        )
        for url in unique
    )


def read_plan(
    *,
    root: Path,
    entity: str,
    project: str,
    api_base_url: str,
    web_base_url: str,
    source_report_url: str,
    protect_report_urls: Sequence[str],
    wandb_module: Any,
    reports_module: Any,
    target_snapshot: ArticleRevisionSnapshot | None = None,
) -> VisualStoryReportPlan:
    source_commit = require_publishable_source(root)
    receipt, ridgeway = _source_reader(
        root=root,
        source_commit=source_commit,
        entity=entity,
        project=project,
        api_base_url=api_base_url,
        web_base_url=web_base_url,
        source_report_url=source_report_url,
        wandb_module=wandb_module,
        reports_module=reports_module,
    )
    source = HumanReadableSourceSnapshot.from_verified_receipt(receipt, ridgeway=ridgeway)
    protected = _protected_report_snapshots(
        reports_module=reports_module,
        urls=[source_report_url, PREVIOUS_READER_REPORT_URL, *protect_report_urls],
        web_base_url=web_base_url,
    )
    assets = load_visual_story_assets(root)
    target = target_snapshot or ArticleRevisionSnapshot.from_report(
        reports_module.Report.from_url(TARGET_REPORT_URL),
        web_base_url=web_base_url,
        label="article revision target",
    )
    _require_exact_pushed_assets(
        root=root,
        source_commit=source_commit,
        repository_paths=[
            *(item.repository_path for item in assets.artifact_files),
            SOURCE_PROJECTION_RELATIVE_PATH,
        ],
    )
    return VisualStoryReportPlan.create(
        source=source,
        protected_reports=protected,
        target_report=target,
        assets=assets,
    )


def _require_exact_pushed_assets(
    *,
    root: Path,
    source_commit: str,
    repository_paths: Sequence[str],
) -> None:
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise PermissionError("visual-story publication requires a clean Eco worktree")
    for repository_path in sorted(set(repository_paths)):
        local = root / repository_path
        if local.is_symlink() or not local.is_file():
            raise PermissionError("a visual-story publication file is missing or symlinked")
        committed = subprocess.run(
            ["git", "show", f"{source_commit}:{repository_path}"],
            cwd=root,
            check=False,
            capture_output=True,
        )
        if committed.returncode != 0 or committed.stdout != local.read_bytes():
            raise PermissionError(
                f"visual-story publication bytes differ from pushed HEAD: {repository_path}"
            )


def verify_committed_figure_bytes(
    plan: VisualStoryReportPlan,
    *,
    root: Path,
) -> None:
    """Recheck that every native W&B media source is the exact pushed PNG."""

    _require_exact_pushed_assets(
        root=root,
        source_commit=plan.source.source_commit,
        repository_paths=[item.png_path for item in plan.assets.figures],
    )
    files = {item.repository_path: item for item in plan.assets.artifact_files}
    for figure in plan.assets.figures:
        expected = files[figure.png_path]
        source = root / figure.png_path
        payload = source.read_bytes()
        if len(payload) != expected.bytes or _sha256_bytes(payload) != expected.sha256:
            raise RuntimeError("commit-pinned article figure bytes differ")


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _validate_artifact_url_target(
    value: object,
    *,
    web_base_url: str,
    entity: str,
    project: str,
    artifact_name: str,
    version: str,
) -> str:
    url = safe_https(value, label="article artifact URL")
    parsed = urlsplit(url)
    expected_origin = urlsplit(web_base_url)
    if (parsed.scheme, parsed.netloc) != (expected_origin.scheme, expected_origin.netloc):
        raise PermissionError("article artifact belongs to another W&B origin")
    parts = [part for part in parsed.path.split("/") if part]
    expected = [
        entity,
        project,
        "artifacts",
        "article-publication",
        artifact_name,
        version,
    ]
    if parts != expected:
        raise PermissionError("article artifact URL names another artifact version")
    return url


class WandbVisualStoryMediaPublisher:
    """Publish and byte-verify deterministic W&B article media lineage."""

    def __init__(
        self,
        *,
        root: Path,
        entity: str,
        project: str,
        api_base_url: str,
        web_base_url: str,
        authorize_synthetic_public_upload: bool,
        wandb_module: Any,
        api: Any,
    ) -> None:
        if not authorize_synthetic_public_upload:
            raise PermissionError("article media upload requires explicit synthetic-public consent")
        if entity != EXPECTED_ENTITY or project != EXPECTED_PROJECT:
            raise PermissionError("visual-story media is bound to the EcoCorridor project")
        self.root = root.resolve()
        self.entity = entity
        self.project = project
        self.api_base_url = safe_https(api_base_url, label="W&B API base URL").rstrip("/")
        self.web_base_url = safe_https(web_base_url, label="W&B web base URL").rstrip("/")
        self.wandb = wandb_module
        self.api = api

    def _expected_run_url(self, plan: VisualStoryReportPlan) -> str:
        return f"{self.web_base_url}/{self.entity}/{self.project}/runs/{plan.run_id}"

    def _find_run(self, plan: VisualStoryReportPlan) -> Any | None:
        candidates = list(
            self.api.runs(
                path=f"{self.entity}/{self.project}",
                filters={"name": plan.run_id},
                per_page=50,
            )
        )
        exact = [
            candidate
            for candidate in candidates
            if str(getattr(candidate, "id", "")) == plan.run_id
        ]
        if len(exact) > 1:
            raise RuntimeError("multiple W&B runs occupy the deterministic article run ID")
        if candidates and len(exact) != len(candidates):
            raise RuntimeError("W&B article-run discovery returned an inexact ID match")
        return exact[0] if exact else None

    def _verify_artifact_bytes(self, plan: VisualStoryReportPlan, artifact: Any) -> None:
        with tempfile.TemporaryDirectory(prefix="ecocorridor-article-artifact-readback-") as tmp:
            downloaded = Path(artifact.download(root=tmp))
            validate_artifact_download(
                plan,
                root=self.root,
                downloaded_root=downloaded,
            )

    def _verify_media_history(self, plan: VisualStoryReportPlan, run: Any) -> None:
        keys = [f"article/figures/{item.figure_id}" for item in plan.assets.figures]
        rows = [row for row in run.scan_history(keys=keys) if any(key in row for key in keys)]
        if len(rows) != 1 or any(key not in rows[0] for key in keys):
            raise RuntimeError("article run does not retain one complete figure-history row")
        by_path = {item.repository_path: item for item in plan.assets.artifact_files}
        with tempfile.TemporaryDirectory(prefix="ecocorridor-article-media-readback-") as tmp:
            for figure, key in zip(plan.assets.figures, keys, strict=True):
                record = rows[0][key]
                if not isinstance(record, dict) or record.get("_type") != "image-file":
                    raise RuntimeError("article figure history record type differs")
                remote_path = str(record.get("path", ""))
                parts = Path(remote_path).parts
                if (
                    not remote_path.startswith("media/images/")
                    or Path(remote_path).is_absolute()
                    or ".." in parts
                ):
                    raise RuntimeError("article figure history path is unsafe")
                expected = by_path[figure.png_path]
                recorded_digest = record.get("sha256")
                if recorded_digest not in (None, expected.sha256):
                    raise RuntimeError("article figure history digest differs")
                recorded_size = record.get("size")
                if recorded_size not in (None, expected.bytes):
                    raise RuntimeError("article figure history size differs")
                handle = run.file(remote_path).download(
                    root=tmp,
                    replace=True,
                )
                downloaded = Path(handle.name)
                handle.close()
                if (
                    downloaded.stat().st_size != expected.bytes
                    or _sha256_bytes(downloaded.read_bytes()) != expected.sha256
                ):
                    raise RuntimeError("article figure media bytes differ")

    def _verify_run(
        self,
        plan: VisualStoryReportPlan,
        run: Any,
        *,
        status: str,
    ) -> VisualStoryMediaReceipt:
        if str(getattr(run, "state", "")) != "finished":
            raise RuntimeError("deterministic article run exists but is not finished")
        if dict(run.config) != expected_run_config(plan):
            raise RuntimeError("deterministic article run config differs")
        summary = dict(run.summary)
        expected_summary = {
            "article/status": "COMPLETE",
            "article/plan_sha256": plan.plan_sha256,
            "article/article_sha256": plan.assets.article_sha256,
            "article/visual_manifest_sha256": plan.assets.visual_manifest_file_sha256,
            "article/artifact_manifest_sha256": publication_artifact_manifest_sha256(plan),
            "article/figure_count": len(plan.assets.figures),
            "article/new_experiment_result": 0,
        }
        mismatches = [key for key, value in expected_summary.items() if summary.get(key) != value]
        if mismatches:
            raise RuntimeError(
                "deterministic article run completion summary differs: "
                + ", ".join(sorted(mismatches))
            )
        artifact_ref = str(summary.get("article/artifact_ref", ""))
        match = re.fullmatch(
            rf"{self.entity}/{self.project}/{re.escape(plan.artifact_name)}:(v[0-9]+)",
            artifact_ref,
        )
        if match is None:
            raise RuntimeError("article run lacks an immutable artifact reference")
        artifacts = [
            item
            for item in run.logged_artifacts()
            if str(getattr(item, "qualified_name", "")) == artifact_ref
        ]
        if len(artifacts) != 1:
            raise RuntimeError("article run does not log exactly one planned artifact")
        artifact = artifacts[0]
        if str(getattr(artifact, "type", "")) != "article-publication":
            raise RuntimeError("article artifact type differs")
        if dict(getattr(artifact, "metadata", {})) != expected_artifact_metadata(plan):
            raise RuntimeError("article artifact metadata differs")
        artifact_url = _validate_artifact_url_target(
            getattr(artifact, "url", ""),
            web_base_url=self.web_base_url,
            entity=self.entity,
            project=self.project,
            artifact_name=plan.artifact_name,
            version=match.group(1),
        )
        run_url = safe_https(getattr(run, "url", ""), label="article run URL")
        if run_url.rstrip("/") != self._expected_run_url(plan).rstrip("/"):
            raise RuntimeError("article run URL differs")
        if summary.get("article/artifact_url") != artifact_url:
            raise RuntimeError("article artifact URL differs from completion summary")
        self._verify_media_history(plan, run)
        self._verify_artifact_bytes(plan, artifact)
        return VisualStoryMediaReceipt(
            status=status,
            run_id=plan.run_id,
            run_url=run_url,
            artifact_ref=artifact_ref,
            artifact_url=artifact_url,
            artifact_manifest_sha256=publication_artifact_manifest_sha256(plan),
        )

    def _readback(
        self,
        plan: VisualStoryReportPlan,
        *,
        status: str,
    ) -> VisualStoryMediaReceipt:
        deadline = time.monotonic() + RUN_READBACK_TIMEOUT_SECONDS
        while True:
            try:
                run = self._find_run(plan)
                if run is not None:
                    return self._verify_run(plan, run, status=status)
            except PermissionError:
                raise
            except Exception:  # noqa: BLE001 - bounded retry hides remote response content
                run = None
            if time.monotonic() >= deadline:
                raise RuntimeError("article media run failed exact readback")
            time.sleep(RUN_READBACK_INTERVAL_SECONDS)

    def publish(self, plan: VisualStoryReportPlan) -> VisualStoryMediaReceipt:
        existing = self._find_run(plan)
        if existing is not None:
            return self._verify_run(plan, existing, status="REUSED")

        with tempfile.TemporaryDirectory(prefix="ecocorridor-article-wandb-") as temporary:
            settings = self.wandb.Settings(
                base_url=self.api_base_url,
                console="off",
                disable_code=True,
                disable_git=True,
                x_disable_machine_info=True,
                x_disable_meta=True,
                x_save_requirements=False,
                silent=True,
            )
            run = self.wandb.init(
                entity=self.entity,
                project=self.project,
                id=plan.run_id,
                name="EcoCorridor visual article · direct evidence and bounded agent",
                job_type="article_publication",
                tags=["article", "synthetic_ridgeway", "direct_evidence", "negative_result"],
                config=expected_run_config(plan),
                resume="never",
                save_code=False,
                dir=temporary,
                settings=settings,
            )
            try:
                if safe_https(getattr(run, "url", ""), label="article run URL").rstrip(
                    "/"
                ) != self._expected_run_url(plan).rstrip("/"):
                    raise RuntimeError("W&B initialized an unexpected article run")
                figures = {
                    f"article/figures/{figure.figure_id}": self.wandb.Image(
                        str(self.root / figure.png_path),
                        caption=figure.caption,
                    )
                    for figure in plan.assets.figures
                }
                run.log(figures)
                run.log(
                    {
                        "article/claim_ledger": self.wandb.Table(
                            columns=[
                                "figure_id",
                                "question",
                                "takeaway",
                                "forbidden_implication",
                                "png_sha256",
                            ],
                            data=[
                                [
                                    figure.figure_id,
                                    figure.question,
                                    figure.takeaway,
                                    figure.forbidden_implication,
                                    figure.png_sha256,
                                ]
                                for figure in plan.assets.figures
                            ],
                        )
                    }
                )
                artifact = self.wandb.Artifact(
                    plan.artifact_name,
                    type="article-publication",
                    metadata=expected_artifact_metadata(plan),
                )
                for item in plan.assets.artifact_files:
                    artifact.add_file(
                        str(self.root / item.repository_path),
                        name=item.logical_name,
                    )
                for name, payload in revision_artifact_payloads(plan).items():
                    previous = Path(temporary) / name
                    _atomic_write(previous, payload)
                    artifact.add_file(str(previous), name=name)
                logged = run.log_artifact(artifact).wait()
                artifact_ref = str(getattr(logged, "qualified_name", "") or "")
                match = re.fullmatch(
                    rf"{self.entity}/{self.project}/{re.escape(plan.artifact_name)}:(v[0-9]+)",
                    artifact_ref,
                )
                if match is None:
                    raise RuntimeError("W&B did not return an immutable article artifact")
                artifact_url = _validate_artifact_url_target(
                    getattr(logged, "url", ""),
                    web_base_url=self.web_base_url,
                    entity=self.entity,
                    project=self.project,
                    artifact_name=plan.artifact_name,
                    version=match.group(1),
                )
                run.summary["article/status"] = "COMPLETE"
                run.summary["article/plan_sha256"] = plan.plan_sha256
                run.summary["article/article_sha256"] = plan.assets.article_sha256
                run.summary["article/visual_manifest_sha256"] = (
                    plan.assets.visual_manifest_file_sha256
                )
                run.summary["article/artifact_manifest_sha256"] = (
                    publication_artifact_manifest_sha256(plan)
                )
                run.summary["article/figure_count"] = len(plan.assets.figures)
                run.summary["article/new_experiment_result"] = 0
                run.summary["article/artifact_ref"] = artifact_ref
                run.summary["article/artifact_url"] = artifact_url
                run.finish(exit_code=0)
            except Exception:
                run.finish(exit_code=1)
                raise
        return self._readback(plan, status="CREATED")


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--entity", default=EXPECTED_ENTITY)
    parser.add_argument("--project", default=EXPECTED_PROJECT)
    parser.add_argument("--wandb-api-base-url", default="https://api.wandb.ai")
    parser.add_argument("--wandb-web-base-url", default="https://wandb.ai")
    parser.add_argument("--source-report-url", required=True)
    parser.add_argument("--protect-report-url", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify", help="freeze exact sources without W&B writes")
    _common_arguments(verify)
    verify.add_argument("--authorize-source-readback", action="store_true")
    publish = commands.add_parser("publish", help="revise the exact Report and retain prior text")
    _common_arguments(publish)
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument("--authorize-source-readback", action="store_true")
    publish.add_argument("--authorize-synthetic-public-upload", action="store_true")
    publish.add_argument("--authorize-report-revision", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    root = arguments.repository_root.resolve()
    if arguments.entity != EXPECTED_ENTITY or arguments.project != EXPECTED_PROJECT:
        raise SystemExit("visual-story publication is bound to wandb/ecocorridor-scenario-studio")
    if not arguments.authorize_source_readback:
        raise SystemExit("remote source verification requires --authorize-source-readback")
    if not os.environ.get("WANDB_API_KEY"):
        raise SystemExit("remote W&B verification requires WANDB_API_KEY in process memory")
    if not _outside_repository(arguments.output, root):
        raise SystemExit("visual-story plan and receipt must be written outside the repository")

    import wandb_workspaces.reports.v2 as reports

    import wandb

    frozen: VisualStoryReportPlan | None = None

    def current_plan() -> VisualStoryReportPlan:
        return read_plan(
            root=root,
            entity=arguments.entity,
            project=arguments.project,
            api_base_url=arguments.wandb_api_base_url,
            web_base_url=arguments.wandb_web_base_url,
            source_report_url=arguments.source_report_url,
            protect_report_urls=arguments.protect_report_url,
            wandb_module=wandb,
            reports_module=reports,
            target_snapshot=frozen.target_report if frozen is not None else None,
        )

    if arguments.command == "verify":
        plan = current_plan()
        _atomic_write(arguments.output, canonical_plan_bytes(plan))
        if _load_plan(arguments.output) != plan:
            raise RuntimeError("visual-story plan failed exact local readback")
        print(
            json.dumps(
                {
                    "status": "VERIFIED_READ_ONLY",
                    "plan_sha256": plan.plan_sha256,
                    "article_sha256": plan.assets.article_sha256,
                    "figure_count": len(plan.assets.figures),
                    "protected_report_count": len(plan.protected_reports),
                    "new_wandb_run_created": False,
                    "new_artifact_created": False,
                    "new_report_created": False,
                },
                sort_keys=True,
            )
        )
        return 0

    if not arguments.authorize_synthetic_public_upload:
        raise SystemExit("article upload requires --authorize-synthetic-public-upload")
    if not arguments.authorize_report_revision:
        raise SystemExit("Report revision requires --authorize-report-revision")
    frozen = _load_plan(arguments.plan)
    if current_plan() != frozen:
        raise SystemExit("frozen visual-story plan differs from current verified sources")
    api = wandb.Api(overrides={"base_url": arguments.wandb_api_base_url})
    media = WandbVisualStoryMediaPublisher(
        root=root,
        entity=arguments.entity,
        project=arguments.project,
        api_base_url=arguments.wandb_api_base_url,
        web_base_url=arguments.wandb_web_base_url,
        authorize_synthetic_public_upload=True,
        wandb_module=wandb,
        api=api,
    )
    publisher = VerifiedVisualStoryReportPublisher(
        api=api,
        reports_module=reports,
        web_base_url=arguments.wandb_web_base_url,
        authorize_report_revision=True,
        source_reader=current_plan,
        media_publisher=media.publish,
        figure_verifier=lambda plan: verify_committed_figure_bytes(plan, root=root),
    )
    receipt: VisualStoryReportReceipt = publisher.publish(frozen)
    payload = (
        json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")
        + b"\n"
    )
    _atomic_write(arguments.output, payload)
    if VisualStoryReportReceipt.model_validate_json(arguments.output.read_bytes()) != receipt:
        raise RuntimeError("visual-story receipt failed exact local readback")
    print(
        json.dumps(
            {
                "status": receipt.status,
                "media_status": receipt.media_status,
                "report_url": receipt.report_url,
                "run_url": receipt.run_url,
                "artifact_ref": receipt.artifact_ref,
                "protected_reports_preserved": receipt.protected_reports_preserved,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

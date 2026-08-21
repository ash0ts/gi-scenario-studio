from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Self

import pytest
from ecocorridor_core.human_readable_report import HumanReadableSourceSnapshot
from ecocorridor_core.usefulness_narrative import (
    HISTORICAL_ARTIFACT_MANIFEST_SHA256,
    HISTORICAL_DOWNLOADED_FILE_MANIFEST_SHA256,
    HISTORICAL_RUN_URL,
    RidgewayNarrativeEvidence,
    UsefulnessNarrativeReceipt,
    _blocks_sha256,
)
from ecocorridor_core.visual_story_report import (
    ARTICLE_RELATIVE_PATH,
    MANIFEST_RELATIVE_PATH,
    PREVIOUS_READER_REPORT_URL,
    REPORT_TITLE,
    SOURCE_PROJECTION_RELATIVE_PATH,
    ProtectedReportSnapshot,
    VerifiedVisualStoryReportPublisher,
    VisualStoryMediaReceipt,
    VisualStoryReportPlan,
    canonical_plan_bytes,
    expected_artifact_metadata,
    expected_run_config,
    load_visual_story_assets,
    parse_report_marker,
    raw_figure_url,
    render_report_blocks,
    report_description,
    validate_artifact_download,
    validate_plan_bytes,
)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
visual_story_cli = importlib.import_module("scripts.publish_ecocorridor_visual_story")
WandbVisualStoryMediaPublisher = visual_story_cli.WandbVisualStoryMediaPublisher
SOURCE_COMMIT = "a" * 40
SOURCE_REPORT_URL = (
    "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
    "historical--VmlldzoxNzY1OTU0MQ=="
)
OLD_ARTICLE_URL = PREVIOUS_READER_REPORT_URL


class _Block:
    def __init__(self, text: str = "", **values: Any) -> None:
        self.text = text
        self._spec = {"text": text, **values}


class H1(_Block):
    pass


class H2(_Block):
    pass


class MarkdownBlock(_Block):
    pass


class Image(_Block):
    def __init__(self, *, url: str, caption: str) -> None:
        super().__init__(url=url, caption=caption)
        self.url = url
        self.caption = caption


class _FakeApi:
    def __init__(self) -> None:
        self.items: list[_FakeReport] = []
        self.run_items: list[Any] = []
        self.run_error: Exception | None = None

    def reports(self, *, path: str, per_page: int) -> list[_FakeReport]:
        assert path == "wandb/ecocorridor-scenario-studio"
        assert per_page == 100
        return list(self.items)

    def runs(
        self,
        *,
        path: str,
        filters: dict[str, str],
        per_page: int,
    ) -> list[Any]:
        assert path == "wandb/ecocorridor-scenario-studio"
        assert per_page == 50
        if self.run_error is not None:
            raise self.run_error
        run_id = filters["name"]
        return [item for item in self.run_items if item.id == run_id]


class _FakeReport:
    api: ClassVar[_FakeApi]
    registry: ClassVar[dict[str, _FakeReport]] = {}
    serial: ClassVar[int] = 0

    def __init__(
        self,
        *,
        entity: str,
        project: str,
        title: str,
        description: str,
        blocks: list[Any],
        width: str,
    ) -> None:
        self.entity = entity
        self.project = project
        self.title = title
        self.display_name = title
        self.description = description
        self.blocks = list(blocks)
        self.width = width
        self.id = ""
        self.url = ""
        self.saved = 0

    @classmethod
    def retained(
        cls,
        *,
        title: str,
        blocks: list[Any],
        report_id: str,
        url: str,
        description: str = "retained",
    ) -> _FakeReport:
        report = cls(
            entity="wandb",
            project="ecocorridor-scenario-studio",
            title=title,
            description=description,
            blocks=blocks,
            width="readable",
        )
        report.id = report_id
        report.url = url
        cls.registry[url] = report
        cls.api.items.append(report)
        return report

    def save(self) -> None:
        assert not self.id
        type(self).serial += 1
        self.saved += 1
        self.id = f"visual-story-{type(self).serial}"
        self.url = (
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/"
            f"visual-story--{self.id}"
        )
        type(self).registry[self.url] = self
        type(self).api.items.append(self)

    @classmethod
    def from_url(cls, url: str) -> _FakeReport:
        return cls.registry[url]


@pytest.fixture
def fake_cloud() -> tuple[Any, _FakeApi]:
    api = _FakeApi()
    _FakeReport.api = api
    _FakeReport.registry = {}
    _FakeReport.serial = 0
    reports = SimpleNamespace(
        Report=_FakeReport,
        H1=H1,
        H2=H2,
        MarkdownBlock=MarkdownBlock,
        Image=Image,
    )
    return reports, api


class _FakeImageValue:
    def __init__(self, path: str, caption: str) -> None:
        self.path = Path(path)
        self.caption = caption


class _FakePendingArtifact:
    def __init__(self, name: str, artifact_type: str, metadata: dict[str, Any]) -> None:
        self.name = name
        self.type = artifact_type
        self.metadata = dict(metadata)
        self.files: dict[str, Path] = {}

    def add_file(self, path: str, *, name: str) -> None:
        self.files[name] = Path(path)


class _FakeLoggedArtifact:
    def __init__(self, pending: _FakePendingArtifact) -> None:
        self.qualified_name = (
            f"wandb/ecocorridor-scenario-studio/{pending.name}:v0"
        )
        self.type = pending.type
        self.metadata = dict(pending.metadata)
        self.url = (
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/artifacts/"
            f"{pending.type}/{pending.name}/v0"
        )
        self.files = dict(pending.files)

    def wait(self) -> _FakeLoggedArtifact:
        return self

    def download(self, *, root: str) -> str:
        destination = Path(root)
        for logical_name, source in self.files.items():
            target = destination / logical_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return str(destination)


class _FakeRemoteFile:
    def __init__(self, path: str, payload: bytes) -> None:
        self.path = path
        self.payload = payload

    def download(self, *, root: str, replace: bool) -> Any:
        assert replace is True
        target = Path(root) / self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.payload)
        return target.open("rb")


class _FakePublicRun:
    def __init__(
        self,
        *,
        api: _FakeApi,
        run_id: str,
        config: dict[str, Any],
    ) -> None:
        self.api = api
        self.id = run_id
        self.state = "running"
        self.config = dict(config)
        self.summary: dict[str, Any] = {}
        self.url = (
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/runs/" + run_id
        )
        self._history: list[dict[str, Any]] = []
        self._media: dict[str, bytes] = {}
        self._artifacts: list[_FakeLoggedArtifact] = []

    def log(self, values: dict[str, Any]) -> None:
        row: dict[str, Any] = {}
        for key, value in values.items():
            if not isinstance(value, _FakeImageValue):
                continue
            payload = value.path.read_bytes()
            remote_path = f"media/images/{key.replace('/', '_')}.png"
            self._media[remote_path] = payload
            row[key] = {
                "_type": "image-file",
                "path": remote_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
            }
        if row:
            self._history.append(row)

    def log_artifact(self, artifact: _FakePendingArtifact) -> _FakeLoggedArtifact:
        logged = _FakeLoggedArtifact(artifact)
        self._artifacts.append(logged)
        return logged

    def logged_artifacts(self) -> list[_FakeLoggedArtifact]:
        return list(self._artifacts)

    def scan_history(self, *, keys: list[str]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in row.items() if key in keys}
            for row in self._history
        ]

    def file(self, path: str) -> _FakeRemoteFile:
        return _FakeRemoteFile(path, self._media[path])

    def finish(self, *, exit_code: int) -> None:
        self.state = "finished" if exit_code == 0 else "failed"
        if self not in self.api.run_items:
            self.api.run_items.append(self)


class _FakeWandbSdk:
    def __init__(self, api: _FakeApi) -> None:
        self.api = api
        self.init_count = 0
        self.errors = SimpleNamespace(CommError=RuntimeError)

    @staticmethod
    def Settings(**values: Any) -> dict[str, Any]:
        return values

    @staticmethod
    def Image(path: str, *, caption: str) -> _FakeImageValue:
        return _FakeImageValue(path, caption)

    @staticmethod
    def Table(**values: Any) -> dict[str, Any]:
        return values

    @staticmethod
    def Artifact(
        name: str,
        *,
        type: str,
        metadata: dict[str, Any],
    ) -> _FakePendingArtifact:
        return _FakePendingArtifact(name, type, metadata)

    def init(self, **values: Any) -> _FakePublicRun:
        self.init_count += 1
        return _FakePublicRun(
            api=self.api,
            run_id=values["id"],
            config=values["config"],
        )


def _source(source_blocks: list[Any]) -> HumanReadableSourceSnapshot:
    source_digest = _blocks_sha256(source_blocks)
    receipt = UsefulnessNarrativeReceipt.create(
        source_commit=SOURCE_COMMIT,
        historical_artifact_manifest_sha256=HISTORICAL_ARTIFACT_MANIFEST_SHA256,
        historical_downloaded_file_manifest_sha256=(
            HISTORICAL_DOWNLOADED_FILE_MANIFEST_SHA256
        ),
        historical_provider_trace_receipts_sha256="b" * 64,
        historical_run_url=HISTORICAL_RUN_URL,
        report_url=SOURCE_REPORT_URL,
        section_marker="verified-source-marker",
        section_body_sha256="c" * 64,
        historical_prefix_sha256=source_digest,
        original_block_count=len(source_blocks),
        final_block_count=len(source_blocks),
    )
    return HumanReadableSourceSnapshot.from_verified_receipt(
        receipt,
        ridgeway=RidgewayNarrativeEvidence(
            resistance_change_percent=-1.6031418683724497,
            review_mask_change_percent=47.34703286028441,
        ),
    )


def _plan(source_report: _FakeReport, old_report: _FakeReport) -> VisualStoryReportPlan:
    assets = load_visual_story_assets(ROOT)
    protected = [
        ProtectedReportSnapshot.from_report(
            source_report,
            web_base_url="https://wandb.ai",
            label="source",
        ),
        ProtectedReportSnapshot.from_report(
            old_report,
            web_base_url="https://wandb.ai",
            label="old article",
        ),
    ]
    return VisualStoryReportPlan.create(
        source=_source(source_report.blocks),
        protected_reports=protected,
        assets=assets,
    )


def _media(plan: VisualStoryReportPlan, *, status: str = "CREATED") -> VisualStoryMediaReceipt:
    return VisualStoryMediaReceipt(
        status=status,
        run_id=plan.run_id,
        run_url=(
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/runs/" + plan.run_id
        ),
        artifact_ref=(
            f"wandb/ecocorridor-scenario-studio/{plan.artifact_name}:v0"
        ),
        artifact_url=(
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/artifacts/"
            f"article-publication/{plan.artifact_name}/v0"
        ),
        artifact_manifest_sha256=plan.assets.artifact_manifest_sha256,
    )


def test_visual_story_assets_bind_article_figures_and_direct_values() -> None:
    assets = load_visual_story_assets(ROOT)
    assert [item.figure_id for item in assets.figures] == [
        "ridgeway-decision",
        "road-mask-hypotheses",
        "road-mask-current-maps",
        "evidence-rail-receipt",
        "evidence-handoff",
        "agent-promotion-gate",
    ]
    assert len(assets.artifact_files) == 13
    maps = next(item for item in assets.figures if item.figure_id == "road-mask-current-maps")
    assert maps.values["valid_mask_cell_count"] == 48
    assert maps.values["baseline_mask_current_sum"] == pytest.approx(1.0638156991)
    assert maps.values["candidate_mask_current_sum"] == pytest.approx(1.5675008678)
    assert maps.values["change_percent"] == pytest.approx(47.34703286028441)
    gate = next(item for item in assets.figures if item.figure_id == "agent-promotion-gate")
    assert gate.values["saved_input_tokens"] == 256
    assert gate.values["context_reduction_percent"] == pytest.approx(2.3708094091)
    assert gate.values["promotion_gate_percent"] == 25.0


def test_article_is_human_readable_and_keeps_claim_boundaries() -> None:
    article = load_visual_story_assets(ROOT).article_markdown
    assert "Go look at the road." in article
    assert "It is not a deer." in article
    assert "We had tested plumbing and given it an agent name." in article
    assert "none failed the connectivity checks we had defined" in article
    assert "12 rules passed" not in article
    assert "42 agent conversations" not in article
    assert "the agent proved useful" not in article.lower()
    assert "collision reduction" in article  # Explicitly named only inside the boundary list.
    assert "does not show that AI improved ecological review" in article


def test_visual_asset_drift_fails_before_publication(tmp_path: Path) -> None:
    for relative in [
        ARTICLE_RELATIVE_PATH,
        MANIFEST_RELATIVE_PATH,
        SOURCE_PROJECTION_RELATIVE_PATH,
    ]:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    manifest = json.loads((tmp_path / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8"))
    for figure in manifest["figures"]:
        for key in ("png_path", "svg_path"):
            if figure.get(key):
                target = tmp_path / figure[key]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / figure[key], target)
    (tmp_path / manifest["figures"][0]["png_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="PNG digest differs"):
        load_visual_story_assets(tmp_path)


def test_plan_bytes_and_run_identity_are_deterministic(fake_cloud: tuple[Any, _FakeApi]) -> None:
    _reports, _api = fake_cloud
    source = _FakeReport.retained(
        title="technical",
        blocks=[MarkdownBlock("historical")],
        report_id="historical",
        url=SOURCE_REPORT_URL,
    )
    old = _FakeReport.retained(
        title="A Green Model Result Is Not a Green Light",
        blocks=[MarkdownBlock("old article")],
        report_id="old-reader",
        url=OLD_ARTICLE_URL,
    )
    plan = _plan(source, old)
    assert validate_plan_bytes(canonical_plan_bytes(plan)) == plan
    assert expected_run_config(plan)["new_experiment_result"] is False
    assert expected_run_config(plan)["cc0_synthetic_only"] is True
    with pytest.raises(ValueError, match="noncanonical"):
        validate_plan_bytes(canonical_plan_bytes(plan).rstrip())


def test_new_report_preserves_both_sources_and_exact_retry_reuses(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    source = _FakeReport.retained(
        title="technical",
        blocks=[MarkdownBlock("historical")],
        report_id="historical",
        url=SOURCE_REPORT_URL,
    )
    old = _FakeReport.retained(
        title="A Green Model Result Is Not a Green Light",
        blocks=[MarkdownBlock("old article")],
        report_id="old-reader",
        url=OLD_ARTICLE_URL,
    )
    plan = _plan(source, old)
    media = _media(plan)
    publisher = VerifiedVisualStoryReportPublisher(
        api=api,
        reports_module=reports,
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        source_reader=lambda: plan,
        media_publisher=lambda _plan: media,
        figure_verifier=lambda _plan: None,
    )

    created = publisher.publish(plan)
    assert created.status == "CREATED"
    assert created.report_url not in {SOURCE_REPORT_URL, OLD_ARTICLE_URL}
    assert source.saved == 0
    assert old.saved == 0
    destination = _FakeReport.from_url(created.report_url)
    assert parse_report_marker(destination.description) == json.loads(
        report_description(plan, media).split("ecocorridor-visual-story-report:", 1)[1].split(
            " -->", 1
        )[0]
    )
    image_blocks = [item for item in destination.blocks if isinstance(item, Image)]
    assert len(image_blocks) == 6
    assert all(f"/{SOURCE_COMMIT}/docs/" in item.url for item in image_blocks)

    reused_publisher = VerifiedVisualStoryReportPublisher(
        api=api,
        reports_module=reports,
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        source_reader=lambda: plan,
        media_publisher=lambda _plan: _media(plan, status="REUSED"),
        figure_verifier=lambda _plan: None,
    )
    reused = reused_publisher.publish(plan)
    assert reused.status == "REUSED"
    assert destination.saved == 1
    assert source.saved == old.saved == 0


def test_title_collision_and_source_drift_fail_closed(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, api = fake_cloud
    source = _FakeReport.retained(
        title="technical",
        blocks=[MarkdownBlock("historical")],
        report_id="historical",
        url=SOURCE_REPORT_URL,
    )
    old = _FakeReport.retained(
        title="A Green Model Result Is Not a Green Light",
        blocks=[MarkdownBlock("old article")],
        report_id="old-reader",
        url=OLD_ARTICLE_URL,
    )
    plan = _plan(source, old)
    occupied = _FakeReport.retained(
        title=REPORT_TITLE,
        blocks=[MarkdownBlock("unrelated")],
        report_id="occupied",
        url=(
            "https://wandb.ai/wandb/ecocorridor-scenario-studio/reports/occupied"
        ),
    )
    media_calls: list[str] = []

    def _publish_media(_plan: VisualStoryReportPlan) -> VisualStoryMediaReceipt:
        media_calls.append("called")
        return _media(plan)

    publisher = VerifiedVisualStoryReportPublisher(
        api=api,
        reports_module=reports,
        web_base_url="https://wandb.ai",
        authorize_new_report=True,
        source_reader=lambda: plan,
        media_publisher=_publish_media,
        figure_verifier=lambda _plan: None,
    )
    with pytest.raises(PermissionError, match="title is occupied"):
        publisher.publish(plan)
    assert occupied.saved == source.saved == old.saved == 0
    assert media_calls == []

    api.items.remove(occupied)
    _FakeReport.registry.pop(occupied.url)
    old.blocks.append(MarkdownBlock("drift"))
    with pytest.raises(RuntimeError, match="protected W&B Report changed"):
        publisher.publish(plan)


def test_artifact_download_requires_exact_source_bytes(tmp_path: Path) -> None:
    assets = load_visual_story_assets(ROOT)
    source = HumanReadableSourceSnapshot.model_construct(
        source_commit=SOURCE_COMMIT,
        snapshot_sha256="d" * 64,
    )
    plan = VisualStoryReportPlan.model_construct(assets=assets, source=source)
    downloaded = tmp_path / "artifact"
    for item in assets.artifact_files:
        target = downloaded / item.logical_name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / item.repository_path, target)
    validate_artifact_download(plan, root=ROOT, downloaded_root=downloaded)
    (downloaded / assets.artifact_files[0].logical_name).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="digest differs"):
        validate_artifact_download(plan, root=ROOT, downloaded_root=downloaded)


def test_report_blocks_keep_caption_and_accessible_description(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    reports, _api = fake_cloud
    source = _FakeReport.retained(
        title="technical",
        blocks=[MarkdownBlock("historical")],
        report_id="historical",
        url=SOURCE_REPORT_URL,
    )
    old = _FakeReport.retained(
        title="A Green Model Result Is Not a Green Light",
        blocks=[MarkdownBlock("old")],
        report_id="old",
        url=OLD_ARTICLE_URL,
    )
    plan = _plan(source, old)
    blocks = render_report_blocks(reports, plan, _media(plan))
    assert isinstance(blocks[0], H1)
    assert blocks[0].text == REPORT_TITLE
    assert len([item for item in blocks if isinstance(item, Image)]) == 6
    prose = "\n".join(item.text for item in blocks if isinstance(item, MarkdownBlock))
    for figure in plan.assets.figures:
        assert figure.caption in prose
        assert figure.alt_text in prose


def _media_plan(fake_cloud: tuple[Any, _FakeApi]) -> tuple[VisualStoryReportPlan, _FakeApi]:
    _reports, api = fake_cloud
    source = _FakeReport.retained(
        title="technical",
        blocks=[MarkdownBlock("historical")],
        report_id="historical",
        url=SOURCE_REPORT_URL,
    )
    old = _FakeReport.retained(
        title="A Green Model Result Is Not a Green Light",
        blocks=[MarkdownBlock("old")],
        report_id="old",
        url=OLD_ARTICLE_URL,
    )
    return _plan(source, old), api


def _media_publisher(
    *,
    api: _FakeApi,
    sdk: _FakeWandbSdk,
) -> WandbVisualStoryMediaPublisher:
    return WandbVisualStoryMediaPublisher(
        root=ROOT,
        entity="wandb",
        project="ecocorridor-scenario-studio",
        api_base_url="https://api.wandb.ai",
        web_base_url="https://wandb.ai",
        authorize_synthetic_public_upload=True,
        wandb_module=sdk,
        api=api,
    )


def test_media_publisher_creates_once_then_exactly_reuses(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    plan, api = _media_plan(fake_cloud)
    sdk = _FakeWandbSdk(api)
    publisher = _media_publisher(api=api, sdk=sdk)

    created = publisher.publish(plan)
    assert created.status == "CREATED"
    assert sdk.init_count == 1
    assert len(api.run_items) == 1

    reused = publisher.publish(plan)
    assert reused.status == "REUSED"
    assert reused.artifact_ref == created.artifact_ref
    assert sdk.init_count == 1


def test_media_publisher_fails_closed_on_discovery_error(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    plan, api = _media_plan(fake_cloud)
    sdk = _FakeWandbSdk(api)
    api.run_error = RuntimeError("authentication failed")
    with pytest.raises(RuntimeError, match="authentication failed"):
        _media_publisher(api=api, sdk=sdk).publish(plan)
    assert sdk.init_count == 0


def test_media_publisher_blocks_partial_or_mismatched_run(
    fake_cloud: tuple[Any, _FakeApi],
) -> None:
    plan, api = _media_plan(fake_cloud)
    sdk = _FakeWandbSdk(api)
    partial = _FakePublicRun(api=api, run_id=plan.run_id, config=expected_run_config(plan))
    api.run_items.append(partial)
    with pytest.raises(RuntimeError, match="not finished"):
        _media_publisher(api=api, sdk=sdk).publish(plan)
    assert sdk.init_count == 0

    partial.state = "finished"
    partial.config["article_sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="config differs"):
        _media_publisher(api=api, sdk=sdk).publish(plan)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_media", "figure-history row"),
        ("extra_artifact_file", "file set differs"),
        ("wrong_artifact_url", "another artifact version"),
        ("wrong_artifact_metadata", "metadata differs"),
    ],
)
def test_media_publisher_rejects_incomplete_cloud_readback(
    fake_cloud: tuple[Any, _FakeApi],
    mutation: str,
    message: str,
) -> None:
    plan, api = _media_plan(fake_cloud)
    sdk = _FakeWandbSdk(api)
    publisher = _media_publisher(api=api, sdk=sdk)
    publisher.publish(plan)
    run = api.run_items[0]
    artifact = run._artifacts[0]

    if mutation == "missing_media":
        run._history.clear()
    elif mutation == "extra_artifact_file":
        artifact.files["unexpected.txt"] = ROOT / "README.md"
    elif mutation == "wrong_artifact_url":
        artifact.url = artifact.url.replace("/v0", "/v1")
    elif mutation == "wrong_artifact_metadata":
        artifact.metadata = {**expected_artifact_metadata(plan), "extra": True}

    with pytest.raises((RuntimeError, PermissionError), match=message):
        publisher.publish(plan)


def test_commit_pinned_figure_fetch_is_byte_verified(
    fake_cloud: tuple[Any, _FakeApi],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _api = _media_plan(fake_cloud)
    payloads = {
        raw_figure_url(plan, figure): (ROOT / figure.png_path).read_bytes()
        for figure in plan.assets.figures
    }

    class _Response:
        def __init__(self, url: str, payload: bytes) -> None:
            self.status = 200
            self.headers = {"Content-Type": "image/png"}
            self.url = url
            self.payload = payload

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def geturl(self) -> str:
            return self.url

        def read(self, limit: int) -> bytes:
            return self.payload[:limit]

    def _open(request: Any, *, timeout: int) -> _Response:
        assert timeout == 20
        return _Response(request.full_url, payloads[request.full_url])

    monkeypatch.setattr(visual_story_cli, "urlopen", _open)
    visual_story_cli.verify_remote_figure_bytes(plan)

    first_url = next(iter(payloads))
    payloads[first_url] = b"corrupt"
    with pytest.raises(RuntimeError, match="bytes differ"):
        visual_story_cli.verify_remote_figure_bytes(plan)

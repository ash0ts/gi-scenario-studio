from __future__ import annotations

import re

from ecocorridor_core.assistant_runtime import _capability_preflight_subject_id
from ecocorridor_core.assistants import AssistantProviderConfiguration


def test_capability_run_identity_is_stable_and_separates_project_auth_and_provider() -> None:
    configuration = AssistantProviderConfiguration(
        base_url="https://api.openai.com/v1",
        model="gpt-test-1",
        timeout_seconds=60,
    )
    baseline = _capability_preflight_subject_id(
        project_id="project-1",
        cloud_authorization_sha256="a" * 64,
        configuration=configuration,
    )
    repeated = _capability_preflight_subject_id(
        project_id="project-1",
        cloud_authorization_sha256="a" * 64,
        configuration=configuration,
    )
    changed_project = _capability_preflight_subject_id(
        project_id="project-2",
        cloud_authorization_sha256="a" * 64,
        configuration=configuration,
    )
    changed_authorization = _capability_preflight_subject_id(
        project_id="project-1",
        cloud_authorization_sha256="b" * 64,
        configuration=configuration,
    )
    changed_provider = _capability_preflight_subject_id(
        project_id="project-1",
        cloud_authorization_sha256="a" * 64,
        configuration=configuration.model_copy(update={"model": "gpt-test-2"}),
    )

    assert baseline == repeated
    assert len({baseline, changed_project, changed_authorization, changed_provider}) == 4
    assert re.fullmatch(r"native-assistant-provider-preflight-v1-[0-9a-f]{24}", baseline)

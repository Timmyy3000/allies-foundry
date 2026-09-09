from __future__ import annotations

from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils import timezone

from runtime.models import (
    Execution,
    PublicationIntent,
    PublicationIntentState,
    RuntimeProfile,
    RuntimeProfileLifecycleState,
    Workspace,
    WorkspaceProvisioningPhase,
)
from runtime.services.claims import claim_next_execution
from runtime.services.publications import (
    acknowledge_frozen_publication,
    create_publication_intent,
)
from runtime.services.runtime_auth import (
    authenticate_runtime_token,
    issue_runtime_credential,
)


@pytest.fixture
def publication_claim(db):
    workspace = Workspace.objects.create(
        tenant_ref=str(uuid4()),
        fly_app_ref="app",
        volume_ref="volume",
        machine_ref="machine-1",
        machine_generation=1,
        provisioning_phase=WorkspaceProvisioningPhase.IDLE,
        ready_generation=1,
        ready_start_epoch=0,
        ready_boot_id=uuid4(),
        runtime_last_seen_at=timezone.now(),
    )
    profile = RuntimeProfile.objects.create(
        workspace=workspace,
        ally_ref="ally",
        hermes_profile_key="ally",
        lifecycle_state=RuntimeProfileLifecycleState.ACTIVE,
        materialized_generation=1,
        seed_payload={"model": "gpt-5.6-luna"},
    )
    execution = Execution.objects.create(
        workspace=workspace,
        profile=profile,
        idempotency_key="publication-turn",
        source_kind="conversation_message",
        cloud_binding_id=uuid4(),
        cloud_message_id=uuid4(),
        input_payload={"message": "publish this"},
    )
    issued = issue_runtime_credential(workspace.id, "runtime-publication-secret")
    context = authenticate_runtime_token(issued.raw_token)
    claim = claim_next_execution(context, uuid4(), 2)
    assert claim is not None
    return context, claim, profile, execution, issued.raw_token


@pytest.mark.django_db
@override_settings(ALLIES_RUNTIME_FILE_PUBLICATION_ENABLED=True)
def test_intent_precedes_and_fences_the_frozen_manifest(publication_claim):
    context, claim, profile, execution, _token = publication_claim
    first = create_publication_intent(
        context,
        claim.attempt_id,
        claim.lease_token,
        "call-1",
        [{"name": "result.csv", "size": 12}],
    )
    replay = create_publication_intent(
        context,
        claim.attempt_id,
        claim.lease_token,
        "call-1",
        [{"name": "result.csv", "size": 12}],
    )

    assert replay == first
    intent = PublicationIntent.objects.get(pk=first.publication_id)
    assert intent.execution_id == execution.id
    assert intent.profile_id == profile.id
    assert intent.state == PublicationIntentState.PREPARING
    assert intent.manifest_digest is None

    frozen = acknowledge_frozen_publication(
        context,
        profile.id,
        first.publication_id,
        [
            {
                "source_version_id": str(uuid4()),
                "name": "result.csv",
                "size": 12,
                "sha256": "a" * 64,
            }
        ],
    )
    assert frozen.state == PublicationIntentState.FROZEN
    assert frozen.manifest_digest

    with pytest.raises(Exception, match="manifest changed"):
        acknowledge_frozen_publication(
            context,
            profile.id,
            first.publication_id,
            [
                {
                    "source_version_id": str(uuid4()),
                    "name": "result.csv",
                    "size": 12,
                    "sha256": "b" * 64,
                }
            ],
        )


@pytest.mark.django_db
@override_settings(ALLIES_RUNTIME_FILE_PUBLICATION_ENABLED=True)
def test_intent_api_replays_the_same_tool_identity(publication_claim, client):
    _context, claim, profile, _execution, token = publication_claim
    headers = {
        "HTTP_AUTHORIZATION": f"Bearer {token}",
        "HTTP_X_FOUNDRY_LEASE_TOKEN": claim.lease_token,
    }
    path = f"/api/v1/runtime/attempts/{claim.attempt_id}/file-publication-intents"
    body = {"tool_call_id": "call-1", "files": [{"name": "result.csv", "size": 12}]}

    first = client.post(path, body, content_type="application/json", **headers)
    replay = client.post(path, body, content_type="application/json", **headers)

    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    publication_id = first.json()["publication_id"]
    frozen = client.post(
        f"/api/v1/runtime/profiles/{profile.id}/file-publication-intents/{publication_id}/frozen",
        {
            "files": [
                {
                    "source_version_id": str(uuid4()),
                    "name": "result.csv",
                    "size": 12,
                    "sha256": "a" * 64,
                }
            ]
        },
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert frozen.status_code == 200
    assert frozen.json()["state"] == "frozen"

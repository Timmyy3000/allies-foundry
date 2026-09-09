"""Durable, scoped publication intent authority and Cloud transport."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from runtime.exceptions import (
    RuntimeFencedError,
    RuntimeIdempotencyConflictError,
    RuntimeLeaseConflictError,
    RuntimeNotReadyError,
    RuntimeValidationError,
)
from runtime.models import (
    Attempt,
    Lease,
    LeaseState,
    PublicationIntent,
    PublicationIntentState,
    RuntimeProfile,
    Workspace,
)

from .event_delivery import _validated_cloud_url
from .profiles import profile_allows_runtime_write
from .runtime_auth import RuntimeContext
from .validation import digest_lease_token

MAX_PUBLICATION_FILES = 10
MAX_PUBLICATION_FILE_BYTES = 25_000_000
MAX_PUBLICATION_BYTES = 50_000_000
MAX_PUBLICATION_ATTEMPTS = 5
MAX_RECOVERY_BATCH = 20
MAX_CLOUD_RESPONSE_BYTES = 64 * 1024
_SAFE_ERROR = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BACKOFF_SECONDS = (5, 30, 120, 300)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True, slots=True)
class PublicationIntentReceipt:
    publication_id: UUID
    state: str
    manifest_digest: str | None = None
    next_due_at: object | None = None


@dataclass(frozen=True, slots=True)
class PublicationIntentPage:
    items: tuple[PublicationIntentReceipt, ...]
    next_cursor: UUID | None


def create_publication_intent(
    context: RuntimeContext,
    attempt_id: UUID,
    lease_token: str,
    tool_call_id: str,
    files: Sequence[Mapping[str, object]],
) -> PublicationIntentReceipt:
    """Record intent before a runtime copy may become a durable snapshot."""

    _require_publications_enabled()
    call_id = _tool_call_id(tool_call_id)
    prepared = _prepared_files(files)
    request_digest = _digest({"tool_call_id": call_id, "files": prepared})
    tool_call_digest = _digest({"tool_call_id": call_id})
    with transaction.atomic():
        attempt, workspace, profile = _active_attempt(context, attempt_id, lease_token)
        execution = attempt.execution
        existing = (
            PublicationIntent.objects.select_for_update()
            .filter(execution_id=execution.id, tool_call_digest=tool_call_digest)
            .first()
        )
        if existing is not None:
            if existing.request_digest != request_digest:
                raise RuntimeIdempotencyConflictError(
                    "publication tool call changed its descriptor set"
                )
            return _receipt(existing)
        try:
            intent = PublicationIntent.objects.create(
                workspace=workspace,
                profile=profile,
                execution=execution,
                source_attempt=attempt,
                cloud_binding_id=execution.cloud_binding_id,
                cloud_message_id=execution.cloud_message_id,
                tool_call_digest=tool_call_digest,
                request_digest=request_digest,
                next_due_at=timezone.now(),
            )
        except IntegrityError as exc:
            raise RuntimeIdempotencyConflictError(
                "publication tool call conflicts with stored state"
            ) from exc
    return _receipt(intent)


def acknowledge_frozen_publication(
    context: RuntimeContext,
    profile_id: UUID,
    publication_id: UUID,
    files: Sequence[Mapping[str, object]],
) -> PublicationIntentReceipt:
    """Persist one exact frozen manifest after its local journal is committed."""

    _require_publications_enabled()
    frozen = _frozen_files(files)
    manifest_digest = _digest({"files": frozen})
    with transaction.atomic():
        _current_profile(context, profile_id)
        intent = (
            PublicationIntent.objects.select_for_update()
            .filter(
                pk=publication_id,
                profile_id=profile_id,
                workspace_id=context.workspace_id,
            )
            .first()
        )
        if intent is None:
            raise RuntimeLeaseConflictError("publication intent is not in this profile")
        if intent.manifest_digest is not None:
            if intent.manifest_digest != manifest_digest:
                raise RuntimeIdempotencyConflictError("publication manifest changed")
            return _receipt(intent)
        if intent.state != PublicationIntentState.PREPARING:
            raise RuntimeLeaseConflictError("publication intent cannot be frozen")
        intent.manifest_digest = manifest_digest
        intent.state = PublicationIntentState.FROZEN
        intent.next_due_at = timezone.now()
        intent.safe_error_code = ""
        intent.save(
            update_fields=[
                "manifest_digest",
                "state",
                "next_due_at",
                "safe_error_code",
                "updated_at",
            ]
        )
    return _receipt(intent)


def list_publication_intents(
    context: RuntimeContext,
    profile_id: UUID,
    limit: int,
    cursor: UUID | None = None,
) -> PublicationIntentPage:
    """Return one stable, current-profile page for runtime recovery."""

    _require_publications_enabled()
    if isinstance(limit, bool) or not 1 <= limit <= MAX_RECOVERY_BATCH:
        raise RuntimeValidationError("publication intent limit must be from 1 to 20")
    _current_profile(context, profile_id)
    query = PublicationIntent.objects.filter(
        workspace_id=context.workspace_id,
        profile_id=profile_id,
    ).order_by("id")
    if cursor is not None:
        query = query.filter(id__gt=cursor)
    rows = list(query[: limit + 1])
    page = rows[:limit]
    return PublicationIntentPage(
        items=tuple(_receipt(row) for row in page),
        next_cursor=rows[limit].id if len(rows) > limit else None,
    )


def due_publication_intents(
    context: RuntimeContext,
    profile_id: UUID,
    limit: int = MAX_RECOVERY_BATCH,
) -> tuple[PublicationIntentReceipt, ...]:
    """Return due frozen work without opening a new model execution."""

    _require_publications_enabled()
    if isinstance(limit, bool) or not 1 <= limit <= MAX_RECOVERY_BATCH:
        raise RuntimeValidationError("publication recovery limit must be from 1 to 20")
    _current_profile(context, profile_id)
    now = timezone.now()
    rows = PublicationIntent.objects.filter(
        workspace_id=context.workspace_id,
        profile_id=profile_id,
        state__in=[PublicationIntentState.FROZEN, PublicationIntentState.FAILED],
        manifest_digest__isnull=False,
        next_due_at__lte=now,
    ).order_by("next_due_at", "id")[:limit]
    return tuple(_receipt(row) for row in rows)


def mark_publication_registered(
    context: RuntimeContext,
    profile_id: UUID,
    publication_id: UUID,
    revision: int | None,
) -> PublicationIntentReceipt:
    _require_publications_enabled()
    with transaction.atomic():
        _current_profile(context, profile_id)
        intent = _locked_intent(context, profile_id, publication_id)
        if intent.state == PublicationIntentState.REGISTERED:
            return _receipt(intent)
        if intent.manifest_digest is None:
            raise RuntimeLeaseConflictError("publication snapshot is unavailable")
        intent.state = PublicationIntentState.REGISTERED
        intent.safe_error_code = ""
        if revision is not None:
            intent.cloud_revision = revision
        intent.save(
            update_fields=["state", "safe_error_code", "cloud_revision", "updated_at"]
        )
    return _receipt(intent)


def defer_publication(
    context: RuntimeContext,
    profile_id: UUID,
    publication_id: UUID,
    error_code: str,
) -> PublicationIntentReceipt:
    """Keep a failed frozen publication durable for bounded explicit retry."""

    _require_publications_enabled()
    if not _SAFE_ERROR.fullmatch(error_code):
        raise RuntimeValidationError("publication error code is invalid")
    with transaction.atomic():
        _current_profile(context, profile_id)
        intent = _locked_intent(context, profile_id, publication_id)
        if intent.manifest_digest is None:
            raise RuntimeLeaseConflictError("publication snapshot is unavailable")
        if intent.attempts >= MAX_PUBLICATION_ATTEMPTS:
            intent.state = PublicationIntentState.FAILED
            intent.safe_error_code = error_code
            intent.save(update_fields=["state", "safe_error_code", "updated_at"])
            return _receipt(intent)
        intent.attempts += 1
        intent.state = PublicationIntentState.FAILED
        intent.safe_error_code = error_code
        intent.next_due_at = timezone.now() + timedelta(
            seconds=_BACKOFF_SECONDS[
                min(intent.attempts - 1, len(_BACKOFF_SECONDS) - 1)
            ]
        )
        intent.save(
            update_fields=[
                "attempts",
                "state",
                "safe_error_code",
                "next_due_at",
                "updated_at",
            ]
        )
    return _receipt(intent)


def cloud_publication_request(
    method: str,
    path: str,
    *,
    body: Mapping[str, object] | None = None,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    """Call only the fixed internal Cloud publication boundary."""

    _require_publications_enabled()
    base_url = _validated_cloud_url(getattr(settings, "ALLIES_CLOUD_URL", None))
    token = getattr(settings, "ALLIES_CLOUD_EVENT_SERVICE_TOKEN", None)
    if not base_url or not token:
        raise RuntimeNotReadyError("file publication is not configured")
    payload = data
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if body is not None:
        payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = Request(
        f"{base_url}/api/v1/internal/v1{path}",
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        response = build_opener(_NoRedirect).open(request, timeout=120)
    except HTTPError as exc:
        try:
            content = exc.read(MAX_CLOUD_RESPONSE_BYTES + 1)
        finally:
            exc.close()
        return int(exc.code), _cloud_body(content)
    except (TimeoutError, URLError, OSError) as exc:
        raise RuntimeNotReadyError("file publication transport is unavailable") from exc
    try:
        return int(response.status), _cloud_body(
            response.read(MAX_CLOUD_RESPONSE_BYTES + 1)
        )
    finally:
        response.close()


def _active_attempt(
    context: RuntimeContext, attempt_id: UUID, lease_token: str
) -> tuple[Attempt, Workspace, RuntimeProfile]:
    if not isinstance(context, RuntimeContext):
        raise RuntimeValidationError("runtime context is required")
    token_digest = digest_lease_token(lease_token)
    attempt = (
        Attempt.objects.select_for_update()
        .select_related("execution__workspace", "execution__profile")
        .filter(pk=attempt_id, execution__workspace_id=context.workspace_id)
        .first()
    )
    if attempt is None:
        raise RuntimeLeaseConflictError("attempt is not in this workspace")
    execution = attempt.execution
    workspace = execution.workspace
    profile = execution.profile
    lease = Lease.objects.select_for_update().filter(attempt_id=attempt.id).first()
    if (
        workspace.machine_generation != context.machine_generation
        or attempt.machine_generation != context.machine_generation
        or lease is None
        or lease.profile_id != profile.id
        or lease.token_digest != token_digest
        or lease.machine_generation != context.machine_generation
        or lease.state != LeaseState.ACTIVE
        or lease.expires_at <= timezone.now()
    ):
        raise RuntimeLeaseConflictError("lease does not authorize publication")
    if not profile_allows_runtime_write(profile):
        raise RuntimeLeaseConflictError("profile lifecycle is not active")
    if (
        execution.source_kind != "conversation_message"
        or execution.cloud_binding_id is None
        or execution.cloud_message_id is None
    ):
        raise RuntimeLeaseConflictError("execution has no publication authority")
    return attempt, workspace, profile


def _current_profile(context: RuntimeContext, profile_id: UUID) -> RuntimeProfile:
    if not isinstance(context, RuntimeContext):
        raise RuntimeValidationError("runtime context is required")
    workspace = (
        Workspace.objects.select_for_update().filter(pk=context.workspace_id).first()
    )
    profile = (
        RuntimeProfile.objects.select_for_update()
        .filter(pk=profile_id, workspace_id=context.workspace_id)
        .first()
    )
    if workspace is None or profile is None:
        raise RuntimeLeaseConflictError("profile is not in this workspace")
    if workspace.machine_generation != context.machine_generation:
        raise RuntimeFencedError("runtime generation is stale")
    if not profile_allows_runtime_write(profile):
        raise RuntimeLeaseConflictError("profile lifecycle is not active")
    return profile


def _locked_intent(
    context: RuntimeContext, profile_id: UUID, publication_id: UUID
) -> PublicationIntent:
    intent = (
        PublicationIntent.objects.select_for_update()
        .filter(
            pk=publication_id,
            profile_id=profile_id,
            workspace_id=context.workspace_id,
        )
        .first()
    )
    if intent is None:
        raise RuntimeLeaseConflictError("publication intent is not in this profile")
    return intent


def _prepared_files(files: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise RuntimeValidationError("publication files are invalid")
    if not 1 <= len(files) <= MAX_PUBLICATION_FILES:
        raise RuntimeValidationError("publication must contain from 1 to 10 files")
    output: list[dict[str, object]] = []
    total = 0
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {"name", "size"}:
            raise RuntimeValidationError("publication file descriptor is invalid")
        name = item.get("name")
        size = item.get("size")
        if (
            not isinstance(name, str)
            or not 1 <= len(name.encode("utf-8")) <= 255
            or "\x00" in name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= MAX_PUBLICATION_FILE_BYTES
        ):
            raise RuntimeValidationError("publication file descriptor is invalid")
        total += size
        output.append({"name": name, "size": size})
    if total > MAX_PUBLICATION_BYTES:
        raise RuntimeValidationError("publication file set exceeds 50 MB")
    return output


def _frozen_files(files: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    if not isinstance(files, Sequence) or isinstance(files, (str, bytes)):
        raise RuntimeValidationError("frozen publication files are invalid")
    if not 1 <= len(files) <= MAX_PUBLICATION_FILES:
        raise RuntimeValidationError("publication must contain from 1 to 10 files")
    output: list[dict[str, object]] = []
    total = 0
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {
            "source_version_id",
            "name",
            "size",
            "sha256",
        }:
            raise RuntimeValidationError(
                "frozen publication file descriptor is invalid"
            )
        try:
            source_version_id = str(UUID(str(item.get("source_version_id"))))
        except (TypeError, ValueError) as exc:
            raise RuntimeValidationError(
                "publication source version is invalid"
            ) from exc
        name = item.get("name")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or not 1 <= len(name.encode("utf-8")) <= 255
            or "\x00" in name
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 1 <= size <= MAX_PUBLICATION_FILE_BYTES
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise RuntimeValidationError(
                "frozen publication file descriptor is invalid"
            )
        total += size
        output.append(
            {
                "source_version_id": source_version_id,
                "name": name,
                "size": size,
                "sha256": digest,
            }
        )
    if total > MAX_PUBLICATION_BYTES:
        raise RuntimeValidationError("publication file set exceeds 50 MB")
    return output


def _tool_call_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value.encode("utf-8")) <= 255
        or any(character in value for character in "\x00\r\n")
    ):
        raise RuntimeValidationError("publication tool call identity is invalid")
    return value


def _receipt(intent: PublicationIntent) -> PublicationIntentReceipt:
    return PublicationIntentReceipt(
        publication_id=intent.id,
        state=intent.state,
        manifest_digest=intent.manifest_digest,
        next_due_at=intent.next_due_at,
    )


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cloud_body(content: bytes) -> dict[str, object]:
    if len(content) > MAX_CLOUD_RESPONSE_BYTES:
        return {"error_code": "publication_response_too_large"}
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"error_code": "publication_response_invalid"}
    return (
        value
        if isinstance(value, dict)
        else {"error_code": "publication_response_invalid"}
    )


def _require_publications_enabled() -> None:
    if not getattr(settings, "ALLIES_RUNTIME_FILE_PUBLICATION_ENABLED", False):
        raise RuntimeNotReadyError("file publication is disabled")


__all__ = [
    "MAX_PUBLICATION_ATTEMPTS",
    "MAX_RECOVERY_BATCH",
    "PublicationIntentPage",
    "PublicationIntentReceipt",
    "acknowledge_frozen_publication",
    "cloud_publication_request",
    "create_publication_intent",
    "defer_publication",
    "due_publication_intents",
    "list_publication_intents",
    "mark_publication_registered",
]

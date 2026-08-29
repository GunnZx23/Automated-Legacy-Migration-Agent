"""Append-only public conversation state for Architect migration intake.

Conversation turns are advisory. Only the UI service's explicit launch method
may start the immutable controller-owned contract for the selected scenario.
"""

from __future__ import annotations

import re
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectConversationMessage,
    ArchitectConversationRun,
)
from legacy_migration_agent.application.migration_scenarios import migration_launch_contract
from legacy_migration_agent.contracts import Platform, Sha256Digest, StrictModel
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation
from legacy_migration_agent.core.redaction import assert_no_request_secrets

MAX_CONVERSATION_EXCHANGES = 12
MAX_CONVERSATIONS = 64
_CONVERSATION_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_RUN_HANDLE_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_LAUNCH_TOKEN_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXCHANGE_FILE_PATTERN = re.compile(r"^exchange-([0-9]{4})\.json$")
_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[tuple[Path, str], threading.RLock] = {}


class ArchitectConversationStaleLaunch(PolicyViolation):
    """Raised when durable launch reservation sees a newer intake state."""


class ArchitectConversationHeader(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    conversation_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    initial_platform: Platform | None = None
    initial_scenario_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )

    @model_validator(mode="after")
    def require_initial_scenario_binding(self) -> ArchitectConversationHeader:
        if (self.initial_platform is None) != (self.initial_scenario_id is None):
            raise ValueError("initial platform and scenario identity must be selected together")
        return self


class ArchitectConversationExchange(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    exchange: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES)
    selected_platform: Platform | None = None
    scenario_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    launch_contract_digest: Sha256Digest | None = None
    user_message: ArchitectConversationMessage
    architect_run: ArchitectConversationRun

    @model_validator(mode="after")
    def validate_roles(self) -> ArchitectConversationExchange:
        if self.user_message.role != "user":
            raise ValueError("conversation exchange must contain a user message")
        scenario_binding = (
            self.selected_platform is not None,
            self.scenario_id is not None,
            self.launch_contract_digest is not None,
        )
        if any(scenario_binding) and not all(scenario_binding):
            raise ValueError(
                "exchange platform, scenario identity, and launch contract must be selected "
                "together"
            )
        return self


class ArchitectConversationLaunchReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    handle: str = Field(pattern=r"^[0-9a-f]{24}$")
    selected_platform: Platform
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    launch_contract_digest: Sha256Digest
    advisory_output_digest: Sha256Digest
    runtime_identity_digest: Sha256Digest | None = None
    model_revision: Sha256Digest | None = None
    launch_token: Sha256Digest
    requested_at: datetime

    @model_validator(mode="after")
    def require_runtime_identity(self) -> ArchitectConversationLaunchReceipt:
        if self.resolved_runtime_identity_digest is None:
            raise ValueError("launch receipt requires a runtime identity")
        return self

    @property
    def resolved_runtime_identity_digest(self) -> Sha256Digest | None:
        return self.runtime_identity_digest or self.model_revision

    @field_validator("requested_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("launch request timestamp must be UTC")
        return value


class ArchitectConversationLaunchIntent(StrictModel):
    """Immutable reservation preventing duplicate runs across launch retries."""

    schema_version: Literal["1.0"] = "1.0"
    handle: str = Field(pattern=r"^[0-9a-f]{24}$")
    selected_platform: Platform
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    launch_contract_digest: Sha256Digest
    advisory_output_digest: Sha256Digest
    runtime_identity_digest: Sha256Digest | None = None
    model_revision: Sha256Digest | None = None
    launch_token: Sha256Digest
    requested_at: datetime

    @model_validator(mode="after")
    def require_runtime_identity(self) -> ArchitectConversationLaunchIntent:
        if self.resolved_runtime_identity_digest is None:
            raise ValueError("launch intent requires a runtime identity")
        return self

    @property
    def resolved_runtime_identity_digest(self) -> Sha256Digest | None:
        return self.runtime_identity_digest or self.model_revision

    @field_validator("requested_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("launch request timestamp must be UTC")
        return value


class _ArchitectConversationLaunchBinding(StrictModel):
    """Exact controller-owned readiness state covered by the browser token."""

    conversation_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    exchange: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES)
    selected_platform: Platform
    scenario_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    launch_contract_digest: Sha256Digest
    advisory_output_digest: Sha256Digest
    runtime_identity_digest: Sha256Digest


class ArchitectConversationSnapshot(StrictModel):
    header: ArchitectConversationHeader
    exchanges: tuple[ArchitectConversationExchange, ...] = Field(
        max_length=MAX_CONVERSATION_EXCHANGES
    )
    launch_intent: ArchitectConversationLaunchIntent | None = None
    launch: ArchitectConversationLaunchReceipt | None = None

    @model_validator(mode="after")
    def validate_sequence_and_launch(self) -> ArchitectConversationSnapshot:
        for expected, exchange in enumerate(self.exchanges, start=1):
            if exchange.exchange != expected:
                raise ValueError("conversation exchanges are not contiguous")
            if exchange.scenario_id is not None and (
                artifact_digest(migration_launch_contract(exchange.scenario_id))
                != exchange.launch_contract_digest
            ):
                raise ValueError(
                    "conversation exchange launch contract differs from the current scenario; "
                    "start a new conversation"
                )
        if self.launch_intent is not None:
            if not self.exchanges:
                raise ValueError("conversation cannot launch without an exchange")
            latest = self.exchanges[-1]
            reply = latest.architect_run.reply
            if reply.status != "ready_to_launch" or reply.advisory_summary is None:
                raise ValueError("launched conversation does not have a ready Architect reply")
            if latest.selected_platform is not self.launch_intent.selected_platform:
                raise ValueError("launch platform differs from the controller-selected platform")
            if latest.scenario_id != self.launch_intent.scenario_id:
                raise ValueError("launch scenario differs from the controller-selected scenario")
            if latest.launch_contract_digest != self.launch_intent.launch_contract_digest:
                raise ValueError("launch contract differs from the ready scenario")
            if (
                latest.architect_run.model_call.output_digest
                != self.launch_intent.advisory_output_digest
            ):
                raise ValueError("launch advisory differs from the ready intake output")
            if (
                latest.architect_run.model_call.resolved_runtime_identity_digest
                != self.launch_intent.resolved_runtime_identity_digest
            ):
                raise ValueError("launch runtime identity differs from the ready intake identity")
            if architect_conversation_launch_token(self) != self.launch_intent.launch_token:
                raise ValueError("launch token differs from the ready intake state")
        if self.launch is not None:
            if self.launch_intent is None or self.launch != ArchitectConversationLaunchReceipt(
                handle=self.launch_intent.handle,
                selected_platform=self.launch_intent.selected_platform,
                scenario_id=self.launch_intent.scenario_id,
                launch_contract_digest=self.launch_intent.launch_contract_digest,
                advisory_output_digest=self.launch_intent.advisory_output_digest,
                runtime_identity_digest=self.launch_intent.runtime_identity_digest,
                model_revision=self.launch_intent.model_revision,
                launch_token=self.launch_intent.launch_token,
                requested_at=self.launch_intent.requested_at,
            ):
                raise ValueError("launch receipt does not match its immutable intent")
        return self

    @property
    def selected_platform(self) -> Platform | None:
        if self.exchanges:
            return self.exchanges[-1].selected_platform
        return self.header.initial_platform

    @property
    def scenario_id(self) -> str | None:
        if self.exchanges:
            return self.exchanges[-1].scenario_id
        return self.header.initial_scenario_id


class ArchitectConversationMessageView(StrictModel):
    sequence: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES * 2)
    role: Literal["user", "architect"]
    content: str = Field(min_length=1, max_length=2_000)


class ArchitectConversationReadinessView(StrictModel):
    ready: bool
    platform: Platform | None
    scenario_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    canonical_request: str | None = Field(default=None, max_length=1_000)
    advisory_summary: str | None = Field(default=None, max_length=1_000)
    launch_contract_digest: Sha256Digest | None = None
    missing_information: tuple[str, ...] = Field(max_length=9)
    launch_token: Sha256Digest | None = None


class ArchitectConversationModelCallView(StrictModel):
    exchange: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES)
    role: Literal["architect"] = "architect"
    agent_version: str = Field(min_length=1, max_length=80)
    latency_ms: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    input_digest: Sha256Digest
    output_digest: Sha256Digest


class ArchitectConversationView(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    conversation_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    status: Literal["open", "ready", "launch_pending", "launched"]
    selected_platform: Platform | None
    selected_scenario_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$",
    )
    messages: tuple[ArchitectConversationMessageView, ...] = Field(
        max_length=MAX_CONVERSATION_EXCHANGES * 2
    )
    readiness: ArchitectConversationReadinessView
    model_calls: tuple[ArchitectConversationModelCallView, ...] = Field(
        max_length=MAX_CONVERSATION_EXCHANGES
    )
    launch_handle: str | None = Field(default=None, pattern=r"^[0-9a-f]{24}$")


class ArchitectConversationStore:
    """Safely persist one immutable header and append-only exchange files."""

    def __init__(self, root: Path) -> None:
        self._store = ArtifactStore(root)
        self.root = self._store.root

    def create(
        self,
        conversation_id: str,
        *,
        initial_platform: Platform | None,
        initial_scenario_id: str | None,
    ) -> ArchitectConversationSnapshot:
        _validate_conversation_id(conversation_id)
        header = ArchitectConversationHeader(
            conversation_id=conversation_id,
            initial_platform=initial_platform,
            initial_scenario_id=initial_scenario_id,
        )
        self._store.write_json(f"{conversation_id}/header.json", header)
        return ArchitectConversationSnapshot(header=header, exchanges=())

    def load(self, conversation_id: str) -> ArchitectConversationSnapshot:
        _validate_conversation_id(conversation_id)
        directory = self.root / conversation_id
        try:
            metadata = directory.lstat()
        except FileNotFoundError:
            raise
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise PolicyViolation("conversation path is not a safe directory")
        try:
            directory.resolve(strict=True).relative_to(self.root)
            names = tuple(sorted(entry.name for entry in directory.iterdir()))
        except (OSError, ValueError) as exc:
            raise PolicyViolation("conversation path could not be verified") from exc
        allowed = {"header.json", "launch-intent.json", "launch.json"}
        exchange_indexes: list[int] = []
        for name in names:
            match = _EXCHANGE_FILE_PATTERN.fullmatch(name)
            if match is not None:
                exchange_indexes.append(int(match.group(1)))
                continue
            if name not in allowed:
                raise PolicyViolation("conversation contains an unexpected artifact")
        if "header.json" not in names:
            raise PolicyViolation("conversation header is missing")

        header = ArchitectConversationHeader.model_validate(
            self._store.read_json(f"{conversation_id}/header.json")
        )
        if header.conversation_id != conversation_id:
            raise PolicyViolation("conversation header identity mismatch")
        if exchange_indexes != list(range(1, len(exchange_indexes) + 1)):
            raise PolicyViolation("conversation exchange sequence is incomplete")
        loaded_exchanges: list[ArchitectConversationExchange] = []
        for exchange_index in exchange_indexes:
            exchange = ArchitectConversationExchange.model_validate(
                self._store.read_json(f"{conversation_id}/exchange-{exchange_index:04d}.json")
            )
            assert_no_request_secrets(
                exchange,
                boundary="conversation exchange",
            )
            loaded_exchanges.append(exchange)
        exchanges = tuple(loaded_exchanges)
        launch_intent = (
            ArchitectConversationLaunchIntent.model_validate(
                self._store.read_json(f"{conversation_id}/launch-intent.json")
            )
            if "launch-intent.json" in names
            else None
        )
        launch = (
            ArchitectConversationLaunchReceipt.model_validate(
                self._store.read_json(f"{conversation_id}/launch.json")
            )
            if "launch.json" in names
            else None
        )
        return ArchitectConversationSnapshot(
            header=header,
            exchanges=exchanges,
            launch_intent=launch_intent,
            launch=launch,
        )

    def append_exchange(
        self,
        conversation_id: str,
        *,
        selected_platform: Platform | None,
        scenario_id: str | None,
        launch_contract_digest: Sha256Digest | None,
        user_message: ArchitectConversationMessage,
        architect_run: ArchitectConversationRun,
    ) -> ArchitectConversationSnapshot:
        with _conversation_mutation_lock(self.root, conversation_id):
            return self._append_exchange_locked(
                conversation_id,
                selected_platform=selected_platform,
                scenario_id=scenario_id,
                launch_contract_digest=launch_contract_digest,
                user_message=user_message,
                architect_run=architect_run,
            )

    def _append_exchange_locked(
        self,
        conversation_id: str,
        *,
        selected_platform: Platform | None,
        scenario_id: str | None,
        launch_contract_digest: Sha256Digest | None,
        user_message: ArchitectConversationMessage,
        architect_run: ArchitectConversationRun,
    ) -> ArchitectConversationSnapshot:
        snapshot = self.load(conversation_id)
        if snapshot.launch_intent is not None or snapshot.launch is not None:
            raise PolicyViolation("launched conversation cannot accept another exchange")
        exchange_number = len(snapshot.exchanges) + 1
        if exchange_number > MAX_CONVERSATION_EXCHANGES:
            raise PolicyViolation("conversation exchange limit reached")
        exchange = ArchitectConversationExchange(
            exchange=exchange_number,
            selected_platform=selected_platform,
            scenario_id=scenario_id,
            launch_contract_digest=launch_contract_digest,
            user_message=user_message,
            architect_run=architect_run,
        )
        assert_no_request_secrets(
            exchange,
            boundary="conversation exchange",
        )
        self._store.write_json(
            f"{conversation_id}/exchange-{exchange_number:04d}.json",
            exchange,
        )
        return ArchitectConversationSnapshot(
            header=snapshot.header,
            exchanges=(*snapshot.exchanges, exchange),
        )

    def begin_launch(
        self,
        conversation_id: str,
        *,
        handle: str,
        expected_launch_token: str,
    ) -> ArchitectConversationSnapshot:
        with _conversation_mutation_lock(self.root, conversation_id):
            return self._begin_launch_locked(
                conversation_id,
                handle=handle,
                expected_launch_token=expected_launch_token,
            )

    def _begin_launch_locked(
        self,
        conversation_id: str,
        *,
        handle: str,
        expected_launch_token: str,
    ) -> ArchitectConversationSnapshot:
        """Atomically bind one run handle to the browser-reviewed intake state.

        The service performs an early token check for a useful response, but
        this store-level check is authoritative: another service instance may
        append an exchange between that read and this durable reservation.
        """

        if _RUN_HANDLE_PATTERN.fullmatch(handle) is None:
            raise ValueError("run handle is invalid")
        if (
            not isinstance(expected_launch_token, str)
            or _LAUNCH_TOKEN_PATTERN.fullmatch(expected_launch_token) is None
        ):
            raise ValueError("launch token is invalid")
        snapshot = self.load(conversation_id)
        current_launch_token = architect_conversation_launch_token(snapshot)
        if current_launch_token != expected_launch_token:
            raise ArchitectConversationStaleLaunch(
                "conversation changed before its launch reservation was recorded"
            )
        if snapshot.launch is not None:
            raise PolicyViolation("conversation launch was already recorded")
        if snapshot.launch_intent is not None:
            # A concurrent exact-token caller may have won the immutable
            # reservation with another random handle. Return that one binding;
            # never publish or start a second handle for the same decision.
            return snapshot
        if not snapshot.exchanges:
            raise PolicyViolation("conversation is not ready to launch")
        latest = snapshot.exchanges[-1]
        reply = latest.architect_run.reply
        if (
            latest.selected_platform is None
            or latest.scenario_id is None
            or latest.launch_contract_digest is None
            or reply.status != "ready_to_launch"
            or reply.advisory_summary is None
            or latest.architect_run.model_call.resolved_runtime_identity_digest is None
        ):
            raise PolicyViolation("conversation is not ready to launch")
        launch_token = architect_conversation_launch_token(snapshot)
        if launch_token is None:
            raise PolicyViolation("conversation readiness token is unavailable")
        intent = ArchitectConversationLaunchIntent(
            handle=handle,
            selected_platform=latest.selected_platform,
            scenario_id=latest.scenario_id,
            launch_contract_digest=latest.launch_contract_digest,
            advisory_output_digest=latest.architect_run.model_call.output_digest,
            runtime_identity_digest=(
                latest.architect_run.model_call.resolved_runtime_identity_digest
            ),
            model_revision=latest.architect_run.model_call.model_revision,
            launch_token=launch_token,
            requested_at=datetime.now(UTC),
        )
        self._store.write_json(f"{conversation_id}/launch-intent.json", intent)
        return ArchitectConversationSnapshot(
            header=snapshot.header,
            exchanges=snapshot.exchanges,
            launch_intent=intent,
        )

    def record_launch(
        self,
        conversation_id: str,
        *,
        handle: str,
    ) -> ArchitectConversationSnapshot:
        with _conversation_mutation_lock(self.root, conversation_id):
            return self._record_launch_locked(conversation_id, handle=handle)

    def _record_launch_locked(
        self,
        conversation_id: str,
        *,
        handle: str,
    ) -> ArchitectConversationSnapshot:
        if _RUN_HANDLE_PATTERN.fullmatch(handle) is None:
            raise ValueError("run handle is invalid")
        snapshot = self.load(conversation_id)
        if snapshot.launch is not None:
            raise PolicyViolation("conversation launch was already recorded")
        if snapshot.launch_intent is None or not snapshot.exchanges:
            raise PolicyViolation("conversation is not ready to launch")
        latest = snapshot.exchanges[-1]
        reply = latest.architect_run.reply
        if (
            latest.selected_platform is None
            or latest.scenario_id is None
            or latest.launch_contract_digest is None
            or reply.status != "ready_to_launch"
            or reply.advisory_summary is None
            or latest.architect_run.model_call.resolved_runtime_identity_digest is None
        ):
            raise PolicyViolation("conversation is not ready to launch")
        launch_token = architect_conversation_launch_token(snapshot)
        if launch_token is None:
            raise PolicyViolation("conversation readiness token is unavailable")
        receipt = ArchitectConversationLaunchReceipt(
            handle=handle,
            selected_platform=latest.selected_platform,
            scenario_id=latest.scenario_id,
            launch_contract_digest=latest.launch_contract_digest,
            advisory_output_digest=latest.architect_run.model_call.output_digest,
            runtime_identity_digest=(
                latest.architect_run.model_call.resolved_runtime_identity_digest
            ),
            model_revision=latest.architect_run.model_call.model_revision,
            launch_token=launch_token,
            requested_at=snapshot.launch_intent.requested_at,
        )
        if (
            snapshot.launch_intent.handle != handle
            or snapshot.launch_intent.selected_platform is not receipt.selected_platform
            or snapshot.launch_intent.scenario_id != receipt.scenario_id
            or snapshot.launch_intent.launch_contract_digest != receipt.launch_contract_digest
            or snapshot.launch_intent.advisory_output_digest != receipt.advisory_output_digest
            or snapshot.launch_intent.resolved_runtime_identity_digest
            != receipt.resolved_runtime_identity_digest
            or snapshot.launch_intent.model_revision != receipt.model_revision
            or snapshot.launch_intent.launch_token != receipt.launch_token
            or snapshot.launch_intent.requested_at != receipt.requested_at
        ):
            raise PolicyViolation("launch result does not match its immutable intent")
        self._store.write_json(f"{conversation_id}/launch.json", receipt)
        return ArchitectConversationSnapshot(
            header=snapshot.header,
            exchanges=snapshot.exchanges,
            launch_intent=snapshot.launch_intent,
            launch=receipt,
        )

    def conversation_count(self) -> int:
        count = 0
        for entry in self.root.iterdir():
            if _CONVERSATION_ID_PATTERN.fullmatch(entry.name) is None:
                continue
            try:
                metadata = entry.lstat()
            except OSError:
                count += 1
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                count += 1
                continue
            count += 1
        return count


def _conversation_mutation_lock(root: Path, conversation_id: str) -> threading.RLock:
    """Share one mutation lock across store instances in the UI process."""

    key = (root, conversation_id)
    with _MUTATION_LOCKS_GUARD:
        return _MUTATION_LOCKS.setdefault(key, threading.RLock())


def project_architect_conversation(
    snapshot: ArchitectConversationSnapshot,
) -> ArchitectConversationView:
    messages: list[ArchitectConversationMessageView] = []
    model_calls: list[ArchitectConversationModelCallView] = []
    for exchange in snapshot.exchanges:
        messages.append(
            ArchitectConversationMessageView(
                sequence=(exchange.exchange * 2) - 1,
                role="user",
                content=exchange.user_message.content,
            )
        )
        messages.append(
            ArchitectConversationMessageView(
                sequence=exchange.exchange * 2,
                role="architect",
                content=exchange.architect_run.reply.assistant_message,
            )
        )
        call = exchange.architect_run.model_call
        usage = call.usage
        model_calls.append(
            ArchitectConversationModelCallView(
                exchange=exchange.exchange,
                agent_version=call.agent_version,
                latency_ms=None if usage is None else usage.latency_ms,
                input_tokens=None if usage is None else usage.input_tokens,
                output_tokens=None if usage is None else usage.output_tokens,
                total_tokens=None if usage is None else usage.total_tokens,
                input_digest=call.input_digest,
                output_digest=call.output_digest,
            )
        )

    platform = snapshot.selected_platform
    scenario_id = snapshot.scenario_id
    launch_contract = None if scenario_id is None else migration_launch_contract(scenario_id)
    if snapshot.exchanges:
        latest_reply = snapshot.exchanges[-1].architect_run.reply
        ready = (
            latest_reply.status == "ready_to_launch"
            and platform is not None
            and scenario_id is not None
        )
        advisory_summary = latest_reply.advisory_summary if ready else None
        missing = list(latest_reply.missing_information)
    else:
        ready = False
        advisory_summary = None
        missing = ["Describe the bounded migration outcome you want."]
    if platform is None and "Select a Salesforce or MuleSoft migration slice." not in missing:
        missing.append("Select a Salesforce or MuleSoft migration slice.")
    if platform is not None and scenario_id is None:
        missing.append("Select one exact bounded migration scenario.")

    status: Literal["open", "ready", "launch_pending", "launched"]
    if snapshot.launch is not None:
        status = "launched"
    elif snapshot.launch_intent is not None:
        status = "launch_pending"
    elif ready:
        status = "ready"
    else:
        status = "open"
    return ArchitectConversationView(
        conversation_id=snapshot.header.conversation_id,
        status=status,
        selected_platform=platform,
        selected_scenario_id=scenario_id,
        messages=tuple(messages),
        readiness=ArchitectConversationReadinessView(
            ready=ready,
            platform=platform,
            scenario_id=scenario_id,
            canonical_request=(
                None if launch_contract is None else launch_contract.canonical_description
            ),
            advisory_summary=advisory_summary,
            launch_contract_digest=(
                snapshot.exchanges[-1].launch_contract_digest if snapshot.exchanges else None
            ),
            missing_information=tuple(missing),
            launch_token=(architect_conversation_launch_token(snapshot) if ready else None),
        ),
        model_calls=tuple(model_calls),
        launch_handle=None if snapshot.launch is None else snapshot.launch.handle,
    )


def conversation_history(
    snapshot: ArchitectConversationSnapshot,
    latest_user_message: ArchitectConversationMessage,
) -> tuple[ArchitectConversationMessage, ...]:
    # Keep the durable public transcript, but send only the newest complete
    # exchanges that fit the model boundary.  Never cut a user/Architect pair,
    # which preserves the typed alternating-role invariant.
    selected_pairs: list[tuple[ArchitectConversationMessage, ArchitectConversationMessage]] = []
    character_count = len(latest_user_message.content)
    for exchange in reversed(snapshot.exchanges):
        architect_message = ArchitectConversationMessage(
            role="architect",
            content=exchange.architect_run.reply.assistant_message,
        )
        pair_count = len(exchange.user_message.content) + len(architect_message.content)
        if character_count + pair_count > 16_000:
            break
        selected_pairs.append((exchange.user_message, architect_message))
        character_count += pair_count
    history: list[ArchitectConversationMessage] = []
    for pair in reversed(selected_pairs):
        history.extend(pair)
    history.append(latest_user_message)
    return tuple(history)


def architect_conversation_launch_token(
    snapshot: ArchitectConversationSnapshot,
) -> str | None:
    """Digest-bind the exact latest ready state exposed to a browser tab."""

    if not snapshot.exchanges:
        return None
    latest = snapshot.exchanges[-1]
    reply = latest.architect_run.reply
    runtime_identity_digest = (
        latest.architect_run.model_call.resolved_runtime_identity_digest
    )
    if (
        latest.selected_platform is None
        or latest.scenario_id is None
        or latest.launch_contract_digest is None
        or reply.status != "ready_to_launch"
        or reply.advisory_summary is None
        or runtime_identity_digest is None
    ):
        return None
    return artifact_digest(
        _ArchitectConversationLaunchBinding(
            conversation_id=snapshot.header.conversation_id,
            exchange=latest.exchange,
            selected_platform=latest.selected_platform,
            scenario_id=latest.scenario_id,
            launch_contract_digest=latest.launch_contract_digest,
            advisory_output_digest=latest.architect_run.model_call.output_digest,
            runtime_identity_digest=runtime_identity_digest,
        )
    )


def _validate_conversation_id(value: str) -> str:
    if not isinstance(value, str) or _CONVERSATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("conversation identifier is invalid")
    return value

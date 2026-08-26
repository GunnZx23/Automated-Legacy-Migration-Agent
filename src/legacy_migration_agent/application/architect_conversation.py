"""Append-only public conversation state for Architect migration intake.

Conversation turns are advisory.  Only the UI service's explicit launch method
may convert the latest controller-selected platform and model-refined request
into an ordinary migration run.
"""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from legacy_migration_agent.agent_runtime.model_agents import (
    ArchitectConversationMessage,
    ArchitectConversationRun,
)
from legacy_migration_agent.contracts import Platform, Sha256Digest, StrictModel
from legacy_migration_agent.core.integrity import ArtifactStore, artifact_digest
from legacy_migration_agent.core.policies import PolicyViolation

MAX_CONVERSATION_EXCHANGES = 12
MAX_CONVERSATIONS = 64
_CONVERSATION_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_RUN_HANDLE_PATTERN = re.compile(r"^[0-9a-f]{24}$")
_EXCHANGE_FILE_PATTERN = re.compile(r"^exchange-([0-9]{4})\.json$")


class ArchitectConversationHeader(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    conversation_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    initial_platform: Platform | None = None


class ArchitectConversationExchange(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    exchange: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES)
    selected_platform: Platform | None = None
    user_message: ArchitectConversationMessage
    architect_run: ArchitectConversationRun

    @model_validator(mode="after")
    def validate_roles(self) -> ArchitectConversationExchange:
        if self.user_message.role != "user":
            raise ValueError("conversation exchange must contain a user message")
        return self


class ArchitectConversationLaunchReceipt(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    handle: str = Field(pattern=r"^[0-9a-f]{24}$")
    selected_platform: Platform
    refined_request_digest: Sha256Digest
    model_revision: Sha256Digest
    launch_token: Sha256Digest


class ArchitectConversationLaunchIntent(StrictModel):
    """Immutable reservation preventing duplicate runs across launch retries."""

    schema_version: Literal["1.0"] = "1.0"
    handle: str = Field(pattern=r"^[0-9a-f]{24}$")
    selected_platform: Platform
    refined_request_digest: Sha256Digest
    model_revision: Sha256Digest
    launch_token: Sha256Digest


class _ArchitectConversationLaunchBinding(StrictModel):
    """Exact controller-owned readiness state covered by the browser token."""

    conversation_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    exchange: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES)
    selected_platform: Platform
    refined_request: str = Field(min_length=10, max_length=1_000)
    model_revision: Sha256Digest


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
        if self.launch_intent is not None:
            if not self.exchanges:
                raise ValueError("conversation cannot launch without an exchange")
            latest = self.exchanges[-1]
            reply = latest.architect_run.reply
            if reply.status != "ready_to_launch" or reply.refined_request is None:
                raise ValueError("launched conversation does not have a ready Architect reply")
            if latest.selected_platform is not self.launch_intent.selected_platform:
                raise ValueError("launch platform differs from the controller-selected platform")
            if _text_digest(reply.refined_request) != self.launch_intent.refined_request_digest:
                raise ValueError("launch request differs from the ready refined request")
            if latest.architect_run.model_call.model_revision != self.launch_intent.model_revision:
                raise ValueError("launch model revision differs from the ready intake revision")
            if architect_conversation_launch_token(self) != self.launch_intent.launch_token:
                raise ValueError("launch token differs from the ready intake state")
        if self.launch is not None:
            if self.launch_intent is None or self.launch != ArchitectConversationLaunchReceipt(
                handle=self.launch_intent.handle,
                selected_platform=self.launch_intent.selected_platform,
                refined_request_digest=self.launch_intent.refined_request_digest,
                model_revision=self.launch_intent.model_revision,
                launch_token=self.launch_intent.launch_token,
            ):
                raise ValueError("launch receipt does not match its immutable intent")
        return self

    @property
    def selected_platform(self) -> Platform | None:
        if self.exchanges:
            return self.exchanges[-1].selected_platform
        return self.header.initial_platform


class ArchitectConversationMessageView(StrictModel):
    sequence: int = Field(ge=1, le=MAX_CONVERSATION_EXCHANGES * 2)
    role: Literal["user", "architect"]
    content: str = Field(min_length=1, max_length=2_000)


class ArchitectConversationReadinessView(StrictModel):
    ready: bool
    platform: Platform | None
    refined_request: str | None = Field(default=None, max_length=1_000)
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
    ) -> ArchitectConversationSnapshot:
        _validate_conversation_id(conversation_id)
        header = ArchitectConversationHeader(
            conversation_id=conversation_id,
            initial_platform=initial_platform,
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
        exchanges = tuple(
            ArchitectConversationExchange.model_validate(
                self._store.read_json(f"{conversation_id}/exchange-{exchange_index:04d}.json")
            )
            for exchange_index in exchange_indexes
        )
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
            user_message=user_message,
            architect_run=architect_run,
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
    ) -> ArchitectConversationSnapshot:
        """Reserve exactly one run handle before any workflow side effect."""

        if _RUN_HANDLE_PATTERN.fullmatch(handle) is None:
            raise ValueError("run handle is invalid")
        snapshot = self.load(conversation_id)
        if snapshot.launch is not None:
            raise PolicyViolation("conversation launch was already recorded")
        if snapshot.launch_intent is not None:
            if snapshot.launch_intent.handle != handle:
                raise PolicyViolation("conversation already reserved a different run handle")
            return snapshot
        if not snapshot.exchanges:
            raise PolicyViolation("conversation is not ready to launch")
        latest = snapshot.exchanges[-1]
        reply = latest.architect_run.reply
        if (
            latest.selected_platform is None
            or reply.status != "ready_to_launch"
            or reply.refined_request is None
            or latest.architect_run.model_call.model_revision is None
        ):
            raise PolicyViolation("conversation is not ready to launch")
        launch_token = architect_conversation_launch_token(snapshot)
        if launch_token is None:
            raise PolicyViolation("conversation readiness token is unavailable")
        intent = ArchitectConversationLaunchIntent(
            handle=handle,
            selected_platform=latest.selected_platform,
            refined_request_digest=_text_digest(reply.refined_request),
            model_revision=latest.architect_run.model_call.model_revision,
            launch_token=launch_token,
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
            or reply.status != "ready_to_launch"
            or reply.refined_request is None
            or latest.architect_run.model_call.model_revision is None
        ):
            raise PolicyViolation("conversation is not ready to launch")
        launch_token = architect_conversation_launch_token(snapshot)
        if launch_token is None:
            raise PolicyViolation("conversation readiness token is unavailable")
        receipt = ArchitectConversationLaunchReceipt(
            handle=handle,
            selected_platform=latest.selected_platform,
            refined_request_digest=_text_digest(reply.refined_request),
            model_revision=latest.architect_run.model_call.model_revision,
            launch_token=launch_token,
        )
        if (
            snapshot.launch_intent.handle != handle
            or snapshot.launch_intent.selected_platform is not receipt.selected_platform
            or snapshot.launch_intent.refined_request_digest != receipt.refined_request_digest
            or snapshot.launch_intent.model_revision != receipt.model_revision
            or snapshot.launch_intent.launch_token != receipt.launch_token
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
    if snapshot.exchanges:
        latest_reply = snapshot.exchanges[-1].architect_run.reply
        ready = latest_reply.status == "ready_to_launch" and platform is not None
        refined_request = latest_reply.refined_request if ready else None
        missing = list(latest_reply.missing_information)
    else:
        ready = False
        refined_request = None
        missing = ["Describe the bounded migration outcome you want."]
    if platform is None and "Select a Salesforce or MuleSoft migration slice." not in missing:
        missing.append("Select a Salesforce or MuleSoft migration slice.")

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
        messages=tuple(messages),
        readiness=ArchitectConversationReadinessView(
            ready=ready,
            platform=platform,
            refined_request=refined_request,
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
    model_revision = latest.architect_run.model_call.model_revision
    if (
        latest.selected_platform is None
        or reply.status != "ready_to_launch"
        or reply.refined_request is None
        or model_revision is None
    ):
        return None
    return artifact_digest(
        _ArchitectConversationLaunchBinding(
            conversation_id=snapshot.header.conversation_id,
            exchange=latest.exchange,
            selected_platform=latest.selected_platform,
            refined_request=reply.refined_request,
            model_revision=model_revision,
        )
    )


def _validate_conversation_id(value: str) -> str:
    if not isinstance(value, str) or _CONVERSATION_ID_PATTERN.fullmatch(value) is None:
        raise ValueError("conversation identifier is invalid")
    return value


def _text_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import Field

from legacy_migration_agent.agent_runtime.openai_model import (
    LiveModelApproval,
    ModelCallRecord,
    ModelConfigurationError,
    ModelEvidenceError,
    ModelOutputError,
    ModelRefusalError,
    OpenAIResponsesModelClient,
    model_call_record,
    verify_model_call_record,
)
from legacy_migration_agent.contracts import StrictModel


class Input(StrictModel):
    value: str


class Output(StrictModel):
    answer: str = Field(min_length=1)


class CapturingResponses:
    def __init__(self, response) -> None:
        self.response = response
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class NoStoreResponses:
    def __init__(self, response) -> None:
        self.response = response
        self.kwargs = None

    def parse(self, *, model, instructions, input, text_format):
        self.kwargs = {
            "model": model,
            "instructions": instructions,
            "input": input,
            "text_format": text_format,
        }
        return self.response


def client_for(responses, **kwargs) -> OpenAIResponsesModelClient:
    return OpenAIResponsesModelClient(
        "gpt-test-explicit",
        client=SimpleNamespace(responses=responses),
        **kwargs,
    )


def test_adapter_calls_responses_parse_with_schema_store_false_and_no_tools() -> None:
    responses = CapturingResponses(
        SimpleNamespace(status="completed", output=(), output_parsed=Output(answer="ok"))
    )
    client = client_for(responses)

    result = client.parse(
        system_prompt="You are a bounded test role.",
        input_value=Input(value="frozen"),
        output_type=Output,
    )

    assert result == Output(answer="ok")
    assert responses.kwargs["model"] == "gpt-test-explicit"
    assert responses.kwargs["text_format"] is Output
    assert responses.kwargs["store"] is False
    assert "tools" not in responses.kwargs
    assert '"value":"frozen"' in responses.kwargs["input"]
    assert client.store_false_sent is True
    assert client.live_invocation is False


def test_adapter_omits_store_only_when_client_signature_does_not_support_it() -> None:
    responses = NoStoreResponses(
        SimpleNamespace(status="completed", output=(), output_parsed={"answer": "ok"})
    )
    client = client_for(responses)

    assert client.parse(
        system_prompt="bounded",
        input_value=Input(value="x"),
        output_type=Output,
    ) == Output(answer="ok")
    assert client.store_false_sent is False
    assert "store" not in responses.kwargs


def test_live_adapter_requires_explicit_consent_and_api_key_even_with_client() -> None:
    fake = SimpleNamespace(responses=CapturingResponses(None))
    with pytest.raises(ModelConfigurationError, match="approval"):
        OpenAIResponsesModelClient("gpt-test", client=fake, live_invocation=True)
    with pytest.raises(ModelConfigurationError, match="API key"):
        OpenAIResponsesModelClient(
            "gpt-test",
            client=fake,
            live_invocation=True,
            approval=LiveModelApproval(
                allow_live_api=True,
                allow_prompt_data_sharing=True,
                approved_by="reviewer",
            ),
        )


def test_offline_adapter_rejects_credentials_and_requires_injected_client() -> None:
    with pytest.raises(ModelConfigurationError, match="offline use requires"):
        OpenAIResponsesModelClient("gpt-test")
    with pytest.raises(ModelConfigurationError, match="credentials"):
        client_for(CapturingResponses(None), api_key="not-used")


def test_adapter_rejects_refusal_incomplete_and_missing_parsed_output() -> None:
    refusal = SimpleNamespace(
        status="completed",
        output=(SimpleNamespace(content=({"type": "refusal", "refusal": "no"},)),),
        output_parsed=None,
    )
    with pytest.raises(ModelRefusalError, match="refused"):
        client_for(CapturingResponses(refusal)).parse(
            system_prompt="bounded", input_value=Input(value="x"), output_type=Output
        )

    incomplete = SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output=(),
        output_parsed=None,
    )
    with pytest.raises(ModelOutputError, match="incomplete"):
        client_for(CapturingResponses(incomplete)).parse(
            system_prompt="bounded", input_value=Input(value="x"), output_type=Output
        )

    missing = SimpleNamespace(status="completed", output=(), output_parsed=None)
    with pytest.raises(ModelOutputError, match="did not contain"):
        client_for(CapturingResponses(missing)).parse(
            system_prompt="bounded", input_value=Input(value="x"), output_type=Output
        )


def test_adapter_revalidates_provider_output_against_requested_schema() -> None:
    invalid = SimpleNamespace(
        status="completed", output=(), output_parsed={"answer": "", "extra": "drift"}
    )
    with pytest.raises(ModelOutputError, match="schema validation"):
        client_for(CapturingResponses(invalid)).parse(
            system_prompt="bounded", input_value=Input(value="x"), output_type=Output
        )


def test_model_call_record_binds_agent_prompt_input_and_output() -> None:
    client = client_for(
        CapturingResponses(
            SimpleNamespace(status="completed", output=(), output_parsed=Output(answer="ok"))
        )
    )
    input_value = Input(value="frozen")
    output_value = client.parse(
        system_prompt="bounded role",
        input_value=input_value,
        output_type=Output,
    )
    record = model_call_record(
        client,
        agent_version="role/v1",
        agent_definition_digest="sha256:" + "a" * 64,
        system_prompt="bounded role",
        input_value=input_value,
        output_value=output_value,
    )

    verify_model_call_record(
        record,
        agent_version="role/v1",
        agent_definition_digest="sha256:" + "a" * 64,
        system_prompt="bounded role",
        input_value=input_value,
        output_value=output_value,
    )

    with pytest.raises(ModelEvidenceError, match="agent definition, system prompt, output"):
        verify_model_call_record(
            record,
            agent_version="role/v1",
            agent_definition_digest="sha256:" + "b" * 64,
            system_prompt="changed role",
            input_value=input_value,
            output_value=Output(answer="changed"),
        )


def test_model_call_record_captures_sanitized_provider_usage_and_latency() -> None:
    response = SimpleNamespace(
        status="completed",
        output=(),
        output_parsed=Output(answer="ok"),
        usage=SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18),
    )
    client = client_for(CapturingResponses(response))
    input_value = Input(value="frozen")
    output_value = client.parse(
        system_prompt="bounded role",
        input_value=input_value,
        output_type=Output,
    )
    record = model_call_record(
        client,
        agent_version="role/v1",
        agent_definition_digest="sha256:" + "a" * 64,
        system_prompt="bounded role",
        input_value=input_value,
        output_value=output_value,
    )

    assert record.usage is not None
    assert record.execution_boundary == "offline_recorded"
    assert record.usage.latency_ms >= 0
    assert record.usage.provider_usage_reported is True
    assert record.usage.input_tokens == 11
    assert record.usage.output_tokens == 7
    assert record.usage.total_tokens == 18


def test_live_model_record_requires_measured_telemetry_and_storage_control() -> None:
    response = SimpleNamespace(
        status="completed",
        output=(),
        output_parsed=Output(answer="ok"),
        usage=None,
    )
    client = client_for(
        CapturingResponses(response),
        live_invocation=True,
        api_key="not-persisted",
        approval=LiveModelApproval(
            allow_live_api=True,
            allow_prompt_data_sharing=True,
            approved_by="reviewer",
        ),
    )
    input_value = Input(value="frozen")
    output_value = client.parse(
        system_prompt="bounded role",
        input_value=input_value,
        output_type=Output,
    )
    record = model_call_record(
        client,
        agent_version="role/v1",
        agent_definition_digest="sha256:" + "a" * 64,
        system_prompt="bounded role",
        input_value=input_value,
        output_value=output_value,
    )

    assert record.live_invocation is True
    assert record.store_false_sent is True
    assert record.execution_boundary == "remote_no_store"
    assert record.live_approval == LiveModelApproval(
        allow_live_api=True,
        allow_prompt_data_sharing=True,
        approved_by="reviewer",
    )
    assert record.usage is not None
    assert record.usage.provider_usage_reported is False


def test_adapter_rejects_invalid_provider_usage_counts() -> None:
    response = SimpleNamespace(
        status="completed",
        output=(),
        output_parsed=Output(answer="ok"),
        usage=SimpleNamespace(input_tokens=True, output_tokens=1, total_tokens=2),
    )
    with pytest.raises(ModelOutputError, match="invalid token count"):
        client_for(CapturingResponses(response)).parse(
            system_prompt="bounded",
            input_value=Input(value="x"),
            output_type=Output,
        )


def test_live_model_call_record_requires_store_false_evidence() -> None:
    class UnsafeLiveClient:
        provider = "unsafe-live-test"
        model_id = "unsafe/v1"
        live_invocation = True
        store_false_sent = False

    with pytest.raises(ModelEvidenceError, match="storage was disabled"):
        model_call_record(
            UnsafeLiveClient(),  # type: ignore[arg-type]
            agent_version="role/v1",
            agent_definition_digest="sha256:" + "a" * 64,
            system_prompt="bounded role",
            input_value=Input(value="frozen"),
            output_value=Output(answer="ok"),
        )


def test_model_call_record_rejects_approval_evidence_on_offline_records() -> None:
    with pytest.raises(ValueError, match="offline model records"):
        ModelCallRecord(
            provider="offline-test",
            model_id="offline-model",
            agent_version="role/v1",
            agent_definition_digest="sha256:" + "a" * 64,
            live_invocation=False,
            store_false_sent=False,
            live_approval=LiveModelApproval(
                allow_live_api=True,
                allow_prompt_data_sharing=True,
                approved_by="reviewer",
            ),
            system_prompt_digest="sha256:" + "b" * 64,
            input_digest="sha256:" + "c" * 64,
            output_digest="sha256:" + "d" * 64,
        )


def test_legacy_model_call_record_infers_boundary_without_reserializing_a_field() -> None:
    record = ModelCallRecord.model_validate(
        {
            "provider": "legacy-offline-test",
            "model_id": "legacy-model",
            "agent_version": "role/v1",
            "agent_definition_digest": "sha256:" + "a" * 64,
            "live_invocation": False,
            "store_false_sent": False,
            "live_approval": None,
            "system_prompt_digest": "sha256:" + "b" * 64,
            "input_digest": "sha256:" + "c" * 64,
            "output_digest": "sha256:" + "d" * 64,
            "usage": None,
        }
    )

    assert record.execution_boundary is None
    assert record.resolved_execution_boundary == "offline_recorded"
    assert "execution_boundary" not in record.model_dump(mode="json")

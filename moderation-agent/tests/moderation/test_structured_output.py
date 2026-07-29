import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from agents.moderation.nodes.structured_output import bind_moderation_structured_output
from schema.models import DeepseekModelName, OpenAIModelName


class ResultSchema(BaseModel):
    value: str


class RecordingModel:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.kwargs = kwargs
        return self


class PlainJsonModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = []

    async def ainvoke(self, messages, config):
        self.messages = messages
        return AIMessage(content=self.response)


@pytest.mark.asyncio
async def test_deepseek_uses_locally_parsed_json_without_response_format() -> None:
    model = PlainJsonModel('{"value": "validated"}')

    runnable = bind_moderation_structured_output(
        model,
        ResultSchema,
        model_name=DeepseekModelName.DEEPSEEK_V4_FLASH,
    )
    result = await runnable.ainvoke([HumanMessage(content="Return a value.")])

    assert result == ResultSchema(value="validated")
    assert "Return exactly one JSON object" in model.messages[0].content


@pytest.mark.asyncio
async def test_deepseek_raw_result_preserves_parse_error() -> None:
    model = PlainJsonModel("not-json")

    runnable = bind_moderation_structured_output(
        model,
        ResultSchema,
        model_name=DeepseekModelName.DEEPSEEK_V4_FLASH,
        include_raw=True,
    )
    result = await runnable.ainvoke([HumanMessage(content="Return a value.")])

    assert result["parsed"] is None
    assert result["raw"].content == "not-json"
    assert result["parsing_error"] is not None


def test_native_provider_keeps_its_default_structured_output_method() -> None:
    model = RecordingModel()

    bind_moderation_structured_output(
        model,
        ResultSchema,
        model_name=OpenAIModelName.GPT_5_NANO,
        include_raw=True,
    )

    assert model.kwargs == {"include_raw": True}

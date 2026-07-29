from typing import Any

from langchain_core.messages import SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import RunnableConfig, RunnableLambda
from pydantic import BaseModel

from schema.models import DeepseekModelName, OpenAICompatibleName, OpenRouterModelName

_FUNCTION_CALLING_MODELS = (*OpenAICompatibleName, *OpenRouterModelName)


def bind_moderation_structured_output[ResultT: BaseModel](
    model: Any,
    schema: type[ResultT],
    *,
    model_name: object,
    include_raw: bool = False,
) -> Any:
    """Bind structured output without assuming OpenAI response-format support."""
    if any(str(model_name) == candidate.value for candidate in DeepseekModelName):
        return _bind_plain_json_output(model, schema, include_raw=include_raw)

    kwargs: dict[str, Any] = {"include_raw": include_raw}
    if any(str(model_name) == candidate.value for candidate in _FUNCTION_CALLING_MODELS):
        kwargs["method"] = "function_calling"
    return model.with_structured_output(schema, **kwargs)


def _bind_plain_json_output[ResultT: BaseModel](
    model: Any,
    schema: type[ResultT],
    *,
    include_raw: bool,
) -> RunnableLambda:
    parser: PydanticOutputParser[ResultT] = PydanticOutputParser(pydantic_object=schema)
    format_message = SystemMessage(
        content=(
            "Return exactly one JSON object with no Markdown or commentary. "
            f"{parser.get_format_instructions()}"
        )
    )

    async def invoke(messages: Any, config: RunnableConfig) -> Any:
        raw = await model.ainvoke([format_message, *messages], config)
        try:
            parsed = await parser.ainvoke(raw, config)
        except Exception as exc:
            if include_raw:
                return {"raw": raw, "parsed": None, "parsing_error": exc}
            raise
        if include_raw:
            return {"raw": raw, "parsed": parsed, "parsing_error": None}
        return parsed

    return RunnableLambda(invoke)

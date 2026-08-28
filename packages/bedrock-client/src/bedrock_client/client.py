"""Constructing Bedrock clients and sending one prompt to one of them."""

from __future__ import annotations

from typing import Any

import boto3
from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage

from bedrock_client.responses import message_text


def build_runtime_client(region: str) -> Any:
    """Return a ``bedrock-runtime`` boto3 client for `region`.

    Typed ``Any`` because boto3 builds its clients dynamically and ships no
    static type for one; this is the single place that escape hatch lives rather
    than one at every call site.

    Not cached here. Building a client costs a credential resolution and a
    metadata load, so callers do cache it — but the cache belongs with the
    caller's settings, which is what decides when it is stale. A cache in here
    keyed on the arguments would be a second lifetime nobody owns.
    """
    return boto3.client("bedrock-runtime", region_name=region)


def build_chat_model(
    *,
    client: Any,
    model_id: str,
    temperature: float,
    max_tokens: int,
) -> ChatBedrock:
    """Return a chat model bound to an existing runtime client.

    The client is passed in rather than built here so that a service making two
    calls to two models — TASK-030 uses Sonnet for the note and Haiku for the
    code extraction — pays for one boto3 client instead of one per model.

    ``model=`` rather than ``model_id=``: langchain-aws aliases the field and
    only the alias appears in the constructor it synthesises, so the other
    spelling works at runtime and fails type checking.
    """
    return ChatBedrock(
        client=client,
        model=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def invoke(model: ChatBedrock, prompt: str) -> str:
    """Send one prompt as a single human message and return the answer's text.

    Raises whatever the Bedrock client raises. What a failed invocation means is
    the caller's decision: in ``track-b-rag`` it means the same as an
    unparseable answer, and in ``track-a-clinical`` it aborts a note generation
    that the buffered transcript makes retryable.
    """
    response = await model.ainvoke([HumanMessage(content=prompt)])
    return message_text(response.content)

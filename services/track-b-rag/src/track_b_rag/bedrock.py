"""The Bedrock reasoning model, reached through LangChain.

Claude is called through AWS Bedrock and never through Anthropic's direct API:
Bedrock is the HIPAA-eligible path covered by the signed BAA (CLAUDE.md, Key
Architectural Constraints; Known Constraints #2 in TASKS.md names
``langchain_aws.ChatBedrock`` over a ``boto3`` ``bedrock-runtime`` client as the
way to do it). Nothing here imports ``anthropic``.

The model id comes from ``BEDROCK_MODEL_ID_REASONING`` — Sonnet, because this
service's one LLM call site reasons over retrieved policy text rather than
extracting fields from it. The id is never written as a literal anywhere in the
codebase, so switching models is an environment change.

**No PHI reaches this module.** The only prompt built in this service describes
a payer, a plan type, a state and a CPT code, plus policy text retrieved from
public payer publications — see :mod:`track_b_rag.policy_rules` for why the
patient's clinical context is deliberately kept out of the prompt. That is a
property of the two-stage design rather than a rule this module enforces, but
it is the reason no redaction happens here.

Both the client and the chat model are lazy singletons. Building a boto3 client
costs a credential resolution and a metadata load, and a unit suite that imports
a route module should pay for neither.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Final

from langchain_aws import ChatBedrock

import bedrock_client
from bedrock_client import message_text
from track_b_rag.config import get_settings

logger = logging.getLogger(__name__)

#: ``message_text`` is re-exported rather than re-implemented: this module stays
#: the one place this service reaches for Bedrock, and the implementation is
#: shared with every other service through ``packages/bedrock-client``.
__all__ = ["get_reasoning_model", "get_runtime_client", "invoke_reasoning", "message_text"]

#: Deterministic decoding. The prompt asks for a JSON document matching a fixed
#: shape, so sampling buys nothing and costs reproducibility — two identical
#: cache misses should not disagree about what a payer requires.
TEMPERATURE: Final = 0.0

#: Enough for a policy summary with a dozen criteria, and a bound on what a
#: runaway generation can cost. A truncated response fails to parse, which the
#: caller already handles by retrying and then falling back.
MAX_TOKENS: Final = 2048


@lru_cache(maxsize=1)
def get_runtime_client() -> Any:
    """Return the process-wide ``bedrock-runtime`` boto3 client.

    The construction lives in ``packages/bedrock-client``; the cache lives here,
    because this service's settings are what decide the region and therefore
    when the client is stale.
    """
    return bedrock_client.build_runtime_client(get_settings().aws_region)


@lru_cache(maxsize=1)
def get_reasoning_model() -> ChatBedrock:
    """Return the Sonnet chat model used for policy analysis."""
    settings = get_settings()
    logger.info("Using Bedrock reasoning model %r", settings.bedrock_model_id_reasoning)
    return bedrock_client.build_chat_model(
        client=get_runtime_client(),
        model_id=settings.bedrock_model_id_reasoning,
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )


def reset_clients() -> None:
    """Forget the cached client and model. For tests and shutdown."""
    get_reasoning_model.cache_clear()
    get_runtime_client.cache_clear()


async def invoke_reasoning(prompt: str) -> str:
    """Send one prompt to the reasoning model and return its text.

    Raises whatever the Bedrock client raises. The caller decides what a failed
    invocation means; in this service it means the same as an unparseable
    answer — see :mod:`track_b_rag.policy_rules`.
    """
    return await bedrock_client.invoke(get_reasoning_model(), prompt)

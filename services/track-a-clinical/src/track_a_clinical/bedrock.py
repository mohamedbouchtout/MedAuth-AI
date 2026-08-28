"""The two Bedrock models this service calls, reached through LangChain.

Claude is called through AWS Bedrock and never through Anthropic's direct API:
Bedrock is the HIPAA-eligible path covered by the signed BAA (CLAUDE.md, Key
Architectural Constraints; Known Constraints #2 in TASKS.md names
``langchain_aws.ChatBedrock`` over a raw ``bedrock-runtime`` client as the way
to do it). Nothing here imports ``anthropic``.

**Two models, because TASK-030 makes two calls.** CLAUDE.md's Bedrock Model
Assignment table names both call sites: the SOAP note is long-form structured
clinical writing and goes to Sonnet through ``BEDROCK_MODEL_ID_REASONING``, and
the ICD-10/CPT pass is extraction rather than reasoning and goes to Haiku
through ``BEDROCK_MODEL_ID_FAST`` at roughly a fifteenth of the cost. Neither id
is ever written as a literal, so changing a model is an environment change.

Both share one ``bedrock-runtime`` client. Construction lives in
``packages/bedrock-client``; the caches live here, because this service's
settings are what decide the region and the model ids and therefore when a
cached object is stale.

**Every prompt built for these models carries PHI**, unlike track-b-rag's, whose
prompts describe a payer and a procedure. The transcript of a clinical encounter
goes to Bedrock and nowhere else: this module logs which model it used and never
what was sent to it. See :mod:`track_a_clinical.soap`.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Final

from langchain_aws import ChatBedrock

import bedrock_client
from track_a_clinical.config import get_settings

logger = logging.getLogger(__name__)

#: Deterministic decoding. Both prompts ask for a JSON document of a fixed
#: shape, and a note regenerated from the same transcript should not differ
#: because of sampling. It also keeps the tests' expectations meaningful.
TEMPERATURE: Final = 0.0

#: A full SOAP note for a long encounter, with headroom. Larger than
#: track-b-rag's 2048 because this is prose across four sections rather than a
#: criteria list, and a truncated answer fails to parse and costs a retry.
SOAP_MAX_TOKENS: Final = 4096

#: The extraction pass returns a short list of codes, so it needs a fraction of
#: the budget — and capping it low bounds what a model that starts narrating
#: instead of extracting can spend.
EXTRACTION_MAX_TOKENS: Final = 1024


@lru_cache(maxsize=1)
def get_runtime_client() -> Any:
    """Return the process-wide ``bedrock-runtime`` boto3 client.

    Typed ``Any`` because boto3 builds its clients dynamically and ships no
    static type for one. Shared by both models: two chat models over one client
    is one credential resolution and one metadata load, not two.
    """
    return bedrock_client.build_runtime_client(get_settings().aws_region)


@lru_cache(maxsize=1)
def get_reasoning_model() -> ChatBedrock:
    """Return the Sonnet chat model that writes the SOAP note."""
    settings = get_settings()
    logger.info("Using Bedrock reasoning model %r", settings.bedrock_model_id_reasoning)
    return bedrock_client.build_chat_model(
        client=get_runtime_client(),
        model_id=settings.bedrock_model_id_reasoning,
        temperature=TEMPERATURE,
        max_tokens=SOAP_MAX_TOKENS,
    )


@lru_cache(maxsize=1)
def get_fast_model() -> ChatBedrock:
    """Return the Haiku chat model that extracts ICD-10 and CPT codes."""
    settings = get_settings()
    logger.info("Using Bedrock fast model %r", settings.bedrock_model_id_fast)
    return bedrock_client.build_chat_model(
        client=get_runtime_client(),
        model_id=settings.bedrock_model_id_fast,
        temperature=TEMPERATURE,
        max_tokens=EXTRACTION_MAX_TOKENS,
    )


def reset_clients() -> None:
    """Forget the cached client and both models. For tests and shutdown."""
    get_reasoning_model.cache_clear()
    get_fast_model.cache_clear()
    get_runtime_client.cache_clear()


async def invoke_reasoning(prompt: str) -> str:
    """Send one prompt to Sonnet and return its text.

    Raises whatever the Bedrock client raises. What a failure means is the
    caller's decision — in this service it aborts a generation that the retained
    transcript buffer makes retryable. See :mod:`track_a_clinical.consumer`.
    """
    return await bedrock_client.invoke(get_reasoning_model(), prompt)


async def invoke_fast(prompt: str) -> str:
    """Send one prompt to Haiku and return its text."""
    return await bedrock_client.invoke(get_fast_model(), prompt)

"""Shared AWS Bedrock access for every service that calls Claude.

**Scope note (read first):** this package builds Bedrock clients and reads what
comes back from them. It is **not** an LLM framework and holds no prompts, no
model-selection policy and no service's settings. Which model answers which call
site is fixed in CLAUDE.md's Bedrock Model Assignment table and read from each
service's own ``Settings``; this package takes a model id and a region as
arguments and has no opinion about either.

It was extracted in TASK-030, when ``track-a-clinical`` became the second
service to call Bedrock and would otherwise have started — as ``track-b-rag``
once did with the response envelope — as a copy of the first one's module. Two
things in here are worth not having two copies of:

* :func:`message_text` exists because ``AIMessage.content`` is a plain string
  for a simple completion and a list of typed blocks for anything structured. A
  caller that assumes the string form silently receives
  ``"[{'type': 'text', ...}]"`` instead of the answer, which parses as no JSON
  at all.
* :func:`first_json_object` exists because a model asked for bare JSON returns
  it wrapped in a markdown fence or prefaced with a sentence often enough to
  matter. It counts braces while tracking string literals and their escapes, so
  a brace inside a criterion's text does not close the object.

Both were found the hard way in ``track-b-rag`` and both fail *quietly* when
they are wrong — a duplicated copy would keep failing quietly in one service
after the other was fixed, which is the whole argument for the package.

Claude is reached through Bedrock and never through Anthropic's direct API:
Bedrock is the HIPAA-eligible path covered by the signed BAA (CLAUDE.md, Key
Architectural Constraints). Nothing here imports ``anthropic``.

**No PHI discipline lives here, because no PHI passes through here as data.**
Prompts arrive already built and answers leave as text; a caller that puts
clinical content in a prompt is the one responsible for not logging it. This
module logs nothing about either.
"""

from __future__ import annotations

from bedrock_client.client import build_chat_model, build_runtime_client, invoke
from bedrock_client.responses import first_json_object, message_text

__all__ = [
    "build_chat_model",
    "build_runtime_client",
    "first_json_object",
    "invoke",
    "message_text",
]

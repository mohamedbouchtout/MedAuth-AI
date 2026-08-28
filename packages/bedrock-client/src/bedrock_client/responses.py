"""Reading what a Bedrock model sent back.

Two pure functions, no AWS and no I/O. Both exist because of a specific way a
model answer is not the shape the naive reading assumes, and both fail silently
rather than loudly when they are missing — see the package docstring.
"""

from __future__ import annotations


def message_text(content: object) -> str:
    """Flatten a LangChain message's content into plain text.

    ``AIMessage.content`` is a string for a simple completion but a list of
    typed blocks when the model returns anything structured, and a caller that
    assumes the string form silently sees ``"[{'type': 'text', ...}]"`` instead
    of the answer. Non-text blocks — a thinking block, a tool call — are dropped
    rather than rendered.

    Anything else is stringified rather than raising: an unanticipated shape
    becomes an unparseable answer, which every caller already handles, instead
    of an exception from inside the client layer.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def first_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` span in `text`, or None if there is none.

    Tolerates the two things a model does to JSON it was asked to return bare:
    wrapping it in a markdown fence, and prefacing it with a sentence.

    Brace counting rather than a regular expression, because the strings inside
    a clinical or policy document contain braces and quotes of their own; the
    scan tracks string literals and their escapes, so a brace inside one does
    not close the object.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None

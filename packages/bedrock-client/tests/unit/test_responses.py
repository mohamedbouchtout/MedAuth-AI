"""Reading a model answer: the two shapes that silently are not what they look like."""

from __future__ import annotations

from bedrock_client import first_json_object, message_text

# --- message_text ----------------------------------------------------------


def test_a_plain_string_response_is_its_own_text() -> None:
    assert message_text("just text") == "just text"


def test_a_block_list_is_flattened() -> None:
    """Anthropic models on Bedrock return content blocks, not a bare string.

    A caller assuming the string form silently gets "[{'type': 'text', ...}]" —
    which parses as no JSON at all and would burn a retry on every call.
    """
    content = [{"type": "text", "text": "first "}, {"type": "text", "text": "second"}]

    assert message_text(content) == "first second"


def test_non_text_blocks_are_dropped_not_rendered() -> None:
    content = [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}]

    assert message_text(content) == "answer"


def test_a_list_of_bare_strings_is_joined() -> None:
    assert message_text(["a", "b"]) == "ab"


def test_a_text_block_with_no_text_key_contributes_nothing() -> None:
    assert message_text([{"type": "text"}]) == ""


def test_anything_else_is_stringified_rather_than_raising() -> None:
    """A shape nobody anticipated becomes an unparseable answer, which is handled."""
    assert message_text(42) == "42"


# --- first_json_object -----------------------------------------------------


def test_a_bare_object_is_returned_whole() -> None:
    assert first_json_object('{"a": 1}') == '{"a": 1}'


def test_a_fenced_object_is_unwrapped() -> None:
    answer = '```json\n{"a": 1}\n```'

    assert first_json_object(answer) == '{"a": 1}'


def test_a_prefacing_sentence_is_skipped() -> None:
    answer = 'Here is the analysis you asked for:\n{"a": 1}'

    assert first_json_object(answer) == '{"a": 1}'


def test_nesting_is_balanced_not_stopped_at_the_first_brace() -> None:
    answer = '{"outer": {"inner": 1}}'

    assert first_json_object(answer) == answer


def test_a_brace_inside_a_string_does_not_close_the_object() -> None:
    """Criteria and clinical text contain braces; a regex would truncate here."""
    answer = '{"criterion": "dose } schedule", "ok": true}'

    assert first_json_object(answer) == answer


def test_an_escaped_quote_does_not_end_the_string() -> None:
    answer = '{"note": "he said \\"no\\" }", "ok": true}'

    assert first_json_object(answer) == answer


def test_text_with_no_object_at_all_returns_none() -> None:
    assert first_json_object("I could not answer that.") is None


def test_an_unbalanced_object_returns_none() -> None:
    """A truncated generation, which the caller treats as an unusable answer."""
    assert first_json_object('{"a": 1') is None


def test_only_the_first_object_is_returned() -> None:
    assert first_json_object('{"a": 1} and then {"b": 2}') == '{"a": 1}'

"""Which bytes are script or style, and the digest taken without them.

The claims that matter most are the two TASK-009 turns on: a document with no
script or style comes back as the identical bytes, and two real fetches of an
Aetna page that differ only in an injected script digest the same.
"""

from __future__ import annotations

import hashlib

import pytest

from html_digest import NON_TEXT_ELEMENTS, html_digest, non_text_spans, strip_non_text
from tests.fixtures import AETNA_FIRST_FETCH, AETNA_SECOND_FETCH

# --- what is removed -------------------------------------------------------


def test_a_script_element_is_removed_whole() -> None:
    assert strip_non_text(b"<p>A</p><script>var t = 1;</script><p>B</p>") == b"<p>A</p><p>B</p>"


def test_a_style_element_is_removed_whole() -> None:
    assert strip_non_text(b"<style>p { color: red }</style><p>A</p>") == b"<p>A</p>"


def test_the_bytes_around_an_element_are_kept_exactly() -> None:
    """Whitespace and newlines either side are document bytes, not the element's."""
    data = b"<p>A</p>\n    <script>x()</script>\n<p>B</p>"

    assert strip_non_text(data) == b"<p>A</p>\n    \n<p>B</p>"


def test_every_element_is_removed() -> None:
    data = b"<script>1</script><p>A</p><style>b{}</style><p>B</p><script>2</script>"

    assert strip_non_text(data) == b"<p>A</p><p>B</p>"


def test_tag_names_match_in_any_case() -> None:
    assert strip_non_text(b"<SCRIPT>x()</ScRiPt><p>A</p>") == b"<p>A</p>"


def test_the_start_tag_may_carry_attributes() -> None:
    data = b'<script type="text/javascript" async>x()</script><p>A</p>'

    assert strip_non_text(data) == b"<p>A</p>"


def test_a_quoted_attribute_value_may_contain_a_closing_bracket() -> None:
    """The ">" inside the quotes does not end the tag, so the content that
    follows is still script content."""
    data = b'<script data-x="a>b">x()</script><p>A</p>'

    assert strip_non_text(data) == b"<p>A</p>"


def test_a_self_closing_script_still_opens_raw_text() -> None:
    """HTML ignores "/>" on a script tag, so everything up to the end tag is script."""
    assert strip_non_text(b"<script src=x.js/><p>hidden</p></script><p>A</p>") == b"<p>A</p>"


def test_the_end_tag_may_carry_whitespace() -> None:
    assert strip_non_text(b"<script>x()</script\n  ><p>A</p>") == b"<p>A</p>"


def test_the_first_end_tag_closes_the_element_even_inside_a_string() -> None:
    """What a browser does: the tokenizer does not understand JavaScript strings.
    Matching that is the point — our boundaries must be the real ones."""
    data = b'<script>var s = "</script>";</script><p>A</p>'

    assert strip_non_text(data) == b'";</script><p>A</p>'


def test_an_unterminated_element_runs_to_the_end() -> None:
    assert strip_non_text(b"<p>A</p><script>never closed <p>B</p>") == b"<p>A</p>"


def test_an_unterminated_start_tag_runs_to_the_end() -> None:
    assert strip_non_text(b'<p>A</p><script src="x') == b"<p>A</p>"


def test_an_unclosed_quote_runs_the_start_tag_to_the_end() -> None:
    """The ">" after the open quote is inside the value, so nothing closes the tag."""
    assert strip_non_text(b'<p>A</p><script src="x>y') == b"<p>A</p>"


def test_a_start_tag_whose_only_closing_bracket_is_quoted_runs_to_the_end() -> None:
    assert strip_non_text(b'<p>A</p><script data-x="a>b" async') == b"<p>A</p>"


# --- what is not removed ---------------------------------------------------


def test_a_document_without_script_or_style_is_returned_unchanged() -> None:
    """The property that keeps every stored CMS digest valid."""
    data = b"<h1>Lumbar MRI</h1>\n<p>Covered when <b>six weeks</b> of therapy failed.</p>"

    assert strip_non_text(data) is data


def test_a_script_inside_a_comment_is_not_an_element() -> None:
    data = b"<!-- <script>x()</script> --><p>A</p>"

    assert strip_non_text(data) == data


@pytest.mark.parametrize("comment", [b"<!---->", b"<!-->", b"<!--->"])
def test_comments_that_close_themselves_do_not_swallow_the_document(comment: bytes) -> None:
    data = comment + b"<script>x()</script><p>A</p>"

    assert strip_non_text(data) == comment + b"<p>A</p>"


def test_a_comment_closed_by_the_bang_form_ends_there() -> None:
    """HTML also ends a comment at "--!>". A browser runs the script after it,
    so treating the rest of the page as comment would keep that script in the
    digest."""
    data = b"<!-- x --!><script>t()</script><p>A</p>"

    assert strip_non_text(data) == b"<!-- x --!><p>A</p>"


def test_a_bang_directly_after_the_opener_does_not_close_the_comment() -> None:
    """An opener followed by "!>" is not a self-closing form; the comment runs on."""
    data = b"<!--!> <script>x()</script> --><p>A</p>"

    assert strip_non_text(data) == data


def test_an_unclosed_comment_runs_to_the_end() -> None:
    data = b"<p>A</p><!-- never closed <script>x()</script>"

    assert strip_non_text(data) == data


def test_a_script_named_in_an_attribute_value_is_not_an_element() -> None:
    data = b'<a title="<script>x()</script>">A</a>'

    assert strip_non_text(data) == data


def test_a_stray_end_tag_removes_nothing() -> None:
    """Fragments are not guaranteed balanced; an unmatched "</script>" opens nothing."""
    data = b"</script><p>Criterion after a stray tag.</p>"

    assert strip_non_text(data) == data


@pytest.mark.parametrize(
    "tag",
    [b"<scripts>x</scripts>", b"<script-loader>x</script-loader>", b"<styles>x</styles>"],
)
def test_a_longer_name_is_a_different_element(tag: bytes) -> None:
    assert strip_non_text(tag) == tag


def test_a_longer_end_tag_does_not_close_a_script() -> None:
    data = b"<script>a</scripts>b</script><p>A</p>"

    assert strip_non_text(data) == b"<p>A</p>"


def test_noscript_is_kept() -> None:
    """Outside the definition on purpose — see the module docstring. If its
    content ever varies between fetches, the digest moves and the nightly check
    against Aetna reports it."""
    data = b"<noscript><img src=pixel.gif></noscript><p>A</p>"

    assert strip_non_text(data) == data


def test_attributes_and_event_handlers_are_kept() -> None:
    data = b'<body onload="init(42)" class="cpb"><p>A</p></body>'

    assert strip_non_text(data) == data


@pytest.mark.parametrize(
    "data",
    [
        b"<!DOCTYPE html><p>A</p>",
        b'<?xml version="1.0"?><p>A</p>',
        b"<![CDATA[ x ]]><p>A</p>",
        b"<p>a < b and b > c</p>",
        b"<p>A</p><",
        b"",
    ],
)
def test_markup_that_is_not_an_element_is_kept(data: bytes) -> None:
    assert strip_non_text(data) == data


def test_non_ascii_bytes_outside_an_element_are_kept_exactly() -> None:
    """The scanner reads bytes, and UTF-8 and cp1252 must both pass through."""
    utf8 = "<p>the payer’s criteria</p><script>x()</script>".encode()
    cp1252 = "<p>the payer’s criteria</p><script>x()</script>".encode("cp1252")

    assert strip_non_text(utf8) == "<p>the payer’s criteria</p>".encode()
    assert strip_non_text(cp1252) == "<p>the payer’s criteria</p>".encode("cp1252")


# --- the spans themselves --------------------------------------------------


def test_spans_cover_exactly_each_element() -> None:
    data = b"<p>A</p><script>x()</script><p>B</p><style>b{}</style>"

    spans = list(non_text_spans(data))

    assert [data[start:end] for start, end in spans] == [
        b"<script>x()</script>",
        b"<style>b{}</style>",
    ]


def test_the_definition_is_script_and_style() -> None:
    """Text extraction imports this set, so widening it widens both at once."""
    assert frozenset({"script", "style"}) == NON_TEXT_ELEMENTS


# --- the digest ------------------------------------------------------------


def test_the_digest_is_sha256_over_the_stripped_bytes() -> None:
    data = b"<p>A</p><script>x()</script>"

    assert html_digest(data) == hashlib.sha256(b"<p>A</p>").hexdigest()


def test_script_free_html_digests_to_the_sha256_of_its_published_bytes() -> None:
    data = b"<p>Conservative therapy is defined as six weeks of home exercise.</p>"

    assert html_digest(data) == hashlib.sha256(data).hexdigest()


def test_documents_differing_only_inside_a_script_are_one_document() -> None:
    first = b'<p>Criteria.</p><script>token="a1b2"</script>'
    second = b'<p>Criteria.</p><script>token="c3d4"</script>'

    assert html_digest(first) == html_digest(second)


def test_documents_differing_in_policy_prose_remain_two() -> None:
    first = b"<p>Six weeks of therapy.</p><script>t=1</script>"
    second = b"<p>Twelve weeks of therapy.</p><script>t=1</script>"

    assert html_digest(first) != html_digest(second)


# --- the real pages this was written for -----------------------------------


def test_the_two_real_aetna_fetches_differ_as_published() -> None:
    """If this ever fails, the fixtures stopped reproducing the bug and every
    test below would pass for the wrong reason."""
    first, second = AETNA_FIRST_FETCH.read_bytes(), AETNA_SECOND_FETCH.read_bytes()

    assert len(first) == len(second)
    assert hashlib.sha256(first).digest() != hashlib.sha256(second).digest()


def test_the_two_real_aetna_fetches_digest_the_same() -> None:
    assert html_digest(AETNA_FIRST_FETCH.read_bytes()) == html_digest(
        AETNA_SECOND_FETCH.read_bytes()
    )


def test_every_difference_between_the_real_fetches_lies_inside_a_script() -> None:
    """The cause TASK-009 names, checked against the capture rather than assumed."""
    first, second = AETNA_FIRST_FETCH.read_bytes(), AETNA_SECOND_FETCH.read_bytes()
    differing = [index for index, (a, b) in enumerate(zip(first, second, strict=True)) if a != b]
    scripts = [
        (start, end)
        for start, end in non_text_spans(first)
        if first[start : start + 7].lower() == b"<script"
    ]

    assert differing
    assert all(any(start <= i < end for start, end in scripts) for i in differing)


def test_nothing_but_script_and_style_is_removed_from_a_real_page() -> None:
    page = AETNA_FIRST_FETCH.read_bytes()
    stripped = strip_non_text(page)

    assert list(non_text_spans(stripped)) == []
    # The page's policy content and its other markup survive untouched.
    assert b"<noscript" in stripped.lower()
    assert b"Risankizumab" in stripped
    removed = sum(end - start for start, end in non_text_spans(page))
    assert len(stripped) == len(page) - removed


def test_a_real_commented_out_script_is_kept() -> None:
    """Aetna's template comments out one script tag. It is not an element — a
    browser never runs it and the stdlib parser reports it as a comment — so it
    stays in the digest like any other comment."""
    stripped = strip_non_text(AETNA_FIRST_FETCH.read_bytes())

    assert b'<!--  <script type="text/javascript"' in stripped

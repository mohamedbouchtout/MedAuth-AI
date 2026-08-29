"""CLAUDE.md's audit action vocabulary must match the constants the services declare.

That table is declared authoritative, and it has drifted from the code three
times, in both directions: ``WRITE_NOTE`` was cited by a task while no service
defined it, ``QUERY_POLICY`` shipped in ``track_b_rag/audit.py`` while the table
had never carried it, and ``STREAM_AUDIO`` did the same from
``audio-ingestion/src/audit.py``. Each was found by someone working on something
else, which is three pieces of evidence that reading carefully is not a working
control. This is TASK-045: the check, rather than a fourth correction.

**What it checks and what it cannot.** It checks that two lists of names agree.
It cannot check that a row's *meaning* is right, or that a service audits when
it should — Known Constraints #6 is a judgement and stays one. Names are the
part that has actually failed.

**Why the constants are parsed rather than imported.** Several services still
install a top-level module named ``src`` into the shared virtualenv (see
CLAUDE.md, "Where the shared SQLAlchemy models live"), so ``src.audit`` resolves
to whichever service sorts first and audio-ingestion's constants and
nudge-service's could not both be imported in one process. ``ast`` reads each
file where it lives, needs no service dependency installed, and cannot be
confused by that shadowing.

**The carve-out for unbuilt work.** The table legitimately carries actions for
services that do not exist yet — ``READ_PATIENT`` (Phase 5),
``SUBMIT_PRIOR_AUTH`` (TASK-061). Those are exempt from the reverse direction,
keyed on the row's own "Written by" column naming a service with no ``audit.py``
yet rather than on a hand-maintained ignore list, which would be one more thing
to drift. The exemption is per service, not per row: ``READ_NUDGE`` names both
prior-auth (unbuilt) and track-b-rag (built), and exempting the whole row would
lose the check on the half that ships.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
SERVICES_DIR = REPO_ROOT / "services"

#: The table is found by its header rather than by line number, and this exact
#: string appears once in the document.
TABLE_HEADER = "| Action | Written by | Meaning |"

#: A "Written by" cell is one or more ``service-name (TASK-0xx)`` entries
#: separated by commas. Only the service name matters here.
_SERVICE_IN_CELL = re.compile(r"^([a-z0-9-]+)")


@dataclass(frozen=True)
class VocabularyRow:
    """One row of the action vocabulary table."""

    action: str
    services: tuple[str, ...]


def parse_vocabulary_table(markdown: str) -> tuple[VocabularyRow, ...]:
    """Parse the action vocabulary table out of CLAUDE.md's text."""
    lines = markdown.splitlines()
    if TABLE_HEADER not in lines:
        raise AssertionError(
            f"CLAUDE.md has no line {TABLE_HEADER!r}. The action vocabulary table is "
            "found by that header — if it moved or was reformatted, fix this test "
            "deliberately rather than letting the check quietly stop running."
        )
    start = lines.index(TABLE_HEADER) + 2  # skip the header and its |---| separator

    rows: list[VocabularyRow] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        # Only the first two columns are read. "Meaning" is prose, and is not
        # something two lists of names can be compared on.
        cells = line.strip().strip("|").split("|")
        action = cells[0].strip().strip("`")
        services = tuple(
            match.group(1)
            for piece in cells[1].split(",")
            if (match := _SERVICE_IN_CELL.match(piece.strip()))
        )
        rows.append(VocabularyRow(action=action, services=services))
    return tuple(rows)


def action_values(source: str) -> frozenset[str]:
    """Return the string values of every module-level ``ACTION_*`` constant.

    Both declaration styles in the tree count: ``ACTION_X = "X"`` in
    track-a-clinical and ``ACTION_X: Final = "X"`` everywhere else. The *value*
    is what is collected, not the constant's name — the value is what reaches
    the ``audit_log.action`` column, and the value is what the table lists.
    """
    values: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign):
            targets: list[ast.expr] = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.startswith("ACTION_"):
                values.add(node.value.value)
    return frozenset(values)


def service_directories(services_dir: Path = SERVICES_DIR) -> frozenset[str]:
    """Every directory under ``services/``, whether or not it audits yet."""
    return frozenset(path.name for path in services_dir.iterdir() if path.is_dir())


def collect_declared_actions(services_dir: Path = SERVICES_DIR) -> dict[str, frozenset[str]]:
    """Map each service that has an ``audit.py`` to the action values it declares.

    A service with no ``audit.py`` is absent from the mapping entirely, which is
    what makes it exempt from the reverse check. A service *with* one that
    declares nothing maps to an empty set and is not exempt — it has an audit
    surface, so a table row naming it is a real disagreement.
    """
    declared: dict[str, frozenset[str]] = {}
    for path in sorted(services_dir.iterdir()):
        if not path.is_dir():
            continue
        sources = [p for p in sorted(path.rglob("audit.py")) if "tests" not in p.parts]
        if not sources:
            continue
        values: set[str] = set()
        for source in sources:
            values |= action_values(source.read_text(encoding="utf-8"))
        declared[path.name] = frozenset(values)
    return declared


def find_disagreements(
    rows: tuple[VocabularyRow, ...],
    declared: dict[str, frozenset[str]],
    known_services: frozenset[str],
) -> list[str]:
    """Compare the table against the code in both directions.

    Returns one message per disagreement, empty when the two agree.
    """
    failures: list[str] = []

    listed: dict[str, set[str]] = {}
    for row in rows:
        for service in row.services:
            listed.setdefault(service, set()).add(row.action)

    # Forward: a constant a service declares must appear in the table, against
    # that service's name. This is the direction QUERY_POLICY and STREAM_AUDIO
    # both failed in — shipped in code, never added to the list.
    for service in sorted(declared):
        for action in sorted(declared[service] - listed.get(service, set())):
            failures.append(
                f"{service} declares the audit action {action!r}, which CLAUDE.md's "
                f"action vocabulary table does not list against {service}. Add the row "
                "in the same change, per that table's own rule."
            )

    # Reverse: a row naming a service that audits must match a constant that
    # service declares. This is the direction WRITE_NOTE failed in — listed and
    # cited while nothing defined it.
    for row in rows:
        for service in row.services:
            if service not in known_services:
                failures.append(
                    f"CLAUDE.md lists {row.action!r} as written by {service!r}, which is "
                    "not a directory under services/. A misspelled service name is "
                    "exempted by the unbuilt-service carve-out and would silently "
                    "disable the check for that row."
                )
            elif service not in declared:
                continue  # Unbuilt: no audit.py yet, so nothing to disagree with.
            elif row.action not in declared[service]:
                failures.append(
                    f"CLAUDE.md lists {row.action!r} as written by {service}, but that "
                    "service's audit.py declares no such constant. Either the service "
                    "dropped it or the row was written ahead of the code."
                )
    return failures


def test_table_and_services_agree() -> None:
    """The real repository: CLAUDE.md's table and the services' constants match."""
    failures = find_disagreements(
        parse_vocabulary_table(CLAUDE_MD.read_text(encoding="utf-8")),
        collect_declared_actions(),
        service_directories(),
    )
    assert not failures, "\n".join(failures)


def test_table_parses_into_rows_and_splits_multi_service_cells() -> None:
    """Guard the parser against the real document, not only its own fixtures.

    A parser that silently returned nothing would make the check above pass by
    comparing two empty lists.
    """
    rows = parse_vocabulary_table(CLAUDE_MD.read_text(encoding="utf-8"))
    by_action = {row.action: row.services for row in rows}

    assert len(rows) >= 15
    assert by_action["START_SESSION"] == ("track-a-clinical",)
    # The row that forced the per-service carve-out: one built service, one not.
    assert by_action["READ_NUDGE"] == ("prior-auth", "track-b-rag")


def test_service_name_constant_matches_its_directory() -> None:
    """The name a service writes into ``audit_log.service_name`` is the name the
    table's "Written by" column uses. If those two diverged, this whole
    comparison would be matching the table against something else.
    """
    for path in sorted(p for p in SERVICES_DIR.iterdir() if p.is_dir()):
        for source in sorted(path.rglob("audit.py")):
            if "tests" in source.parts:
                continue
            for node in ast.parse(source.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.AnnAssign):
                    target: ast.expr | None = node.target
                elif isinstance(node, ast.Assign) and node.targets:
                    target = node.targets[0]
                else:
                    continue
                if isinstance(target, ast.Name) and target.id == "SERVICE_NAME":
                    assert isinstance(node.value, ast.Constant)
                    assert node.value.value == path.name, (
                        f"{source} writes service_name={node.value.value!r} but lives in "
                        f"services/{path.name}"
                    )


def test_both_declaration_styles_are_read() -> None:
    """``ACTION_X = "X"`` and ``ACTION_X: Final = "X"`` are both in the tree."""
    source = "\n".join(
        [
            "from typing import Final",
            'ACTION_PLAIN = "PLAIN"',
            'ACTION_ANNOTATED: Final = "ANNOTATED"',
            'SERVICE_NAME: Final = "not-an-action"',
            'OTHER = "NOT_COLLECTED"',
            "def f():",
            '    ACTION_LOCAL = "NOT_MODULE_LEVEL"',
            "    return ACTION_LOCAL",
        ]
    )
    assert action_values(source) == {"PLAIN", "ANNOTATED"}


def test_a_service_constant_absent_from_the_table_fails() -> None:
    rows = (VocabularyRow("READ_ENCOUNTER", ("track-a-clinical",)),)
    failures = find_disagreements(
        rows,
        {"track-a-clinical": frozenset({"READ_ENCOUNTER", "INVENTED_ACTION"})},
        frozenset({"track-a-clinical"}),
    )
    assert len(failures) == 1
    assert "INVENTED_ACTION" in failures[0]


def test_a_row_for_a_service_that_audits_without_that_constant_fails() -> None:
    rows = (VocabularyRow("NEVER_DECLARED", ("track-b-rag",)),)
    failures = find_disagreements(
        rows,
        {"track-b-rag": frozenset({"QUERY_POLICY"})},
        frozenset({"track-b-rag"}),
    )
    # Both directions fire here, and that is correct: the table names an action
    # the service does not declare, and the service declares one the table does
    # not list.
    assert any("NEVER_DECLARED" in failure for failure in failures)
    assert any("QUERY_POLICY" in failure for failure in failures)


def test_a_row_for_an_unbuilt_service_passes() -> None:
    """prior-auth is a directory with no ``audit.py``, so ``SUBMIT_PRIOR_AUTH``
    and the prior-auth half of ``READ_NUDGE`` are exempt while track-b-rag's
    half of that same row is still checked.
    """
    rows = (
        VocabularyRow("SUBMIT_PRIOR_AUTH", ("prior-auth",)),
        VocabularyRow("READ_NUDGE", ("prior-auth", "track-b-rag")),
    )
    failures = find_disagreements(
        rows,
        {"track-b-rag": frozenset({"READ_NUDGE"})},
        frozenset({"prior-auth", "track-b-rag"}),
    )
    assert failures == []


def test_a_row_naming_no_service_directory_fails() -> None:
    """A misspelled service name would otherwise look exactly like unbuilt work."""
    rows = (VocabularyRow("READ_PATIENT", ("fhir-integrations",)),)
    failures = find_disagreements(rows, {}, frozenset({"fhir-integration"}))
    assert len(failures) == 1
    assert "not a directory under services/" in failures[0]

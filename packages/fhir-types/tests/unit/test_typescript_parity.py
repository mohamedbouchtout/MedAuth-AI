"""The TypeScript mirrors must not drift from the Pydantic models.

Two hand-written definitions of the same seven resources will diverge — someone
adds an element on the Python side for the prior-auth bundle and no one touches
``typescript/src/``, and the drift only surfaces months later as a screen that
silently renders nothing. This module is what makes that a CI failure instead.

It compares two things:

* **Element names.** Every model's serialization aliases against its interface's
  properties, resolving ``extends`` so inherited resource header fields count.
* **Closed value sets.** Every ``Literal`` in ``codes.py`` against the same-named
  string-literal union in ``codes.ts``, member for member. A code added to one
  side only is the drift most likely to reach a payer as a rejected request.

Types are deliberately *not* compared. ``string`` versus ``str`` is a mapping this
test would have to reimplement, and ``tsc --noEmit`` already proves the TypeScript
side is internally consistent. Names and code sets are where drift actually hides.

The parser is a regex, not a TypeScript compiler, which puts three requirements on
``typescript/src/``: one property per line, no inline object literal types (give a
nested shape its own interface), and a closing brace on its own line. Those are
documented in ``typescript/src/base.ts`` as well. If a file stops matching, the
mirror is unparsed rather than silently passing — ``test_every_interface_is_parsed``
catches that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, get_args, get_origin

import pytest
from pydantic import BaseModel

import fhir_types
from fhir_types import codes

TYPESCRIPT_SRC = Path(__file__).resolve().parents[2] / "typescript" / "src"

_INTERFACE_RE = re.compile(
    r"^export interface (?P<name>\w+)"
    r"(?:\s+extends\s+(?P<parents>[\w\s,]+?))?"
    r"\s*\{(?P<body>[^}]*)\}",
    re.MULTILINE,
)
_PROPERTY_RE = re.compile(r"^\s*readonly\s+(?P<name>\w+)\??\s*:", re.MULTILINE)
_TYPE_ALIAS_RE = re.compile(r"^export type (?P<name>\w+)\s*=(?P<body>[^;]*);", re.MULTILINE)
_STRING_LITERAL_RE = re.compile(r"'([^']*)'")


def _typescript_sources() -> dict[str, str]:
    """Every mirror file, keyed by file name."""
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(TYPESCRIPT_SRC.glob("*.ts"))
    }


def _interfaces() -> dict[str, tuple[frozenset[str], tuple[str, ...]]]:
    """Interface name -> (its own properties, the names it extends)."""
    found: dict[str, tuple[frozenset[str], tuple[str, ...]]] = {}
    for source in _typescript_sources().values():
        for match in _INTERFACE_RE.finditer(source):
            properties = frozenset(m.group("name") for m in _PROPERTY_RE.finditer(match["body"]))
            raw_parents = match["parents"] or ""
            parents = tuple(p.strip() for p in raw_parents.split(",") if p.strip())
            found[match["name"]] = (properties, parents)
    return found


def _resolved_properties(
    name: str, interfaces: dict[str, tuple[frozenset[str], tuple[str, ...]]]
) -> set[str]:
    """An interface's properties including everything it inherits."""
    own, parents = interfaces[name]
    resolved = set(own)
    for parent in parents:
        resolved |= _resolved_properties(parent, interfaces)
    return resolved


def _typescript_code_sets() -> dict[str, set[str]]:
    """Type alias name -> its string-literal members, from codes.ts only."""
    source = _typescript_sources()["codes.ts"]
    return {
        match["name"]: set(_STRING_LITERAL_RE.findall(match["body"]))
        for match in _TYPE_ALIAS_RE.finditer(source)
    }


def _python_models() -> dict[str, type[BaseModel]]:
    """Every exported model that has fields.

    ``FHIRBase`` is excluded by the field count: it carries configuration only, and
    an empty interface would be noise on the TypeScript side.
    """
    models: dict[str, type[BaseModel]] = {}
    for name in fhir_types.__all__:
        obj = getattr(fhir_types, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj.model_fields:
            models[name] = obj
    return models


def _python_code_sets() -> dict[str, set[str]]:
    """Every ``Literal`` alias declared in codes.py."""
    return {
        name: set(get_args(obj)) for name, obj in vars(codes).items() if get_origin(obj) is Literal
    }


def _serialized_names(model: type[BaseModel]) -> set[str]:
    """The element names a model produces with ``by_alias=True``.

    A self-recursive model — Identifier holds a Reference, which holds an
    Identifier — gets a schema whose root is a ``$ref`` into ``$defs`` rather than
    an inline object, so the reference is followed before reading properties.
    """
    schema = model.model_json_schema(by_alias=True)
    if "$ref" in schema:
        schema = schema["$defs"][schema["$ref"].rsplit("/", 1)[-1]]
    return set(schema["properties"])


MODELS = _python_models()
INTERFACES = _interfaces()
PYTHON_CODE_SETS = _python_code_sets()
TYPESCRIPT_CODE_SETS = _typescript_code_sets()


def test_every_interface_is_parsed() -> None:
    """A file that stops matching the regex would make every other test vacuous."""
    declared = sum(source.count("export interface ") for source in _typescript_sources().values())

    assert declared == len(INTERFACES), (
        "an interface was declared but not parsed — check for an inline object "
        "literal type or a property spread across lines"
    )


def test_every_model_has_an_interface() -> None:
    assert set(MODELS) <= set(INTERFACES), (
        f"no TypeScript mirror for: {sorted(set(MODELS) - set(INTERFACES))}"
    )


def test_every_interface_has_a_model() -> None:
    assert set(INTERFACES) <= set(MODELS), (
        f"TypeScript interface with no Pydantic model: {sorted(set(INTERFACES) - set(MODELS))}"
    )


@pytest.mark.parametrize("name", sorted(MODELS))
def test_element_names_match(name: str) -> None:
    python_names = _serialized_names(MODELS[name])
    typescript_names = _resolved_properties(name, INTERFACES)

    assert python_names == typescript_names, (
        f"{name} has drifted — only in Python: {sorted(python_names - typescript_names)}; "
        f"only in TypeScript: {sorted(typescript_names - python_names)}"
    )


def test_every_code_set_is_mirrored() -> None:
    assert set(PYTHON_CODE_SETS) == set(TYPESCRIPT_CODE_SETS), (
        f"only in codes.py: {sorted(set(PYTHON_CODE_SETS) - set(TYPESCRIPT_CODE_SETS))}; "
        f"only in codes.ts: {sorted(set(TYPESCRIPT_CODE_SETS) - set(PYTHON_CODE_SETS))}"
    )


@pytest.mark.parametrize("name", sorted(PYTHON_CODE_SETS))
def test_code_set_members_match(name: str) -> None:
    python_members = PYTHON_CODE_SETS[name]
    typescript_members = TYPESCRIPT_CODE_SETS.get(name, set())

    assert python_members == typescript_members, (
        f"{name} has drifted — only in codes.py: {sorted(python_members - typescript_members)}; "
        f"only in codes.ts: {sorted(typescript_members - python_members)}"
    )


def test_resource_type_is_a_property_on_every_resource() -> None:
    """The discriminator both sides narrow on. Missing it breaks ``AnyResource``."""
    resources = [name for name in MODELS if "resourceType" in _serialized_names(MODELS[name])]

    assert sorted(resources) == [
        "Claim",
        "Condition",
        "Coverage",
        "DocumentReference",
        "Encounter",
        "MedicationRequest",
        "Patient",
    ]

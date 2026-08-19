"""The envelope's two shapes, and the rule that a response never has both."""

from __future__ import annotations

from pydantic import BaseModel

from api_envelope import ApiError, ApiResponse


class Thing(BaseModel):
    name: str


def test_a_success_serialises_with_a_null_error() -> None:
    """`error` is present and null, not absent — clients read the key."""
    payload = ApiResponse[Thing](data=Thing(name="knee MRI"))

    assert payload.model_dump() == {"data": {"name": "knee MRI"}, "error": None}


def test_a_failure_serialises_with_null_data() -> None:
    payload = ApiResponse[None](error=ApiError(code="not_found", message="No such session"))

    assert payload.model_dump() == {
        "data": None,
        "error": {"code": "not_found", "message": "No such session"},
    }


def test_an_empty_envelope_is_both_null() -> None:
    assert ApiResponse[Thing]().model_dump() == {"data": None, "error": None}


def test_the_data_type_is_enforced() -> None:
    """ApiResponse[Thing] validates its payload rather than passing anything through."""
    payload = ApiResponse[Thing].model_validate({"data": {"name": "x"}, "error": None})

    assert isinstance(payload.data, Thing)


def test_different_parameterisations_are_distinct_models() -> None:
    """Each service parameterises this per route; they must not share a schema."""
    assert ApiResponse[Thing] is not ApiResponse[None]

"""The mapped classes describe the schema TASK-005 specifies.

These assertions read against ``Base.metadata`` and need no database. They are
the guard on the models themselves; ``tests/integration/test_migrations.py``
separately proves the migration produces the same shape in PostgreSQL.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from track_a_clinical.models import (
    Base,
    ClinicalNote,
    ClinicalNudge,
    Encounter,
    InsurancePolicy,
    PriorAuthRequest,
)

EXPECTED_TABLES = {
    "encounters",
    "clinical_notes",
    "clinical_nudges",
    "prior_auth_requests",
    "insurance_policies",
}

# Straight from TASK-005's inline SQL, in declaration order.
EXPECTED_COLUMNS = {
    "encounters": [
        "id",
        "session_id",
        "ehr_encounter_id",
        "patient_fhir_id",
        "provider_id",
        "organization_id",
        "status",
        "started_at",
        "ended_at",
        "insurance_payer",
        "insurance_plan_type",
        "insurance_member_id",
        "state",
        "deleted_at",
    ],
    "clinical_notes": [
        "id",
        "encounter_id",
        "soap_subjective",
        "soap_objective",
        "soap_assessment",
        "soap_plan",
        "icd10_codes",
        "cpt_codes",
        "ehr_document_ref_id",
        "generated_at",
        "reviewed_by_provider",
        "provider_edited",
        "deleted_at",
    ],
    "clinical_nudges": [
        "id",
        "encounter_id",
        "procedure_name",
        "cpt_code",
        "nudge_message",
        "missing_criteria",
        "denial_risk",
        "payer_policy_source",
        "fired_at",
        "acknowledged",
        "acknowledged_at",
        "resulted_in_documentation",
    ],
    "prior_auth_requests": [
        "id",
        "encounter_id",
        "status",
        "payer_name",
        "procedures",
        "diagnoses",
        "clinical_evidence",
        "submission_method",
        "payer_reference_number",
        "submitted_at",
        "decided_at",
        "denial_reason",
    ],
    "insurance_policies": [
        "id",
        "payer",
        "plan_type",
        "state",
        "jurisdiction_states",
        "policy_id",
        "source_url",
        "content_hash",
        "last_ingested_at",
        "effective_date",
        "qdrant_collection",
    ],
}

EXPECTED_INDEXES = {
    "idx_encounters_session",
    "idx_encounters_provider",
    "idx_clinical_notes_encounter",
    "idx_clinical_nudges_encounter",
    "idx_prior_auth_encounter",
    "idx_prior_auth_status",
    "idx_insurance_policies_payer_state",
}


def test_metadata_holds_exactly_the_five_core_tables() -> None:
    """audit_log is absent on purpose — hipaa-logger owns it and migrates it first."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_every_table_has_the_specified_columns() -> None:
    for table_name, expected in EXPECTED_COLUMNS.items():
        table = Base.metadata.tables[table_name]
        assert [column.name for column in table.columns] == expected, table_name


def test_every_specified_index_is_declared() -> None:
    declared = {index.name for table in Base.metadata.tables.values() for index in table.indexes}
    assert declared == EXPECTED_INDEXES


def test_ids_are_generated_server_side() -> None:
    """CLAUDE.md: UUIDs come from gen_random_uuid(), never from the client."""
    for table in Base.metadata.tables.values():
        default = table.columns["id"].server_default
        assert default is not None, table.name
        assert "gen_random_uuid()" in str(default.arg), table.name


def test_session_id_is_unique_and_not_null() -> None:
    """One encounter per session — every Redis channel is keyed by session_id."""
    column = Encounter.__table__.columns["session_id"]
    assert column.nullable is False
    constraints = {
        constraint.name
        for constraint in Encounter.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_encounters_session_id" in constraints


def test_policy_id_is_unique() -> None:
    """A re-scrape updates the existing policy row rather than duplicating it."""
    constraints = {
        constraint.name
        for constraint in InsurancePolicy.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert "uq_insurance_policies_policy_id" in constraints


def test_child_tables_reference_encounters() -> None:
    for model in (ClinicalNote, ClinicalNudge, PriorAuthRequest):
        column = model.__table__.columns["encounter_id"]
        assert column.nullable is False, model.__tablename__
        targets = {foreign_key.target_fullname for foreign_key in column.foreign_keys}
        assert targets == {"encounters.id"}, model.__tablename__


def test_insurance_policies_stands_alone() -> None:
    """It holds public payer documents, not encounter data — no foreign keys."""
    assert InsurancePolicy.__table__.foreign_keys == set()


def test_encounter_state_is_a_nullable_usps_code() -> None:
    """CHAR(2) and nullable, matching insurance_policies.state.

    The two columns are compared to each other, so a width or a nullability that
    disagreed would either truncate a code on one side or force a value nothing
    can supply on the other. Null means "not known yet" — TASK-052b is what
    fills it, and until then every encounter has it unset.
    """
    column = Encounter.__table__.columns["state"]
    assert isinstance(column.type, sa.CHAR)
    assert column.type.length == 2
    assert column.nullable

    policy_state = InsurancePolicy.__table__.columns["state"]
    assert column.type.length == policy_state.type.length


def test_soft_delete_only_where_rows_may_be_retired() -> None:
    """Nudges and prior-auth submissions are records of what happened, so they
    carry no deleted_at — the same reasoning that keeps audit_log append-only."""
    assert "deleted_at" in Encounter.__table__.columns
    assert "deleted_at" in ClinicalNote.__table__.columns
    assert "deleted_at" not in ClinicalNudge.__table__.columns
    assert "deleted_at" not in PriorAuthRequest.__table__.columns


def test_status_columns_default_to_their_opening_state() -> None:
    assert "'active'" in str(Encounter.__table__.columns["status"].server_default.arg)
    assert "'pending'" in str(PriorAuthRequest.__table__.columns["status"].server_default.arg)


def test_json_columns_are_jsonb_not_json() -> None:
    """JSONB so the columns stay queryable; plain JSON would store raw text."""
    json_columns = {
        "clinical_notes": ("icd10_codes", "cpt_codes"),
        "clinical_nudges": ("missing_criteria",),
        "prior_auth_requests": ("procedures", "diagnoses", "clinical_evidence"),
    }
    for table_name, columns in json_columns.items():
        table = Base.metadata.tables[table_name]
        for column_name in columns:
            assert isinstance(table.columns[column_name].type, postgresql.JSONB), column_name


def test_timestamps_are_timezone_aware() -> None:
    """CLAUDE.md: all timestamps are ISO 8601 UTC, so every column is TIMESTAMPTZ."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.TIMESTAMP):
                assert column.type.timezone is True, f"{table.name}.{column.name}"


def test_relationships_resolve_from_the_package_import() -> None:
    """Importing the package registers every model, so string targets resolve."""
    assert Encounter.notes.property.mapper.class_ is ClinicalNote
    assert Encounter.nudges.property.mapper.class_ is ClinicalNudge
    assert Encounter.prior_auth_requests.property.mapper.class_ is PriorAuthRequest
    assert ClinicalNote.encounter.property.mapper.class_ is Encounter

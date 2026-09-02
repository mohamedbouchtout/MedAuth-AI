"""The audit action vocabulary — one definition, imported by every service.

``audit_log.action`` is a ``VARCHAR(100)``: the database constrains nothing, and
the value's whole purpose is that every service spells it the same way. "Who
accessed patient X" is only a query you can run if the answer does not depend on
knowing that one service wrote ``QUERY_POLICY`` and another wrote
``POLICY_QUERY``.

That vocabulary used to live in a table in ``CLAUDE.md`` while each service
declared its own string literals against it, and the two drifted three times, in
both directions: ``WRITE_NOTE`` was cited by a task while no service defined it,
``QUERY_POLICY`` shipped in ``track_b_rag/audit.py`` while the list had never
carried it, and ``STREAM_AUDIO`` did the same from ``audio-ingestion``. Each was
found by someone working on something else.

This module is the fix, and it is a different shape from the one TASK-045
originally proposed. A test comparing the document against the code would have
detected a fourth instance; defining the vocabulary once, here, means a service
cannot write an action that is not in it — mypy rejects the call, and
``audit_log`` rejects it again at runtime for callers mypy does not cover.

It lives in this package because this package owns the ``audit_log`` table and
its migration, and ``action`` is a column of that table. Every service that
audits already depends on ``hipaa-logger``, so nothing new is coupled by it.

**Adding an action.** Add the member here, in the same change as the code that
writes it. Members for work that is not built yet are fine and expected —
``READ_PATIENT`` waits on Phase 5, ``SUBMIT_PRIOR_AUTH`` on TASK-061 — because
an unused member is inert, unlike a documented row with nothing behind it.

**Which service writes which action is deliberately not recorded here.** It was
a column of the old table and it was the half that rotted fastest. With one
symbol per action it is a grep — ``grep -rn "AuditAction.READ_NUDGE"`` — which
cannot go stale.
"""

from __future__ import annotations

from enum import StrEnum


class AuditAction(StrEnum):
    """What an audit row records having happened.

    ``StrEnum``, so a member compares equal to its own text and reaches asyncpg
    as an ordinary string. The type exists to stop a *new* spelling being
    invented, not to change how a value is stored or read back.
    """

    #: An encounter was opened.
    START_SESSION = "START_SESSION"

    #: An encounter was closed.
    END_SESSION = "END_SESSION"

    #: An ``encounters`` row was read. Also what an idempotent repeat of a state
    #: transition records — a call that read a row it did not move.
    READ_ENCOUNTER = "READ_ENCOUNTER"

    #: A session's token was refreshed (TASK-006b). Kept distinct from
    #: START_SESSION so an audit can tell "a visit was opened" from "a visit's
    #: token was refreshed" — different events with the same actor.
    REMINT_SESSION_TOKEN = "REMINT_SESSION_TOKEN"

    #: Encounter audio was streamed through transcription under one session.
    STREAM_AUDIO = "STREAM_AUDIO"

    #: A SOAP note was generated and stored.
    WRITE_NOTE = "WRITE_NOTE"

    #: A stored note was written out to an EHR as a ``DocumentReference``
    #: (TASK-053). Deliberately distinct from WRITE_NOTE, which means the note
    #: was generated and stored *here*: putting a note onto a patient's chart is
    #: a different event with different consequences, and collapsing the two
    #: would make "was this note ever sent to the EHR" unanswerable from the
    #: audit trail. One write-back writes this action twice — once by
    #: ``fhir-integration``, which sent the note, and once by
    #: ``track-a-clinical``, whose row was mutated to carry the resulting
    #: document id. Two services, two accesses, told apart by ``service_name``.
    WRITE_NOTE_TO_EHR = "WRITE_NOTE_TO_EHR"

    #: A ``clinical_notes`` row was read.
    READ_NOTE = "READ_NOTE"

    #: A provider edited a stored note.
    UPDATE_NOTE = "UPDATE_NOTE"

    #: An encounter's clinical context was read to answer a policy query.
    QUERY_POLICY = "QUERY_POLICY"

    #: A nudge was raised and stored against an encounter.
    WRITE_NUDGE = "WRITE_NUDGE"

    #: An encounter's nudge stream was opened to a client.
    RELAY_NUDGES = "RELAY_NUDGES"

    #: A provider dismissed a nudge, and the row changed.
    ACKNOWLEDGE_NUDGE = "ACKNOWLEDGE_NUDGE"

    #: An encounter's ``clinical_nudges`` rows were read — including a repeat
    #: acknowledge, which reads a row it does not change.
    READ_NUDGE = "READ_NUDGE"

    #: A prior-auth bundle was assembled and stored. Not written yet (TASK-060).
    WRITE_PRIOR_AUTH = "WRITE_PRIOR_AUTH"

    #: A bundle was transmitted to a payer. Not written yet (TASK-061).
    SUBMIT_PRIOR_AUTH = "SUBMIT_PRIOR_AUTH"

    #: Patient context was read from an EHR. Not written yet (Phase 5).
    READ_PATIENT = "READ_PATIENT"

"""Insurance policy RAG — Qdrant retrieval plus Claude on Bedrock.

Shipped as a named package rather than a bare ``src`` module. Every other
service in this workspace still installs a top-level ``src``, so they shadow
each other in the shared venv; this service moved out of that arrangement in
TASK-010 because TASK-011 imports ``track_a_clinical.models`` to write the
``insurance_policies`` row, and an ambiguous ``src`` breaks cross-service
imports in both directions.
"""

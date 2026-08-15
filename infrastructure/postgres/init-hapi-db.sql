-- Runs once on first postgres container start (docker-entrypoint-initdb.d).
-- The HAPI FHIR dev server keeps its own database, separate from the MedAuth
-- application schema so Alembic migrations never touch HAPI's tables.
CREATE DATABASE hapi;

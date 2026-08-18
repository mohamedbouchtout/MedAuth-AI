#!/usr/bin/env bash
#
# Apply every Alembic history in the monorepo, in dependency order.
#
#   DATABASE_URL=postgresql+asyncpg://medauth:medauth_local_dev@localhost:5432/medauth \
#     ./scripts/init-db.sh
#
# Order matters. packages/hipaa-logger owns audit_log and every service depends
# on it, so it can never wait on a service-owned schema — it goes first.
# services/track-a-clinical owns the core clinical schema and goes second.
#
# Each history writes its own namespaced alembic_version table, so running this
# repeatedly is safe: an already-current history is a no-op.
#
# Add new entries to MIGRATION_ROOTS as packages and services grow their own
# Alembic setups.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MIGRATION_ROOTS=(
  "packages/hipaa-logger"
  "services/track-a-clinical"
)

if [ -z "${DATABASE_URL:-}" ]; then
  echo "error: DATABASE_URL is not set." >&2
  echo "       Copy .env.example to .env.local and export it, or pass it inline." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv is not installed — see the Prerequisites section of CLAUDE.md." >&2
  exit 1
fi

for root in "${MIGRATION_ROOTS[@]}"; do
  echo "==> alembic upgrade head  (${root})"
  (cd "${REPO_ROOT}/${root}" && uv run alembic upgrade head)
done

echo "==> database is at head"

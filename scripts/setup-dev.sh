#!/usr/bin/env bash
set -euo pipefail

# One-command dev environment setup: installs all Python + Node dependencies.
#
# Implemented in TASK-052c. CLAUDE.md's "Start full local stack" documents this
# as one of the three commands a new developer runs, which is why every
# prerequisite check below fails with a sentence rather than a traceback: the
# person running it has, by definition, not set the environment up yet.
#
# What it installs:
#   uv sync --all-packages   every Python service and shared package
#   npm install              every workspace declared in the root package.json
#
# --all-packages is not optional. The root pyproject.toml is a *virtual* root --
# it declares no package of its own -- so a bare `uv sync` installs the dev
# dependency group and none of the workspace members, exits 0, and leaves you
# with an environment that looks installed and imports nothing.
#
# The npm workspaces are deliberately not enumerated, here or in TASK-052c. A
# root `npm install` covers whatever the root package.json declares, and that
# list has already grown past the three packages the task originally named.
#
# IDEMPOTENCY -- read this before editing.
# Running this twice is a no-op the second time because `uv sync` and
# `npm install` are each already no-ops against an unchanged lockfile. That is
# the entire mechanism; the script keeps no state of its own and needs none.
# Do NOT "improve" it with force-reinstall logic -- `uv sync --reinstall`,
# `npm ci`, or an `rm -rf node_modules` -- because every one of those turns the
# second run into a full reinstall and breaks the property TASK-052c asks for.
# `npm ci` in particular deletes node_modules before it starts. A developer who
# genuinely wants a clean slate should run that deliberately, not have it
# happen on every invocation.

fail() {
  echo "setup-dev: $*" >&2
  exit 1
}

warn() {
  echo "setup-dev: warning: $*" >&2
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

# Compares dotted numeric versions: version_ge 24.15.0 24.15 -> true.
# Absent components count as zero, so 24 is not >= 24.15 but 24.15 is.
version_ge() {
  local have="$1" want="$2" i hv wv
  local -a h w
  IFS=. read -r -a h <<<"${have}"
  IFS=. read -r -a w <<<"${want}"
  for i in 0 1 2; do
    hv="${h[i]:-0}"
    wv="${w[i]:-0}"
    hv="${hv//[!0-9]/}"
    wv="${wv//[!0-9]/}"
    hv="${hv:-0}"
    wv="${wv:-0}"
    if [ "${hv}" -gt "${wv}" ]; then return 0; fi
    if [ "${hv}" -lt "${wv}" ]; then return 1; fi
  done
  return 0
}

# --- Python toolchain -------------------------------------------------------

command -v uv >/dev/null 2>&1 || fail \
  "uv is not installed. It is the package manager for every Python service and package here (not pip, not poetry). Install it with 'pip install uv' or from https://docs.astral.sh/uv/, then re-run."

# --- Node toolchain ---------------------------------------------------------
#
# Two files in this repository constrain the Node version and they say
# different things, so both are checked and every message names the file it
# came from. A developer told only "wrong Node version" has to work out for
# themselves which of the two actually governs, which is what this avoids.
#
#   package.json engines.node (">=24.15")  a hard floor -- below it, stop.
#   .nvmrc ("24")                          the version CI installs, and the
#                                          authoritative statement of what this
#                                          repo is built against.
#
# A mismatch against .nvmrc warns rather than stops, because CLAUDE.md's own
# prerequisite line reads "node 24+" -- a floor, not an exact major. Refusing
# to install on a newer Node would block a developer whose environment the
# repository's own documentation permits. The warning still does the useful
# work: it names .nvmrc as what CI runs, so anyone whose local build and CI
# disagree knows immediately where to look.

command -v node >/dev/null 2>&1 || fail \
  "node is not installed. See .nvmrc for the version CI uses."
command -v npm >/dev/null 2>&1 || fail \
  "npm is not installed. It ships with Node, so check the Node install."

NODE_VERSION="$(node --version)"
NODE_VERSION="${NODE_VERSION#v}"

# engines.node is a floor of the form ">=24.15". Read it without jq, which is
# not a documented prerequisite of this repo and is missing from many shells.
ENGINES_FLOOR="$(sed -n 's/.*"node"[[:space:]]*:[[:space:]]*"[^0-9]*\([0-9][0-9.]*\).*/\1/p' package.json | head -1)"
if [ -n "${ENGINES_FLOOR}" ] && ! version_ge "${NODE_VERSION}" "${ENGINES_FLOOR}"; then
  fail "Node ${NODE_VERSION} is below the minimum this repo requires: package.json sets engines.node >= ${ENGINES_FLOOR}. Upgrade Node and re-run."
fi

if [ -f .nvmrc ]; then
  NVMRC_PIN="$(tr -d '[:space:]' <.nvmrc)"
  NVMRC_PIN="${NVMRC_PIN#v}"
  # A version manager may pin an alias ("lts/*", "node") rather than a number.
  # Nothing here can compare against that, so say so instead of guessing.
  if [ -z "${NVMRC_PIN##[0-9]*}" ]; then
    if [ "${NODE_VERSION%%.*}" != "${NVMRC_PIN%%.*}" ]; then
      warn "Node ${NODE_VERSION} does not match .nvmrc, which pins ${NVMRC_PIN} and is the version CI installs. Continuing, because CLAUDE.md requires 'node 24+' rather than an exact major -- but a local build differing from CI here is yours to justify. 'nvm use' switches to the pinned version."
    fi
  else
    warn ".nvmrc pins '${NVMRC_PIN}', which is not a version number -- cannot compare it, skipping this check."
  fi
else
  warn "no .nvmrc found -- cannot check Node against the version CI installs."
fi

# --- Install ----------------------------------------------------------------

echo "setup-dev: installing Python dependencies (uv sync --all-packages)"
uv sync --all-packages || fail "uv sync failed -- see the output above."

echo "setup-dev: installing Node dependencies (npm install, all root workspaces)"
npm install || fail "npm install failed -- see the output above."

echo "setup-dev: done. See CLAUDE.md, 'Start full local stack', for the rest."

#!/usr/bin/env bash
#
# Tests for detect-changed-members.sh.
#
# The script decides which CI jobs run, so its failure mode is silent
# under-selection — a green run that tested nothing looks identical to a green
# run that tested everything. Two bugs of exactly that shape landed in
# consecutive pull requests before this existed. Every case below is therefore
# written as much to pin down what must NOT be selected as what must be.
#
# No framework on purpose: this repo runs one Python and one Node toolchain, and
# adding bats-core to test forty lines of branching would be a third. Run it
# directly:
#
#     bash .github/scripts/detect-changed-members.test.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DETECT="$SCRIPT_DIR/detect-changed-members.sh"

passed=0
failed=0


# Run the detector over a newline-separated file list and echo one output value.
detect_key() {
  local key="$1" changed="$2"
  printf '%s\n' "$changed" | bash "$DETECT" | sed -n "s/^$key=//p"
}


assert_key() {
  local name="$1" key="$2" changed="$3" expected="$4"
  local got
  got="$(detect_key "$key" "$changed")"
  if [ "$got" = "$expected" ]; then
    passed=$((passed + 1))
    printf 'PASS  %s\n' "$name"
  else
    failed=$((failed + 1))
    printf 'FAIL  %s\n      %s expected %s\n      %s got      %s\n' \
      "$name" "$key" "$expected" "$key" "$got"
  fi
}


assert_members() {
  assert_key "$1" members "$2" "$3"
}


section() {
  printf '\n--- %s\n' "$1"
}


section 'the docs/api gap this suite was written for'
assert_members 'track-a-clinical spec alone selects its service' \
  'docs/api/track-a-clinical.yaml' '["services/track-a-clinical"]'
assert_members 'track-b-rag spec alone selects its service' \
  'docs/api/track-b-rag.yaml' '["services/track-b-rag"]'
assert_members 'audio-ingestion spec alone selects its service' \
  'docs/api/audio-ingestion.yaml' '["services/audio-ingestion"]'
assert_members 'two specs select both services' \
  "$(printf 'docs/api/track-b-rag.yaml\ndocs/api/audio-ingestion.yaml')" \
  '["services/audio-ingestion","services/track-b-rag"]'

section 'must not over-select — the anchors are what make these pass'
assert_members 'an unrelated docs file selects nothing' \
  'docs/adr/0003-redis-pubsub-not-kafka.md' '[]'
assert_members 'a spec for a service with no job selects nothing' \
  'docs/api/some-future-thing.yaml' '[]'
assert_members 'a nested lookalike path selects nothing' \
  'services/x/docs/api/track-b-rag.yaml' '[]'
assert_members 'a .yaml.bak suffix selects nothing' \
  'docs/api/track-b-rag.yaml.bak' '[]'
assert_members 'a README selects nothing' 'README.md' '[]'
assert_members 'nothing changed selects nothing' '' '[]'

section 'service and package selection'
assert_members 'a service selects only itself' \
  'services/track-b-rag/src/x.py' '["services/track-b-rag"]'
# Three couplings at once: the JWT contract test in packages/session-auth, and
# the shared SQLAlchemy models that track-b-rag and policy-scraper import from
# this service rather than mapping their own.
assert_members 'track-a-clinical src selects its dependents' \
  'services/track-a-clinical/src/x.py' \
  '["packages/session-auth","services/policy-scraper","services/track-a-clinical","services/track-b-rag"]'

# The model dependents hang off src/ specifically. A migration or a test is not
# code those services import, so it selects the JWT pairing and no more — the
# rule stays as narrow as the coupling it stands for. Note what it must NOT
# select: the issuer changing is a reason to re-run the contract test, not a
# reason to re-test every service the way an edit to session-auth itself would.
assert_members 'a track-a-clinical migration selects only the JWT pairing' \
  'services/track-a-clinical/migrations/versions/0005_x.py' \
  '["packages/session-auth","services/track-a-clinical"]'

# session-auth is a package like any other: editing it re-tests every service,
# because audio-ingestion and nudge-service both authenticate through it.
assert_members 'session-auth selects itself and every service' \
  'packages/session-auth/src/x.py' \
  '["packages/session-auth","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
assert_members 'a spec and its own service dedupe to one entry' \
  "$(printf 'docs/api/track-b-rag.yaml\nservices/track-b-rag/src/x.py')" \
  '["services/track-b-rag"]'
assert_members 'a package selects itself and every service' \
  'packages/api-envelope/src/x.py' \
  '["packages/api-envelope","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
# cors-policy is a package like any other. TASK-041c: track-a-clinical and
# track-b-rag install its middleware, audio-ingestion and nudge-service read the
# same origin list for their WebSocket handshakes.
assert_members 'cors-policy selects itself and every service'   'packages/cors-policy/src/x.py'   '["packages/cors-policy","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
assert_members 'bedrock-client selects itself and every service'   'packages/bedrock-client/src/x.py'   '["packages/bedrock-client","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
assert_members 'a package plus a spec still dedupes' \
  "$(printf 'packages/api-envelope/src/x.py\ndocs/api/track-b-rag.yaml')" \
  '["packages/api-envelope","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'

section 'roots that must test everything'
for root in pyproject.toml uv.lock .github/workflows/ci.yml; do
  assert_members "$root selects every member" "$root" \
    '["packages/api-envelope","packages/bedrock-client","packages/cors-policy","packages/crypto-utils","packages/fhir-types","packages/hipaa-logger","packages/payer-vocab","packages/session-auth","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
done
# The selection logic cannot be trusted to select its own blast radius, so a
# change to it runs everything. Without this a bug in the detector would be
# merged by a run that tested nothing — the exact failure this suite guards.
assert_members 'a change to the detector itself selects every member' \
  '.github/scripts/detect-changed-members.sh' \
  '["packages/api-envelope","packages/bedrock-client","packages/cors-policy","packages/crypto-utils","packages/fhir-types","packages/hipaa-logger","packages/payer-vocab","packages/session-auth","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'

section 'any_python gates the lint, typecheck and test jobs'
assert_key 'any_python is false when nothing python-ish moved' \
  any_python 'README.md' 'false'
assert_key 'any_python is true for a service change' \
  any_python 'services/track-b-rag/src/x.py' 'true'
assert_key 'any_python is false for a frontend-only change' \
  any_python 'apps/web/src/App.tsx' 'false'

section 'the frontend and dual-toolchain flags — no coverage before this suite'
assert_key 'web reacts to apps/web' web 'apps/web/src/App.tsx' 'true'
assert_key 'web reacts to audio-wire, which ships as source' \
  web 'packages/audio-wire/src/frame.ts' 'true'
assert_key 'web ignores apps/mobile' web 'apps/mobile/App.tsx' 'false'
assert_key 'mobile reacts to apps/mobile' mobile 'apps/mobile/App.tsx' 'true'
assert_key 'mobile reacts to audio-wire' \
  mobile 'packages/audio-wire/src/frame.ts' 'true'
assert_key 'mobile ignores apps/web' mobile 'apps/web/src/App.tsx' 'false'
assert_key 'fhir_types reacts to its own package' \
  fhir_types 'packages/fhir-types/src/codes.py' 'true'
assert_key 'fhir_types reacts to the npm lockfile' \
  fhir_types 'package-lock.json' 'true'
assert_key 'fhir_types ignores an unrelated package' \
  fhir_types 'packages/crypto-utils/src/x.py' 'false'
assert_key 'audio_wire reacts to its own package' \
  audio_wire 'packages/audio-wire/src/frame.ts' 'true'
assert_key 'audio_wire reacts to the npm lockfile' \
  audio_wire 'package-lock.json' 'true'
assert_key 'audio_wire ignores the uv lockfile' \
  audio_wire 'uv.lock' 'false'
assert_key 'session_client reacts to its own package' \
  session_client 'packages/session-client/src/jwt.ts' 'true'
assert_key 'session_client reacts to the npm lockfile' \
  session_client 'package-lock.json' 'true'
assert_key 'session_client ignores the uv lockfile' \
  session_client 'uv.lock' 'false'
assert_key 'session_client ignores audio-wire' \
  session_client 'packages/audio-wire/src/frame.ts' 'false'
assert_key 'nudge_client reacts to its own package' \
  nudge_client 'packages/nudge-client/src/payload.ts' 'true'
assert_key 'nudge_client reacts to the npm lockfile' \
  nudge_client 'package-lock.json' 'true'
assert_key 'nudge_client ignores the uv lockfile' \
  nudge_client 'uv.lock' 'false'
assert_key 'nudge_client ignores session-client' \
  nudge_client 'packages/session-client/src/jwt.ts' 'false'
# Both apps compile this package's source into themselves, so a change to the
# only client that may re-mint a session token has to re-run both app suites.
assert_key 'web reacts to session-client, which ships as source' \
  web 'packages/session-client/src/sessions.ts' 'true'
assert_key 'mobile reacts to session-client' \
  mobile 'packages/session-client/src/sessions.ts' 'true'
# Both apps render nudges from this package's parser and dismiss them through
# its acknowledge client, so a change to it changes what a provider is shown
# in both apps at once.
assert_key 'web reacts to nudge-client, which ships as source' \
  web 'packages/nudge-client/src/payload.ts' 'true'
assert_key 'mobile reacts to nudge-client' \
  mobile 'packages/nudge-client/src/payload.ts' 'true'

section 'the conventions the rules are derived from must hold in the repo'
# The docs/api rule maps a spec to a job by filename alone. If a spec is named
# for something that is not a service directory it silently gets no job — the
# same class of gap one level up, so it is asserted against the real tree.
for spec in "$REPO_ROOT"/docs/api/*.yaml; do
  [ -e "$spec" ] || continue
  name="$(basename "$spec" .yaml)"
  if [ -d "$REPO_ROOT/services/$name" ]; then
    passed=$((passed + 1))
    printf 'PASS  docs/api/%s.yaml maps to a real service\n' "$name"
  else
    failed=$((failed + 1))
    printf 'FAIL  docs/api/%s.yaml maps to no services/ directory — it would\n' "$name"
    printf '      select no CI job, so its drift test would never run\n'
  fi
done

# A service directory absent from ALL_SERVICES gets no CI at all, which is the
# silent-gap failure again. Compare the array against the real tree.
declared_services="$(sed -n '/^ALL_SERVICES=(/,/^)/p' "$DETECT" \
  | sed '1d;$d' | tr -d ' ' | sort)"
actual_services="$(
  for dir in "$REPO_ROOT"/services/*/; do
    [ -d "$dir" ] || continue
    basename "$dir"
  done | sort
)"
if [ "$declared_services" = "$actual_services" ]; then
  passed=$((passed + 1))
  printf 'PASS  ALL_SERVICES matches the services/ directory\n'
else
  failed=$((failed + 1))
  printf 'FAIL  ALL_SERVICES does not match services/ — a service missing from\n'
  printf '      the array gets no CI job at all\n'
  diff <(printf '%s\n' "$declared_services") <(printf '%s\n' "$actual_services") \
    | sed 's/^/      /' || true
fi

# The same gap one directory over, and the one this suite did not cover until
# TASK-041 added packages/session-auth and noticed. CLAUDE.md's ci.yml section
# requires every directory under packages/ to have its own test job; a package
# missing from ALL_PACKAGES silently has none, and its own suite never runs —
# which is exactly how packages/hipaa-logger went untested for a while.
#
# Compared against the Python packages specifically: packages/audio-wire is
# TypeScript-only, has its own job in ci.yml, and is excluded from the uv
# workspace in the root pyproject.toml. A pyproject.toml is what makes a package
# a member of the Python matrix, so that is the test — not the directory listing,
# which would demand audio-wire join an array it does not belong in.
declared_packages="$(sed -n 's/^ALL_PACKAGES=(\(.*\))$/\1/p' "$DETECT" | tr ' ' '\n' | sort)"
actual_packages="$(
  for dir in "$REPO_ROOT"/packages/*/; do
    [ -f "$dir/pyproject.toml" ] || continue
    basename "$dir"
  done | sort
)"
if [ "$declared_packages" = "$actual_packages" ]; then
  passed=$((passed + 1))
  printf 'PASS  ALL_PACKAGES matches the Python packages under packages/\n'
else
  failed=$((failed + 1))
  printf 'FAIL  ALL_PACKAGES does not match packages/ — a package missing from\n'
  printf '      the array gets no test job, so its own suite never runs\n'
  diff <(printf '%s\n' "$declared_packages") <(printf '%s\n' "$actual_packages") \
    | sed 's/^/      /' || true
fi

# The TypeScript-only packages are the half ALL_PACKAGES cannot cover, and until
# TASK-042 nothing asserted them at all: `packages/audio-wire` had explicit cases
# because someone wrote them, and a second TS package could have landed with no
# job and no failing test — the same silent gap, in the one place the array check
# above is deliberately blind to.
#
# The rule these are held to is the naming convention the two existing jobs
# already follow: a TS package's output key is its directory name with hyphens
# replaced by underscores. Asserting the convention rather than a list means a
# third package is covered the moment it exists.
for dir in "$REPO_ROOT"/packages/*/; do
  [ -d "$dir" ] || continue
  [ -f "$dir/pyproject.toml" ] && continue
  name="$(basename "$dir")"
  key="${name//-/_}"
  got="$(detect_key "$key" "packages/$name/src/x.ts")"
  if [ "$got" = "true" ]; then
    passed=$((passed + 1))
    printf 'PASS  packages/%s selects the %s job\n' "$name" "$key"
  else
    failed=$((failed + 1))
    printf 'FAIL  packages/%s selects no job — a TypeScript-only package needs an\n' "$name"
    printf '      output named %s in the detector and a job in ci.yml, or its\n' "$key"
    printf '      suite never runs\n'
  fi
  # The uv workspace globs packages/*, so a TypeScript-only package must also be
  # excluded there or `uv run` fails outright for every Python service — not just
  # in CI, but on any developer's machine, with an error naming a missing
  # pyproject.toml rather than the glob that claimed the directory.
  if grep -q "packages/$name" "$REPO_ROOT/pyproject.toml"; then
    passed=$((passed + 1))
    printf 'PASS  packages/%s is excluded from the uv workspace\n' "$name"
  else
    failed=$((failed + 1))
    printf 'FAIL  packages/%s has no pyproject.toml and is not in the uv\n' "$name"
    printf "      workspace's exclude list — uv sync fails for every service\n"
  fi
done

printf '\n%d passed, %d failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]

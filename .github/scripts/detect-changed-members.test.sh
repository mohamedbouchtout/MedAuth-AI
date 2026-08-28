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
# Three couplings at once: the JWT contract test in audio-ingestion, and the
# shared SQLAlchemy models that track-b-rag and policy-scraper import from this
# service rather than mapping their own.
assert_members 'track-a-clinical src selects its dependents' \
  'services/track-a-clinical/src/x.py' \
  '["services/audio-ingestion","services/policy-scraper","services/track-a-clinical","services/track-b-rag"]'

# The model dependents hang off src/ specifically. A migration or a test is not
# code those services import, so it selects the JWT pairing and no more — the
# rule stays as narrow as the coupling it stands for.
assert_members 'a track-a-clinical migration selects only the JWT pairing' \
  'services/track-a-clinical/migrations/versions/0005_x.py' \
  '["services/audio-ingestion","services/track-a-clinical"]'
assert_members 'a spec and its own service dedupe to one entry' \
  "$(printf 'docs/api/track-b-rag.yaml\nservices/track-b-rag/src/x.py')" \
  '["services/track-b-rag"]'
assert_members 'a package selects itself and every service' \
  'packages/api-envelope/src/x.py' \
  '["packages/api-envelope","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
assert_members 'bedrock-client selects itself and every service'   'packages/bedrock-client/src/x.py'   '["packages/bedrock-client","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
assert_members 'a package plus a spec still dedupes' \
  "$(printf 'packages/api-envelope/src/x.py\ndocs/api/track-b-rag.yaml')" \
  '["packages/api-envelope","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'

section 'roots that must test everything'
for root in pyproject.toml uv.lock .github/workflows/ci.yml; do
  assert_members "$root selects every member" "$root" \
    '["packages/api-envelope","packages/bedrock-client","packages/crypto-utils","packages/fhir-types","packages/hipaa-logger","packages/payer-vocab","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'
done
# The selection logic cannot be trusted to select its own blast radius, so a
# change to it runs everything. Without this a bug in the detector would be
# merged by a run that tested nothing — the exact failure this suite guards.
assert_members 'a change to the detector itself selects every member' \
  '.github/scripts/detect-changed-members.sh' \
  '["packages/api-envelope","packages/bedrock-client","packages/crypto-utils","packages/fhir-types","packages/hipaa-logger","packages/payer-vocab","services/audio-ingestion","services/fhir-integration","services/nudge-service","services/policy-scraper","services/prior-auth","services/track-a-clinical","services/track-b-rag"]'

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

printf '\n%d passed, %d failed\n' "$passed" "$failed"
[ "$failed" -eq 0 ]

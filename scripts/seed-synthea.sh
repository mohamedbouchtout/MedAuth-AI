#!/usr/bin/env bash
set -euo pipefail

# Loads synthetic Synthea patients into the local HAPI FHIR server.
#
# Implemented in TASK-052, which needs patients on the local server to verify
# the base FHIR reads against something other than a hand-written fixture.
#
# Why the JAR and not a pre-built sample archive: the sample-data download that
# is widely linked for this
# (synthetichealth.github.io/synthea-sample-data/downloads/...) answers 404, so
# pointing the script at it would leave a prerequisite that fails the first time
# anyone runs it. The release JAR at the URL below was checked and serves. It
# costs a Java dependency and a few minutes of generation, and it is the
# canonical source.
#
# Re-running is safe: the script counts the patients already on the server and
# does nothing when the population is already there. Pass FORCE=1 to seed anyway.

FHIR_BASE_URL="${FHIR_BASE_URL:-http://localhost:8080/fhir}"
SYNTHEA_POPULATION="${SYNTHEA_POPULATION:-100}"
# Massachusetts because it is the pilot geography — the payer vocabulary's first
# licensee slug is bcbs-ma, so the seeded Coverage resources name payers the
# corpus actually holds policies for.
SYNTHEA_STATE="${SYNTHEA_STATE:-Massachusetts}"
# Fixed so two developers, and CI, get the same patients. Change it deliberately.
SYNTHEA_SEED="${SYNTHEA_SEED:-20260830}"
SYNTHEA_JAR_URL="https://github.com/synthetichealth/synthea/releases/latest/download/synthea-with-dependencies.jar"
CACHE_DIR="${SYNTHEA_CACHE_DIR:-${TMPDIR:-/tmp}/medauth-synthea}"
JAR_PATH="${CACHE_DIR}/synthea-with-dependencies.jar"

fail() {
  echo "seed-synthea: $*" >&2
  exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required"
command -v java >/dev/null 2>&1 || fail "java is required to run Synthea — install a JDK 11 or newer"

echo "seed-synthea: checking HAPI FHIR at ${FHIR_BASE_URL}"
if ! curl -sS -f --max-time 10 "${FHIR_BASE_URL}/metadata" >/dev/null 2>&1; then
  fail "no FHIR server answering at ${FHIR_BASE_URL} — run 'docker compose up -d hapi-fhir' first"
fi

existing="$(curl -sS -f --max-time 30 "${FHIR_BASE_URL}/Patient?_summary=count" |
  grep -o '"total"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$' || echo 0)"
existing="${existing:-0}"

if [ "${FORCE:-0}" != "1" ] && [ "${existing}" -ge "${SYNTHEA_POPULATION}" ]; then
  echo "seed-synthea: ${existing} patients already loaded — nothing to do (FORCE=1 to seed anyway)"
  exit 0
fi

mkdir -p "${CACHE_DIR}"
if [ ! -f "${JAR_PATH}" ]; then
  echo "seed-synthea: downloading Synthea (one time, ~150MB)"
  curl -sS -L --max-time 600 -o "${JAR_PATH}.partial" "${SYNTHEA_JAR_URL}" ||
    fail "could not download Synthea from ${SYNTHEA_JAR_URL}"
  mv "${JAR_PATH}.partial" "${JAR_PATH}"
fi

OUT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/medauth-synthea-out.XXXXXX")"
trap 'rm -rf "${OUT_DIR}"' EXIT

echo "seed-synthea: generating ${SYNTHEA_POPULATION} patients in ${SYNTHEA_STATE} (seed ${SYNTHEA_SEED})"
java -jar "${JAR_PATH}" \
  -p "${SYNTHEA_POPULATION}" \
  -s "${SYNTHEA_SEED}" \
  --exporter.baseDirectory "${OUT_DIR}" \
  --exporter.fhir.export true \
  --exporter.hospital.fhir.export true \
  --exporter.practitioner.fhir.export true \
  --exporter.csv.export false \
  "${SYNTHEA_STATE}" >/dev/null || fail "Synthea generation failed"

BUNDLE_DIR="${OUT_DIR}/fhir"
[ -d "${BUNDLE_DIR}" ] || fail "Synthea produced no FHIR output in ${BUNDLE_DIR}"

# Hospitals and practitioners first: patient bundles reference organizations and
# practitioners, and posting a patient bundle before them leaves those references
# dangling on a server configured to check them.
post_bundle() {
  local file="$1"
  curl -sS -f --max-time 120 \
    -H 'Content-Type: application/fhir+json' \
    -d "@${file}" \
    "${FHIR_BASE_URL}" >/dev/null
}

loaded=0
failed=0
for prefix in hospitalInformation practitionerInformation ""; do
  for bundle in "${BUNDLE_DIR}/${prefix}"*.json; do
    [ -e "${bundle}" ] || continue
    case "${prefix}" in
      "")
        case "$(basename "${bundle}")" in
          hospitalInformation* | practitionerInformation*) continue ;;
        esac
        ;;
    esac
    if post_bundle "${bundle}"; then
      loaded=$((loaded + 1))
    else
      failed=$((failed + 1))
      echo "seed-synthea: failed to load $(basename "${bundle}")" >&2
    fi
  done
done

echo "seed-synthea: loaded ${loaded} bundles, ${failed} failed"
[ "${failed}" -eq 0 ] || fail "${failed} bundle(s) did not load"

total="$(curl -sS -f --max-time 30 "${FHIR_BASE_URL}/Patient?_summary=count" |
  grep -o '"total"[[:space:]]*:[[:space:]]*[0-9]*' | grep -o '[0-9]*$' || echo 0)"
echo "seed-synthea: ${total} patients now on ${FHIR_BASE_URL}"

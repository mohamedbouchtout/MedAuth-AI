#!/usr/bin/env bash
#
# Decide which CI jobs a change needs to run.
#
# Reads changed file paths on stdin, one per line, and writes the seven outputs
# the `changes` job publishes — members, any_python, web, mobile, fhir_types,
# audio_wire, session_client — to stdout as `key=value` lines. The workflow
# redirects that into $GITHUB_OUTPUT.
#
# **Why this is a script rather than inline `run:` bash.** These rules decide
# whether anything is tested at all, and their failure mode is silent
# under-selection: CI passes because nothing ran, which looks exactly like CI
# passing because everything ran. Two bugs of that shape landed in consecutive
# pull requests — a contract test that never re-ran when the code it guarded
# moved, and `docs/api/**` selecting no job whatsoever. Inline in YAML the logic
# could only be exercised by pushing a commit, and then only for whatever paths
# that commit happened to touch. Here it can be called with any file list, which
# is what `detect-changed-members.test.sh` does.
#
# **The input boundary is deliberate.** Working out the base SHA and running
# `git diff` stay in the workflow, because they depend on GitHub's event context
# and on a real repository. What arrives here is just a list of paths, so every
# rule below is a pure function of its input and needs no git, no network and no
# GitHub to test. The cost of that split is that the base-SHA fallback for a new
# branch's all-zero SHA remains untested; that is a known and accepted boundary,
# not an oversight.
#
# Keep this script and its test in step: a new rule without a new case in the
# test file is exactly the situation this extraction exists to prevent.

set -euo pipefail

# Workspace members that get a job. A service or package missing from these
# arrays gets no CI, so adding one here is part of adding it to the monorepo.
ALL_SERVICES=(
  audio-ingestion
  track-a-clinical
  track-b-rag
  fhir-integration
  prior-auth
  nudge-service
  policy-scraper
)
ALL_PACKAGES=(api-envelope hipaa-logger crypto-utils fhir-types payer-vocab bedrock-client session-auth cors-policy)


# Return 0 when any changed path matches the given extended regular expression.
changed_matches() {
  printf '%s\n' "$CHANGED" | grep -qE "$1"
}


main() {
  CHANGED="$(cat)"

  local selected=()

  if changed_matches '^(pyproject\.toml|uv\.lock|\.github/workflows/ci\.yml|\.github/scripts/)'; then
    # Workspace root, the CI definition, or this script and its test — test
    # everything. The scripts directory is here because a change to the
    # selection logic itself cannot be trusted to select its own blast radius.
    for p in "${ALL_PACKAGES[@]}"; do selected+=("packages/$p"); done
    for s in "${ALL_SERVICES[@]}"; do selected+=("services/$s"); done
  else
    # A changed package gets its own job, so its unit and integration tests
    # actually run — they never did while the matrix held services only.
    for p in "${ALL_PACKAGES[@]}"; do
      if changed_matches "^packages/$p/"; then
        selected+=("packages/$p")
      fi
    done

    if changed_matches '^packages/'; then
      # Every service imports the shared packages, so re-test them all too.
      for s in "${ALL_SERVICES[@]}"; do selected+=("services/$s"); done
    else
      for s in "${ALL_SERVICES[@]}"; do
        if changed_matches "^services/$s/"; then
          selected+=("services/$s")
        fi
      done

      # packages/session-auth validates the session JWT that track-a-clinical
      # mints, and tests/unit/test_issuer_contract.py proves the two agree by
      # calling the real issuer. That test is decorative unless a change to the
      # issuer re-runs it, so select the validator's job too. It moved here from
      # services/audio-ingestion in TASK-041, along with the validator itself —
      # the coupling is to whoever owns the validation, not to one of its
      # consumers.
      if changed_matches '^services/track-a-clinical/'; then
        selected+=("packages/session-auth")
      fi

      # track-a-clinical also ships the shared SQLAlchemy models (CLAUDE.md,
      # "Where the shared SQLAlchemy models live"), and every service that
      # writes those tables imports them from there rather than mapping its own
      # — which is the whole point of centralising them. That makes an edit to
      # a mapped class a change to those services' code, and until TASK-040 it
      # selected none of their jobs: track-b-rag's nudge emitter builds an
      # insert against ClinicalNudge, and a column renamed one service over
      # would have gone green here and failed at runtime.
      #
      # The same reasoning as the JWT pairing above, and the same reasoning
      # CLAUDE.md gives for a service's OpenAPI spec: a test that guards two
      # things has to re-run when either of them moves. These are dependency
      # edges rather than test couplings, so they are listed from
      # `medauth-track-a-clinical` in each pyproject.toml — add to this list
      # when a new service declares that dependency.
      if changed_matches '^services/track-a-clinical/src/'; then
        selected+=("services/track-b-rag")
        selected+=("services/policy-scraper")
      fi
    fi

    # A service's OpenAPI spec is half of a contract its own test checks:
    # tests/unit/api/test_openapi_contract.py compares docs/api/<name>.yaml
    # against the app's generated schema on routes, methods, status codes and
    # required request fields. Editing only the spec is therefore a way to break
    # that test, and it used to select no job at all — the drift test ran on
    # changes to the half that cannot drift alone and not on the half that can.
    #
    # The mapping is the filename convention from CLAUDE.md's API Design
    # section, docs/api/<service-name>.yaml, so no lookup table is needed and a
    # spec added for a service that has no job yet correctly selects nothing.
    # The test asserts that convention against the real docs/api directory, so a
    # mis-named spec fails rather than silently going untested.
    #
    # This sits outside the packages/services if-else above because it holds
    # either way: a spec edit selects its service whether or not anything under
    # packages/ moved.
    for s in "${ALL_SERVICES[@]}"; do
      if changed_matches "^docs/api/$s\.yaml$"; then
        selected+=("services/$s")
      fi
    done
  fi

  # Several rules above can select the same member — a service whose own
  # directory, its OpenAPI spec, and the track-a-clinical pairing all moved in
  # one change picks it three times. The matrix must still run it once.
  if [ ${#selected[@]} -gt 0 ]; then
    mapfile -t selected < <(printf '%s\n' "${selected[@]}" | sort -u)
  fi

  if [ ${#selected[@]} -eq 0 ]; then
    echo "members=[]"
    echo "any_python=false"
  else
    local joined
    printf -v joined '"%s",' "${selected[@]}"
    echo "members=[${joined%,}]"
    echo "any_python=true"
  fi

  # Both apps import @medauth/audio-wire, @medauth/session-client and
  # @medauth/nudge-client, and all three ship TypeScript source rather than a
  # build, so a change in any of them is a change to code that runs inside the
  # apps — test them when it moves.
  if changed_matches '^(apps/web/|packages/(audio-wire|session-client|nudge-client)/)'; then
    echo "web=true"
  else
    echo "web=false"
  fi

  if changed_matches '^(apps/mobile/|packages/(audio-wire|session-client|nudge-client)/)'; then
    echo "mobile=true"
  else
    echo "mobile=false"
  fi

  # fhir-types spans both toolchains, so it also reacts to the npm root: the
  # TypeScript mirrors are an npm workspace, and a lockfile change can break
  # `npm ci` without touching the package itself.
  if changed_matches '^(pyproject\.toml|uv\.lock|package\.json|package-lock\.json|\.github/workflows/ci\.yml|packages/fhir-types/)'; then
    echo "fhir_types=true"
  else
    echo "fhir_types=false"
  fi

  # audio-wire is TypeScript only, so it gets an npm job rather than a place in
  # ALL_PACKAGES. It reacts to the npm root for the same reason fhir-types does:
  # a lockfile change can break `npm ci` without touching the package.
  if changed_matches '^(package\.json|package-lock\.json|\.github/workflows/ci\.yml|packages/audio-wire/)'; then
    echo "audio_wire=true"
  else
    echo "audio_wire=false"
  fi

  # session-client is TypeScript only for the same reason and gets the same
  # treatment: its own npm job, no place in ALL_PACKAGES, and a reaction to the
  # npm root because a lockfile change can break `npm ci` without touching the
  # package. It holds the only client that may re-mint a session token
  # (TASK-042), so a change here that nothing tested would be a change to how a
  # credential is refreshed in both apps at once.
  if changed_matches '^(package\.json|package-lock\.json|\.github/workflows/ci\.yml|packages/session-client/)'; then
    echo "session_client=true"
  else
    echo "session_client=false"
  fi

  # nudge-client is TypeScript only and gets the same treatment again: its own
  # npm job, no place in ALL_PACKAGES, and a reaction to the npm root because a
  # lockfile change can break `npm ci` without touching the package. It holds the
  # payload contract both apps read a nudge through and the acknowledge call both
  # of them dismiss one with (TASK-043), so a change here that nothing tested
  # would change what a provider is shown in both apps at once.
  if changed_matches '^(package\.json|package-lock\.json|\.github/workflows/ci\.yml|packages/nudge-client/)'; then
    echo "nudge_client=true"
  else
    echo "nudge_client=false"
  fi
}

main "$@"

#!/usr/bin/env bash
# Launch the local stack with the Bash version shipped on macOS.
set -euo pipefail

usage() {
    echo "Usage: $0 [--skip-web] [--migrate]"
    echo "Runs in the foreground. Press Ctrl-C to stop the app services."
}

skip_web=false
migrate=false
for arg in "$@"; do
    case "$arg" in
        --skip-web) skip_web=true ;;
        --migrate) migrate=true ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

fail() {
    echo "dev-up: $*" >&2
    exit 1
}

cd "$(dirname "${BASH_SOURCE[0]}")/.."
[[ -f .env.local ]] || fail "Copy .env.example to .env.local and fill it in."

# Match dev-up.ps1: trim whitespace, retain literal values, and skip blanks.
# Read assignments as data so shell syntax in a value is never executed.
assignment='^[[:space:]]*([a-zA-Z_][a-zA-Z0-9_]*)[[:space:]]*=[[:space:]]*(.*[^[:space:]])[[:space:]]*$'
while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ $assignment ]]; then
        export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
    fi
done < .env.local

for name in JWT_SIGNING_KEY DATABASE_URL SMART_WEB_RETURN_URL SMART_MOBILE_RETURN_URI; do
    [[ -n "${!name:-}" ]] || fail "Set $name in .env.local."
done

for tool in docker uv curl lsof; do
    command -v "$tool" >/dev/null || fail "$tool is required."
done
if ! "$skip_web"; then
    command -v npm >/dev/null || fail "npm is required for the web app."
fi

names=(audio-ingestion track-b-rag track-a-clinical fhir-integration nudge-service prior-auth)
ports=(8001 8002 8003 8004 8005 8007)
modules=(src.main:app track_b_rag.main:app track_a_clinical.main:app src.main:app src.main:app prior_auth.main:app)
if ! "$skip_web"; then
    names+=(web)
    ports+=(5173)
fi

for port in "${ports[@]}"; do
    if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null; then
        fail "Port $port is already in use."
    fi
done

echo "Starting Docker Compose backing services"
docker compose up -d
docker compose up -d --wait postgres redis qdrant

if "$migrate"; then
    (cd packages/hipaa-logger && uv run alembic upgrade head)
    (cd services/track-a-clinical && uv run alembic upgrade head)
fi

mkdir -p .dev-logs
launcher_file=".dev-logs/dev-up.$$.pid"
pids=()
# Bash monitor mode gives each launched job its own process group, including
# uvicorn reload workers and the npm/Vite child processes.
set -m
cleanup() {
    local pid
    for pid in ${pids[@]+"${pids[@]}"}; do
        kill -TERM -- "-$pid" 2>/dev/null || true
    done
    wait || true
    rm -f "$launcher_file"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_service() {
    local name=$1 directory=$2
    shift 2
    (cd "$directory" && exec "$@") >".dev-logs/$name.log" 2>&1 &
    pids+=("$!")
}

for i in "${!modules[@]}"; do
    start_service "${names[$i]}" "services/${names[$i]}" \
        uv run uvicorn "${modules[$i]}" --reload --port "${ports[$i]}"
done
if ! "$skip_web"; then
    start_service web apps/web npm run dev -- --port 5173 --strictPort
fi

# Record the process start time so dev-down can detect a stale or reused PID.
ps -p "$$" -o lstart= >"$launcher_file"

echo "Waiting for application services. Logs are in .dev-logs/."
pending=("${!names[@]}")
deadline=$((SECONDS + 90))
while (("${#pending[@]}" > 0)); do
    remaining=()
    for i in "${pending[@]}"; do
        name=${names[$i]}
        kill -0 "${pids[$i]}" 2>/dev/null || fail "$name exited. See .dev-logs/$name.log."
        path=/health
        if [[ "$name" == web ]]; then path=/; fi
        status=$(curl --noproxy '*' --silent --output /dev/null --write-out '%{http_code}' \
            --max-time 1 "http://localhost:${ports[$i]}$path") || status=000
        case "$status" in
            200) echo "  $name ready on :${ports[$i]}" ;;
            503) echo "  $name reports unhealthy (503). See .dev-logs/$name.log." ;;
            *) remaining+=("$i") ;;
        esac
    done
    # Bash 3.2 under nounset treats expansion of an empty array as unset.
    if [[ -z "${remaining[*]:-}" ]]; then break; fi
    if ((SECONDS >= deadline)); then
        for i in "${remaining[@]}"; do
            echo "  ${names[$i]} did not answer. See .dev-logs/${names[$i]}.log." >&2
        done
        exit 1
    fi
    pending=("${remaining[@]}")
    sleep 1
done

if ! "$skip_web"; then echo "Open http://localhost:5173"; fi
echo "Press Ctrl-C or run ./scripts/dev-down.sh to stop the app services."

# Bash 3.2 has no wait -n. Check each process so failures in any service
# are reported, including after the readiness checks have finished.
while true; do
    for i in "${!pids[@]}"; do
        kill -0 "${pids[$i]}" 2>/dev/null ||
            fail "${names[$i]} exited. See .dev-logs/${names[$i]}.log."
    done
    sleep 1
done

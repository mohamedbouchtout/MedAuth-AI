#!/usr/bin/env bash
# Stop the application processes supervised by the macOS launcher.
set -euo pipefail

usage() {
    echo "Usage: $0 [--include-backing-services]"
}

stop_backing=false
for arg in "$@"; do
    case "$arg" in
        --include-backing-services) stop_backing=true ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

cd "$(dirname "${BASH_SOURCE[0]}")/.."

stopped=false
still_listening=false
shopt -s nullglob
for file in .dev-logs/dev-up.*.pid; do
    pid=${file#.dev-logs/dev-up.}
    pid=${pid%.pid}
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || continue
    recorded=$(cat "$file") || continue
    current=$(ps -p "$pid" -o lstart=) || continue
    [[ -n "$recorded" && "$current" == "$recorded" ]] || continue

    if kill -TERM "$pid"; then
        echo "Stopping app services supervised by PID $pid."
        stopped=true
        deadline=$((SECONDS + 30))
        while [[ -f "$file" ]] && kill -0 "$pid" 2>/dev/null; do
            if ((SECONDS >= deadline)); then
                echo "Launcher $pid is still stopping. Check .dev-logs/." >&2
                exit 1
            fi
            sleep 1
        done
    fi
done

# A launcher interrupted during shutdown can leave its Uvicorn reloader
# listening. Only stop listeners whose command and cwd match this checkout.
for entry in audio-ingestion:8001 track-b-rag:8002 track-a-clinical:8003 \
    fhir-integration:8004 nudge-service:8005 prior-auth:8007; do
    service=${entry%:*}
    port=${entry#*:}
    listeners=$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null) || continue
    for pid in $listeners; do
        command=$(ps -p "$pid" -o command=) || continue
        cwd=$(lsof -a -p "$pid" -d cwd -Fn | sed -n 's/^n//p') || continue
        if [[ "$cwd" == "$PWD/services/$service" && "$command" == *"/uvicorn "* &&
              "$command" == *" --port $port" ]]; then
            if kill -TERM "$pid"; then
                stopped=true
                deadline=$((SECONDS + 5))
                while lsof -a -p "$pid" -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; do
                    if ((SECONDS >= deadline)); then
                        echo "$service (PID $pid) still holds port $port after SIGTERM." >&2
                        still_listening=true
                        break
                    fi
                    sleep 1
                done
                if ! lsof -a -p "$pid" -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
                    echo "Released port $port from the remaining $service listener (PID $pid)."
                fi
            fi
        fi
    done
done

if ! "$stopped"; then echo "No running macOS dev launcher or matching Uvicorn listener found."; fi

if "$stop_backing"; then
    echo "Stopping Docker Compose backing services."
    docker compose stop
fi

if "$still_listening"; then exit 1; fi

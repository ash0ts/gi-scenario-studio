#!/usr/bin/env bash
# Keep long, normally quiet release commands observable without hiding their result.

set -uo pipefail

usage() {
  echo "usage: $0 --interval-seconds SECONDS --label LABEL -- COMMAND [ARG ...]" >&2
}

interval_seconds=""
label=""

while (($#)); do
  case "$1" in
    --interval-seconds)
      (($# >= 2)) || { usage; exit 64; }
      interval_seconds="$2"
      shift 2
      ;;
    --label)
      (($# >= 2)) || { usage; exit 64; }
      label="$2"
      shift 2
      ;;
    --)
      shift
      break
      ;;
    *)
      usage
      exit 64
      ;;
  esac
done

if [[ ! "${interval_seconds}" =~ ^[0-9]+$ ]] ||
  ((interval_seconds < 1 || interval_seconds > 300)) ||
  [[ -z "${label}" ]] ||
  [[ "${label}" == *$'\n'* ]] ||
  (($# == 0)); then
  usage
  exit 64
fi

poll_seconds=5
if ((interval_seconds < poll_seconds)); then
  poll_seconds="${interval_seconds}"
fi

"$@" &
child_pid=$!
started_at=${SECONDS}
next_notice=${interval_seconds}
forwarded_signal=""

forward_signal() {
  local signal="$1"
  forwarded_signal="${signal}"
  if kill -0 "${child_pid}" 2>/dev/null; then
    kill -s "${signal}" "${child_pid}" 2>/dev/null || true
  fi
}

trap 'forward_signal HUP' HUP
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM

while kill -0 "${child_pid}" 2>/dev/null; do
  sleep "${poll_seconds}" &
  sleep_pid=$!
  wait "${sleep_pid}" 2>/dev/null || true
  if [[ -n "${forwarded_signal}" ]]; then
    break
  fi
  elapsed=$((SECONDS - started_at))
  if ((elapsed >= next_notice)) && kill -0 "${child_pid}" 2>/dev/null; then
    printf '::notice::%s is still running (%d seconds elapsed).\n' "${label}" "${elapsed}"
    while ((next_notice <= elapsed)); do
      next_notice=$((next_notice + interval_seconds))
    done
  fi
done

wait "${child_pid}"
status=$?
elapsed=$((SECONDS - started_at))
if ((status == 0)); then
  printf '::notice::%s completed after %d seconds.\n' "${label}" "${elapsed}"
else
  printf '::error::%s exited with status %d after %d seconds.\n' \
    "${label}" "${status}" "${elapsed}" >&2
fi
exit "${status}"

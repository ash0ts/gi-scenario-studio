#!/usr/bin/env bash
set -euo pipefail

version="1.12.6"
archive="julia-${version}-linux-x86_64.tar.gz"
expected="bbabf3bef19421a9dbd24a767d807606ab85e444323b5a1c73ffe293fa3d079a"
destination="${RUNNER_TEMP:?RUNNER_TEMP is required}/julia"
download="${RUNNER_TEMP}/${archive}"

curl --fail --location --retry 3 \
  --proto '=https' --proto-redir '=https' --tlsv1.2 \
  --output "${download}" \
  "https://julialang-s3.julialang.org/bin/linux/x64/1.12/${archive}"
printf '%s  %s\n' "${expected}" "${download}" | sha256sum --check --status
mkdir -p "${destination}"
tar --extract --gzip --file "${download}" --directory "${destination}" --strip-components=1
"${destination}/bin/julia" --version
printf '%s\n' "${destination}/bin" >> "${GITHUB_PATH:?GITHUB_PATH is required}"

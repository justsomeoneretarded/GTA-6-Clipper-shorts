#!/usr/bin/env bash
set -euo pipefail

if [[ ${GITHUB_ACTIONS:-} == true ]] && command -v apt-get >/dev/null; then
  # The static fallback crashes when opening remote streams on Ubuntu's hosted runners.
  if [[ ! -x /usr/bin/ffmpeg || ! -x /usr/bin/ffprobe ]]; then
    sudo apt-get update -qq -o Acquire::Retries=3
    sudo apt-get install -y --no-install-recommends ffmpeg
  fi
  tools="${RUNNER_TEMP:?}/gta6-distro-media"
  mkdir -p "$tools"
  ln -sf /usr/bin/ffmpeg /usr/bin/ffprobe "$tools/"
  printf '%s\n' "$tools" >> "${GITHUB_PATH:?}"
  exit 0
fi

if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  exit 0
fi

if [[ "$(uname -s):$(uname -m)" != Linux:x86_64 ]]; then
  echo 'Install FFmpeg and ffprobe for this platform.' >&2
  exit 1
fi

destination="${RUNNER_TEMP:?}/gta6-media"
mkdir -p "$destination"
release=https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1

install_binary() {
  local name=$1 expected=$2
  local archive="$destination/$name.gz"
  curl --fail --silent --show-error --location --retry 2 --max-time 105 \
    "$release/$name-linux-x64.gz" --output "$archive"
  printf '%s  %s\n' "$expected" "$archive" | sha256sum --check --status
  gzip --decompress --stdout "$archive" > "$destination/$name"
  chmod +x "$destination/$name"
  rm "$archive"
}

install_binary ffmpeg bfe8a8fc511530457b528c48d77b5737527b504a3797a9bc4866aeca69c2dffa
install_binary ffprobe 25d9b6ccb05e3d9de9e04e31e2506d8dd7f9f0418981965ac6df12e8d3afd067
printf '%s\n' "$destination" >> "${GITHUB_PATH:?}"

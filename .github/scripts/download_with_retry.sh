#!/usr/bin/env bash
download_with_retry() {
  local url="$1" output="$2"
  for attempt in 1 2 3 4 5; do
    curl -fL --retry 3 --retry-delay 5 --retry-all-errors -o "$output" "$url" && return 0
    echo "Download failed for $url on attempt $attempt"
    sleep $((attempt * 10))
  done
  return 1
}

#!/usr/bin/env bash
run_opam() {
  if [[ -n "${OPAM_EXE:-}" && -f "$(cygpath -u "$OPAM_EXE" 2>/dev/null || printf '%s' "$OPAM_EXE")" ]]; then
    "$(cygpath -u "$OPAM_EXE" 2>/dev/null || printf '%s' "$OPAM_EXE")" "$@"
  elif command -v opam >/dev/null 2>&1; then
    opam "$@"
  elif command -v opam.exe >/dev/null 2>&1; then
    opam.exe "$@"
  else
    echo "opam is not available on PATH" >&2
    return 127
  fi
}

#!/usr/bin/env bash
# Word-boundary check against SHARD_TOOLS env var.
# Usage: source this file, then: has_group <tool-tag>
has_group() { [[ " ${SHARD_TOOLS} " == *" $1 "* ]]; }

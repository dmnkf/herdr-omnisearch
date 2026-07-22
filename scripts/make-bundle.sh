#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${1:-"$ROOT_DIR/dist/herdr-omnisearch-portable.tar.gz"}
OUT_DIR=$(dirname -- "$OUT")

mkdir -p "$OUT_DIR"
git -C "$ROOT_DIR" archive \
    --format=tar.gz \
    --prefix=herdr-omnisearch/ \
    --output="$OUT" \
    HEAD

printf 'wrote %s\n' "$OUT"

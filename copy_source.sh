#!/usr/bin/env bash

set -euo pipefail

SRC_DIR="${1:-}"

if [[ -z "$SRC_DIR" ]]; then
  echo "Usage: $0 <source_dir>"
  exit 1
fi

# Normalize the path
SRC_DIR="$(realpath "$SRC_DIR")"
CWD="$(pwd)"

# Detect platform
if command -v pbcopy >/dev/null 2>&1; then
  CLIP_CMD="pbcopy"
elif command -v xclip >/dev/null 2>&1; then
  CLIP_CMD="xclip -selection clipboard"
else
  echo "No clipboard utility found (install pbcopy or xclip)."
  exit 1
fi

# Build the content
output=""
while IFS= read -r -d '' file; do
  rel_path="${file#$CWD/}"  # keep src/ in the header
  output+=$'\n'"## $rel_path"$'\n'
  output+="$(cat "$file")"$'\n'
done < <(find "$SRC_DIR" \( -name '*.py' -o -name '*.yaml' -o -name '*.html' -o -name '*.txt' \) \
  ! -path '*/migrations/*' \
  ! -path '*/.*/*' \
  ! -path 'src/playground.py' \
  ! -path '*/media/*' \
  ! -path '*/revel.egg-info/*' \
  -type f -print0)


# Copy to clipboard
printf "%s" "$output" | eval "$CLIP_CMD"

echo "Copied filtered source files from '$SRC_DIR' to clipboard."
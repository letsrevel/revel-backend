#!/usr/bin/env bash
# Check that no Python file exceeds the maximum line count.
# Usage: ./scripts/check-file-length.sh [max_lines]
# Default max_lines: 1000
# We have this script to avoid installing pylint, given that this is the only feature we need that ruff doesn't have.

set -e

MAX_LINES=${1:-1000}
FAILED=0

while IFS= read -r -d '' file; do
    lines=$(wc -l < "$file")
    if [ "$lines" -gt "$MAX_LINES" ]; then
        echo "❌ $file has $lines lines (max: $MAX_LINES)"
        FAILED=1
    fi
done < <(find src -name "*.py" -not -path "*/migrations/*" -print0)

if [ "$FAILED" -eq 0 ]; then
    echo "✅ All files are within the $MAX_LINES line limit."
fi

exit $FAILED

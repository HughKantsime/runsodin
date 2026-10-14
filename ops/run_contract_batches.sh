#!/usr/bin/env bash
# Run the in-process backend suites in file batches with a disposable basetemp per
# batch, so per-test databases never accumulate on disk. Usage:
#   ops/run_contract_batches.sh <output-dir> [batch-size]
set -u
out=${1:?output directory required}
size=${2:-40}
root=$(cd "$(dirname "$0")/.." && pwd)
python=${PYTHON:-$root/.venv-stress/bin/python}
mkdir -p "$out"
cd "$root" || exit 2
git rev-parse HEAD > "$out/source-head.txt"
git status --short >> "$out/source-head.txt"
find tests/test_contracts tests/test_telemetry tests/test_release_control -name 'test_*.py' | sort > "$out/files.txt"
split -l "$size" "$out/files.txt" "$out/batch-"
status=0
for list in "$out"/batch-*; do
  name=$(basename "$list")
  temp=$(mktemp -d "${TMPDIR:-/tmp}/odin-contract-$name.XXXXXX")
  # shellcheck disable=SC2046
  PYTHONPATH="$root/backend:$root" "$python" -m pytest $(cat "$list") -q -p no:cacheprovider --tb=short -rfE \
    --basetemp="$temp/t" --junitxml="$out/$name.xml" > "$out/$name.log" 2>&1
  code=$?
  echo "$name exit=$code $(tail -1 "$out/$name.log")" >> "$out/summary.txt"
  [ "$code" -ne 0 ] && status=1
  rm -rf "$temp"
done
echo "DONE status=$status" >> "$out/summary.txt"
exit "$status"

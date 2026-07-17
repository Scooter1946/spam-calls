#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="${PYTHON:-$repo/.venv/bin/python}"
source_dir="$(mktemp -d /tmp/pitchloop-demo.XXXXXX)"
mkdir -p "$repo/runs"
run_dir="$(mktemp -d "$repo/runs/fake-demo.XXXXXX")"
trap 'rm -rf "$source_dir"' EXIT

git -C "$repo" archive --format=tar --output="$source_dir/release.tar" HEAD
tar -xf "$source_dir/release.tar" -C "$source_dir"

cd "$source_dir"
env \
  ZERO_MODE=fake \
  POLICY_MODE=fake \
  CALL_MODE=fake \
  REPO_MODE=fake \
  EVIDENCE_MODE=local \
  AUTHOR_MODE=fake \
  PITCHLOOP_RUN_DIR="$run_dir" \
  CONFORMANCE_COMMAND="$python -m pytest -q conformance/test_generated_tool.py" \
  "$python" -m agent --spec scenario/run_spec.json --run-dir "$run_dir"
"$python" -m demo.show_timeline --run-dir "$run_dir"
echo "Artifacts: $run_dir"

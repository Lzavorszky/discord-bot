#!/usr/bin/env bash
# check.sh — single health-check entrypoint for the ID Bot rebuild.
#
# Run it TWICE per session: once at the start (confirm prior phases still green),
# once before committing (confirm this session didn't regress anything).
#
# It is INTENTIONALLY tolerant: each block runs only if its target exists yet,
# so the same script works from Phase 0 (almost nothing) through cutover (everything).
# It runs ALL blocks and reports a summary; it does not stop at the first failure.
#
# Keep it OFFLINE and FREE: the regression harness must use recorded/mocked LLM
# responses by default. Live model calls go behind `./check.sh --live` only.
#
# Usage:
#   ./check.sh           # fast, offline, run every session
#   ./check.sh --live    # also run the small live-model smoke set (costs a few cents)

set -uo pipefail
LIVE=0
[[ "${1:-}" == "--live" ]] && LIVE=1

FAILED=()
run () {  # run <label> <command...>
  local label="$1"; shift
  echo "──▶ $label"
  if "$@"; then
    echo "   ✓ $label"
  else
    echo "   ✗ $label"
    FAILED+=("$label")
  fi
  echo
}

echo "=================  ID Bot health check  ================="
echo

# 1. Legacy suite — guards the OLD bot until it is decommissioned (Phase 6).
if compgen -G "test_*.py" > /dev/null; then
  run "legacy tests (test_*.py)" python -m pytest -q test_*.py
fi

# 2. New package unit tests — tools, loader, provider (grows from Phase 1+).
if [[ -d id_bot2/tests ]]; then
  run "id_bot2 unit tests" python -m pytest -q id_bot2/tests
fi

# 3. Protocol schema validation + linter (Phase 2+).
#    Every protocol must load; no duplicate aliases across files; referenced drug_ids exist.
if [[ -f id_bot2/validate_protocols.py ]]; then
  run "protocol schema + linter" python id_bot2/validate_protocols.py
fi

# 4. LLMProvider contract test (Phase 1+) — the seam every provider must satisfy.
if [[ -f id_bot2/tests/test_provider_contract.py ]]; then
  run "LLMProvider contract" python -m pytest -q id_bot2/tests/test_provider_contract.py
fi

# 5. Behavioural regression harness (Phase 0+) — the clinical-quality guard.
#    Reads regression_cases.yaml; asserts route/tool/protocol/output per case.
#    Offline by default (mocked router); prints pass-rate.
if [[ -f id_bot2/run_harness.py ]]; then
  run "regression harness (offline)" python id_bot2/run_harness.py regression_cases.yaml
fi

# 6. Smoke test — does the pipeline start and answer one canned query end to end?
if [[ -f id_bot2/smoke.py ]]; then
  run "smoke test" python id_bot2/smoke.py
fi

# 7. Live model smoke set — only with --live (Phase 3+, per-phase / pre-cutover).
if [[ $LIVE -eq 1 && -f id_bot2/run_harness.py ]]; then
  run "regression harness (LIVE model)" python id_bot2/run_harness.py regression_cases.yaml --live
fi

echo "========================================================="
if [[ ${#FAILED[@]} -eq 0 ]]; then
  echo "ALL GREEN ✓   (record the harness pass-rate in PROGRESS.md, then commit)"
  exit 0
else
  echo "RED ✗ — ${#FAILED[@]} block(s) failed:"
  printf '   - %s\n' "${FAILED[@]}"
  echo "Do NOT commit until green (or update PROGRESS.md with a deliberate, noted exception)."
  exit 1
fi

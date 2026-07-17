# PitchLoop — P1 (agent, Zero, generated-tool lifecycle) bootstrap targets.
# P1 owns the Makefile initially; other roles add ADDITIVE targets for their own
# modules only (global context §6, §12).

PY ?= .venv/bin/python
PYTHON312 ?= python3.12
SPEC ?= scenario/run_spec.json

.PHONY: help setup test test-p1 run run-fake zero-install-proof clean

help:
	@echo "PitchLoop make targets:"
	@echo "  setup               create .venv (Python 3.12) and install deps"
	@echo "  test                run the full test suite"
	@echo "  test-p1             run P1 contract + fake-loop tests"
	@echo "  run                 python -m agent --spec $(SPEC)  (needs P2 scenario + live adapters)"
	@echo "  run-fake            run the whole loop end-to-end against P1 fakes"
	@echo "  zero-install-proof  capture live Zero CLI install proof (needs the CLI)"
	@echo "  clean               remove caches"

setup:
	$(PYTHON312) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

# Preferred P1 proof command (global context "Required commands").
test-p1:
	$(PY) -m pytest -q tests/test_contracts.py tests/test_agent_fake_loop.py

run:
	$(PY) -m agent --spec $(SPEC)

# Self-contained P1 demo: writes a throwaway spec under runs/ (gitignored) and
# drives the loop with every adapter in fake mode.
run-fake:
	@mkdir -p runs
	@printf '%s' '{"run_id":"demo-001","goal":"book_one_qualified_meeting","product":"MigrationGuard","persona":"maya_chen","candidates":["alex_rivera","maya_chen"],"budget_cents":5000,"policy_ref":"northstar/pitch","required_claims":["fact_a","fact_b"],"max_paid_calls":2}' > runs/_p1_demo_spec.json
	$(PY) -m agent --spec runs/_p1_demo_spec.json --run-dir runs/demo-001

zero-install-proof:
	$(PY) -c "from agent.artifacts import Artifacts; from integrations.zero_client import capture_cli_install_proof; print(capture_cli_install_proof(Artifacts()))"

clean:
	rm -rf .pytest_cache **/__pycache__ .ruff_cache

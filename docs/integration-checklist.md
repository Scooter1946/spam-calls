# Integration checklist

Merge only handoffs marked `READY: yes`, in this order.

## P1 — contracts, agent, Zero

- [ ] Run the command in `handoffs/p1.md`.
- [ ] Run `pytest -q tests/test_contracts.py tests/test_agent_fake_loop.py`.
- [ ] Confirm required Zero and authoring environment names are documented without values.

## P2 — scenario and calls

- [ ] Run the command in `handoffs/p2.md`.
- [ ] Run `pytest -q tests/test_pitch.py tests/test_call_adapter.py conformance/test_generated_tool.py`.
- [ ] Confirm fixture, call, and callee phone environment names are documented without values.

## P3 — policy and repository

- [ ] Run the command in `handoffs/p3.md`.
- [ ] Run `pytest -q tests/test_policy_adapter.py tests/test_repo_adapter.py`.
- [ ] Confirm Pomerium and GitHub environment names are documented without values.

## P4 — evidence and release

- [x] Run `pytest -q tests/test_evidence_adapter.py`.
- [ ] Run `python -m demo.prove_nexla` with live Nexla configuration.
- [ ] Save redacted flow identifiers and lineage proof under `docs/lineage/`.

## Release gate

- [ ] Run `pytest -q`.
- [ ] Run `python -m agent --spec scenario/run_spec.json` with all modes fake.
- [ ] Review `python -m demo.show_timeline` on one screen.
- [ ] Scan tracked files for secrets and phone numbers.

## Integration status

- P1 and P3 were merged from their shared-history branches; P2's three isolated commits were replayed onto the release history.
- `pytest -q` passes 88 tests.
- `demo/run_demo.sh` exercises P1 orchestration, P2 pitch/call/conformance, P3 fake policy/repository, and P4 local evidence; it reaches `MEETING_BOOKED` and prints the complete timeline.
- Live sponsor proof remains gated only by the account-backed environment values and services listed above.

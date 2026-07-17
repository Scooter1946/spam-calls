# PitchLoop Pomerium policy proof

This stack uses a Pomerium Zero-managed Core data plane. Pomerium service
accounts are a Zero/Enterprise capability, so the route policies live in the
Zero control plane while the upstream and data plane run locally.

1. Copy `.env.example` to `.env` and set the Zero bootstrap token. To reuse a
   root project environment file instead, set `PITCHLOOP_ENV_FILE=.env` when
   invoking both scripts; relative paths resolve from the repository root.
2. Create one Zero service account whose user ID is `pitchloop-agent`. Store
   its JWT only in the local `.env` as `POMERIUM_SERVICE_ACCOUNT_TOKEN`.
3. Create both routes in `route-blueprint.yaml` in the same cluster. Both point
   to `http://policy-target:8080` and both receive the same service account.
   The deny policy deliberately has a matching `deny` rule; Pomerium gives deny
   precedence. The allow policy contains only the matching allow rule.
4. Set both public URLs in `.env`, start the stack, and run the probe:

   ```bash
   bash infra/scripts/start_policy_stack.sh
   bash infra/scripts/probe_policy.sh
   ```

   Or, with a single root environment file:

   ```bash
   PITCHLOOP_ENV_FILE=.env bash infra/scripts/start_policy_stack.sh
   PITCHLOOP_ENV_FILE=.env bash infra/scripts/probe_policy.sh
   ```

The policy target publishes no host port and performs no consent check. A 403
from the denied route therefore happens before the upstream; a 200 response
whose JSON contains `reached_upstream: true` proves the allowed request was
proxied. Pomerium's documented service-account header form is
`Authorization: Bearer Pomerium-<JWT>`.

Primary references:

- https://www.pomerium.com/docs/get-started/quickstart
- https://www.pomerium.com/docs/capabilities/service-accounts
- https://www.pomerium.com/docs/internals/ppl

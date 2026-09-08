# GitHub Actions secrets

This file records secret names and their purpose only. Secret values must be added through GitHub and must never be committed here.

| Secret | Used by | Status |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Enkii review and production release-note summarization | To be added |
| `PROMOTION_TOKEN` | Protected branch promotions and Fastlane back-merge PRs | To be added |
| `GITLEAKS_LICENSE` | Gitleaks scan if the action requires licensing | To be confirmed |
| `DEPLOYMENT_TOKEN` | Future hosted Foundry deployment workflow | Not used yet |
| `RAILWAY_TOKEN` | Publish Allies Runtime Images workflow | Required at repository scope or in both Foundry GitHub environments |

The promotion credential should be a narrowly scoped GitHub App or fine-grained repository token with only the permissions required to update promotion branches and open the Fastlane back-merge PR.

## Repository variables

| Variable | Used by | Status |
| --- | --- | --- |
| `STAGING_URL` | Public HTTPS staging readiness verification on pushes to `staging` | Required for the push-triggered gate; set to the public base URL, for example `https://staging.example.com` |
| `RAILWAY_PROJECT_ID` | Foundry Railway project selected by the image publishing workflow | Required at repository scope |

The verifier also supports a manual `workflow_dispatch` URL input. It requires
an `https://` URL and checks `/healthz` until the service returns HTTP 200 with
`{"status":"ok"}`.

The **Publish Allies Runtime Images** workflow builds and publishes the Hermes
and runtime images from one commit. Select `staging` or `production` to update
that Railway environment's shared `HERMES_IMAGE` and `RUNTIME_IMAGE` values as
one CLI operation. Configure production reviewers on the
`foundry / production` GitHub environment. Selecting `none` only publishes the
images. Railway redeploys services that reference the changed shared values;
existing Fly machines reconcile to the pair through Foundry's normal image
update flow.

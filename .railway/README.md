# Staging deployment

`.railway/railway.ts` owns Foundry staging's existing API, event publisher and database/volume configuration, plus a dedicated readiness publisher. Existing values are preserved; the new publisher references the API's database and Cloud delivery credentials. It has no public endpoint and no Fly credentials. The definition rejects environments other than staging.

Install the pinned tooling with `npm ci`. `railway config plan` previews changes; the initial verified preview creates only Readiness Publisher. This PR does not apply infrastructure.

Configure the GitHub repository secret `RAILWAY_STAGING_TOKEN` once with a Railway project token scoped to staging. A PR targeting staging that changes infrastructure runs `.github/workflows/staging-infrastructure.yml`: it publishes a pinned plan for review, then applies that exact plan after merge. Same-repository PRs only receive the token. Destructive applies are disabled. Drift or a changed configuration tree fails closed; reopen/re-plan the promotion before merging. Keep the plan check required for infrastructure promotions. The workflow does not apply on merges into dev.

After the initial setup, Railway's staging GitHub source deploys the publisher alongside subsequent staging code pushes. The managed start command uses the existing one-second watch default; nobody needs to run a command after each deployment. Deploy compatible Cloud receipt handling before promoting Foundry. Existing explicit feature overrides still win over new defaults. Existing Fly runtimes require their normal image/configuration reconciliation.

Backend tuning defaults to activity waits on, 16 web threads, five-second waits and eight waiters per process. Hints enable when the Cloud URL and token exist. Keep ready-pool size explicitly controlled; idle stopping remains off. Do not apply this staging snapshot to production.

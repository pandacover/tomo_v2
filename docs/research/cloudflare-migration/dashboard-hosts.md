# Compare Cloudflare and Vercel for the Tomo dashboard

Research snapshot: 2026-08-23. This compares the current working dashboard, including its uncommitted UI changes, against current first-party platform documentation. It does not select a host.

## Decision in one paragraph

The dashboard is a full-stack Next.js 16.2.10 application, so **Cloudflare Pages static hosting is not a candidate without removing the dashboard's server behavior**. The actual comparison is **Cloudflare Workers via `@opennextjs/cloudflare`** versus **Vercel's native Next.js platform**. Both can run the dashboard's App Router, React Server Components, SSR, and Route Handlers. Vercel has the smaller framework-compatibility delta and full Node.js coverage; Cloudflare requires the OpenNext adapter and a production-runtime proof. Neither host can retain the current file-backed auth database: both have ephemeral function filesystems, so auth persistence must move. Cloudflare can put Better Auth on D1 and later bind the dashboard directly to a Cloudflare control Worker, keeping the existing $5 Workers Paid account as the likely cost floor. Vercel can deploy the Next.js surface with less adaptation, but a professional/internal workload should not assume eligibility for the personal, non-commercial Hobby plan; Pro starts at $20/month and a durable database or Cloudflare-hosted auth service is still required.

## What the dashboard actually requires

| Concern | Current evidence | Hosting consequence |
| --- | --- | --- |
| Framework | The package manifest pins Next.js 16.2.10, React 19.2.4, Better Auth 1.6.23, and Better Auth Infra 0.3.6 ([`dashboard/package.json`](../../../dashboard/package.json#L12-L19)). | Compare current Next 16 behavior, not a generic React SPA. |
| Server rendering | `/tomos` is an async Server Component that reads request headers, validates the Better Auth session, redirects unauthenticated users, and fetches initial relationship data ([`dashboard/src/app/tomos/page.tsx`](../../../dashboard/src/app/tomos/page.tsx#L9-L15)). | Requires dynamic server execution. A static export cannot preserve it. |
| API surface | Better Auth is mounted as a `GET`/`POST` catch-all Route Handler ([`dashboard/src/app/api/auth/[...all]/route.ts`](../../../dashboard/src/app/api/auth/%5B...all%5D/route.ts#L1-L9)). The peers handler exposes authenticated `GET`, `PUT`, `POST`, and `DELETE` mutations ([`dashboard/src/app/api/peers/route.ts`](../../../dashboard/src/app/api/peers/route.ts#L17-L50)); the relationship handler adds dynamic authenticated reads and mutations ([`dashboard/src/app/api/peers/[relationshipId]/route.ts`](../../../dashboard/src/app/api/peers/%5BrelationshipId%5D/route.ts#L19-L36)). | Requires Route Handlers, request cookies/headers, and mutation support. |
| Server Actions | No `"use server"` directive or Server Action was found in the current dashboard. Client mutations call same-origin Route Handlers instead. | Server Actions need not block this migration, though both candidates support them if later introduced. |
| Auth | Better Auth uses email/password accounts, session lookup, reset-email delivery through Better Auth Infra, and a trusted-origin/base-URL tied to the deployed dashboard ([`dashboard/src/lib/auth.ts`](../../../dashboard/src/lib/auth.ts#L31-L65)). | Auth stays server-side and needs durable account/session storage plus deployment-specific origin configuration. |
| Auth database | Startup creates a local directory, opens `bun:sqlite` or `node:sqlite`, and runs schema migrations against a path-based database ([`dashboard/src/lib/auth.ts`](../../../dashboard/src/lib/auth.ts#L18-L29), [`dashboard/src/lib/auth.ts`](../../../dashboard/src/lib/auth.ts#L67-L75)). The path defaults to `.tomo_dashboard/auth.sqlite` ([`dashboard/src/lib/env.ts`](../../../dashboard/src/lib/env.ts#L3-L15)). | This is the mandatory change on either serverless candidate; a local SQLite file is not durable there. |
| Runtime | No route declares an Edge runtime and `next.config.ts` only enables React Compiler and an image remote ([`dashboard/next.config.ts`](../../../dashboard/next.config.ts#L1-L15)). Next.js defaults route segments to the Node.js runtime ([Next.js runtime reference](https://nextjs.org/docs/app/api-reference/file-conventions/route-segment-config#runtime)). | Vercel can use its Node.js runtime directly. Cloudflare must provide the Node-compatible surface through Workers/OpenNext. |
| Control backend | The dashboard calls a configurable control origin with a shared `x-api-key`; it does not hard-code a Railway hostname ([`dashboard/src/lib/peer-client.ts`](../../../dashboard/src/lib/peer-client.ts#L10-L15), [`dashboard/src/app/api/onboarding/telegram/route.ts`](../../../dashboard/src/app/api/onboarding/telegram/route.ts#L15-L22)). | Either host can initially call a public Cloudflare control URL. Cloudflare hosting additionally permits a private Service Binding later. |

The dashboard is therefore not a viable static export: Next.js documents that static export cannot support request-dependent Route Handlers, cookies, redirects, or Server Actions ([Next.js static-export limitations](https://nextjs.org/docs/app/guides/static-exports#unsupported-features)). Cloudflare likewise directs full-stack SSR Next.js applications to Workers and reserves Pages for static exports ([Cloudflare Next.js Pages guidance](https://developers.cloudflare.com/pages/framework-guides/nextjs/)).

## Railway coupling to remove

The direct deployment coupling is narrow:

- `dashboard/railway.toml` installs/builds with Bun, starts `next start` on Railway's `$PORT`, and defines `/` as its health check ([`dashboard/railway.toml`](../../../dashboard/railway.toml#L1-L8)). This file becomes obsolete for either candidate.
- `RAILWAY_PUBLIC_DOMAIN` is only a fallback used to derive `BETTER_AUTH_URL`; an explicit `BETTER_AUTH_URL` already overrides it ([`dashboard/src/lib/env.ts`](../../../dashboard/src/lib/env.ts#L5-L15)). Replace the Railway fallback and explicitly configure the canonical dashboard URL.
- The durable local auth file is an architectural Railway coupling even though its path is platform-neutral. It assumes a persistent mounted filesystem.
- `TOMO_CONTROL_API_URL` and `TOMO_CONTROL_API_KEY` are portable already. They identify the control service, not Railway itself.

No deployment-relevant source file above was among the uncommitted UI edits visible during this review.

## Candidate A: Cloudflare Workers through OpenNext

### Compatibility

Cloudflare's supported path deploys Next.js to Workers through the OpenNext adapter. Cloudflare lists App Router, Route Handlers, React Server Components, SSR, Server Actions, and response streaming as supported; Node.js Middleware is the named unsupported feature ([Cloudflare's Next.js feature matrix](https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/#nextjs-supported-features)). This dashboard has no middleware/proxy file, so that named gap does not currently apply. OpenNext says all Next.js 16 minor and patch versions are supported, which includes 16.2.10 ([OpenNext Cloudflare compatibility](https://opennext.js.org/cloudflare#supported-nextjs-versions)).

There is still more compatibility risk than on Vercel. Next.js classifies Vercel as a verified adapter while Cloudflare's integration is not yet built on Next.js's verified Adapter API, so support may vary by feature ([Next.js deployment-adapter status](https://nextjs.org/docs/app/getting-started/deploying#adapters)). Cloudflare also explicitly distinguishes local `next dev` on Node.js from production `workerd` and tells users to test with the adapter's preview command ([Cloudflare preview guidance](https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/#deploy-an-existing-nextjs-project-on-workers)). A successful `next build` alone would not retire this risk.

Required framework work:

1. Add `@opennextjs/cloudflare` and Wrangler, generate Worker/OpenNext configuration, replace Railway scripts, and configure production/preview secrets. Cloudflare can auto-detect an existing Next.js project, but the generated files should be committed and reviewed ([Cloudflare existing-project deployment](https://developers.cloudflare.com/workers/framework-guides/web-apps/nextjs/#deploy-an-existing-nextjs-project-on-workers)).
2. Run all auth, reset-password, onboarding, peers, and authenticated SSR paths under the `workerd` preview, not only `next dev`.
3. Measure the compressed Worker bundle and runtime memory. Workers Paid limits a Worker to 10 MB compressed and 128 MB memory, with a default 30-second CPU limit configurable up to five minutes ([Workers limits](https://developers.cloudflare.com/workers/platform/limits/)). The current dependency set has not been bundled through OpenNext yet.
4. Verify `@better-auth/infra`'s `dash()` plugin and email sender in `workerd`; its presence is visible in the auth construction ([`dashboard/src/lib/auth.ts`](../../../dashboard/src/lib/auth.ts#L31-L64)), but no first-party compatibility guarantee for that plugin was found.

### Auth persistence

The current filesystem code must be replaced, not redirected to `/tmp`. Cloudflare's Workers filesystem is memory-backed and its writable `/tmp` is unique to one request and non-persistent ([Cloudflare Workers `node:fs`](https://developers.cloudflare.com/workers/runtime-apis/nodejs/fs/)).

D1 is the native replacement to evaluate. Better Auth 1.5 introduced first-class D1 support by accepting the D1 binding directly, and the deployed version is newer at 1.6.23 ([Better Auth D1 support](https://better-auth.com/blog/1-5#cloudflare-d1-support)). Better Auth also supports programmatic migrations in Workers/serverless environments with the built-in D1/Kysely adapter ([Better Auth database migrations](https://better-auth.com/docs/concepts/database#programmatic-migrations)). The rewrite would:

- remove `fs.mkdirSync`, `node:path`, and path-based `node:sqlite`/`bun:sqlite` opening;
- construct auth with a D1 binding available in the Worker request context;
- decide whether schema migration happens as an explicit deployment step or an idempotent guarded startup path;
- keep the existing Better Auth handlers and page-level session checks after runtime verification.

Because this effort allows an empty-state relaunch, no user-row migration is required; durable schema creation is still required.

### Integration with the Cloudflare control backend

The smallest first deployment can retain the present `fetch(controlApiUrl)` plus `x-api-key` contract. If the control backend becomes another Worker on the same account, a later change can replace the public hop with a Service Binding. Cloudflare Service Bindings can invoke another Worker without a public URL, add no service-to-service request charge, and can keep the downstream service off the public Internet ([Cloudflare Service Bindings](https://developers.cloudflare.com/workers/runtime-apis/bindings/service-bindings/)). That would require changing the current URL-based client boundary; it is not automatic simply because both services use Cloudflare.

### Cost and operations

Assuming the user's $5 subscription is specifically **Workers Paid** (Cloudflare says this is separate from other Cloudflare plans), it is an account-wide $5 minimum that includes 10 million Worker requests and 30 million CPU milliseconds per month; static-asset requests are free ([Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/#workers)). D1 Paid includes 25 billion rows read, 50 million rows written, and 5 GB storage per month before overage ([D1 pricing](https://developers.cloudflare.com/workers/platform/pricing/#d1)). Three internal application users should be tiny relative to those allocations, but the $5 ceiling cannot be guaranteed until dashboard and backend usage are budgeted together because the included Worker allocation is shared at account level and overages are enabled.

Workers Builds provides GitHub/GitLab deployments and preview versions. Paid plans include 6,000 build minutes per month, six concurrent builds, and a 20-minute build timeout ([Workers Builds limits](https://developers.cloudflare.com/workers/ci-cd/builds/limits-and-pricing/)). Cloudflare supports version preview URLs and code rollbacks; bindings/data are separate resources, so rolling code back does not roll a D1 schema back ([Workers rollbacks](https://developers.cloudflare.com/workers/versions-and-deployments/rollbacks/)). This is a single-vendor operational surface if the backend also lands on Cloudflare, but OpenNext configuration and `workerd` preview testing become permanent release responsibilities.

## Candidate B: Vercel

### Compatibility

Vercel is the native, zero-configuration Next.js target and server-renders Next.js through Vercel Functions ([Next.js on Vercel](https://vercel.com/docs/frameworks/full-stack/nextjs)). Its Node.js functions provide full Node.js API coverage, and Next.js is a verified adapter on Vercel ([Vercel Function limits](https://vercel.com/docs/functions/limitations/), [Next.js deployment-adapter status](https://nextjs.org/docs/app/getting-started/deploying#adapters)). That removes the OpenNext transformation and makes the current route/page model the lower-risk framework deployment.

Required framework work is correspondingly small: remove Railway deployment configuration, connect the repository, set the project root to `dashboard`, and configure production/preview environment variables. Vercel automatically creates branch and commit preview deployments and provides instant rollback by reassigning domains to a previous immutable deployment ([Vercel environments](https://vercel.com/docs/deployments/environments), [Vercel deployment promotion and rollback](https://vercel.com/docs/deployments/promoting-a-deployment#instant-rollback)).

### Auth persistence

Vercel does **not** remove the mandatory database rewrite. Vercel Functions have a read-only filesystem with only 500 MB of writable `/tmp` scratch space ([Vercel runtimes](https://vercel.com/docs/functions/runtimes#file-system-support)), and Vercel explicitly says persistent SQLite cannot be used on the platform ([Vercel SQLite guidance](https://vercel.com/kb/guide/is-sqlite-supported-in-vercel)).

The durable-auth choices are therefore:

1. use a Vercel Marketplace database such as Neon/Postgres or Turso/libSQL and change Better Auth's database adapter; Marketplace storage is provided by external vendors and has its own selected pricing plan ([Vercel Marketplace storage](https://vercel.com/docs/marketplace-storage)); or
2. move Better Auth and its D1 binding into a Cloudflare Worker, then proxy or call that auth service from the Vercel dashboard. This preserves Cloudflare storage but is a larger change to the current same-origin Better Auth route, cookie/origin handling, and failure boundary.

A Vercel function cannot receive a native Cloudflare D1 binding. That is an architectural inference from D1 being injected as a Worker binding in Better Auth's first-party example, not a separately documented Vercel limitation. A public auth API in front of D1 would be needed for cross-vendor access.

### Integration with the Cloudflare control backend

The existing HTTPS `TOMO_CONTROL_API_URL` plus secret header maps directly to Vercel, so no control-client rewrite is required. Unlike the Cloudflare-to-Cloudflare case, the call remains a public cross-vendor network hop and cannot use a Worker Service Binding. Operationally, deployments, secrets, logs, incidents, and spend stay split across Vercel and Cloudflare.

### Cost and operations

Vercel Hobby is $0 but is restricted to personal, non-commercial use. Pro is intended for professional/internal work and has a $20/month platform fee including one deploying seat and $20 of usage credit; each additional Owner or Member who can deploy is another $20/month, while Viewer seats are free ([Vercel plan eligibility and pricing](https://vercel.com/pricing), [Vercel Pro seats](https://vercel.com/docs/plans/pro-plan#team-seats)). The three Tomo application users do **not** create Vercel seat charges. Only people who need Vercel deployment/configuration permissions do.

Consequently, Vercel meets the $5 target only if the owner confirms the workload is eligible for Hobby **and** the chosen auth database remains free. If this is a professional or commercial internal service, the Vercel platform fee alone exceeds the target before database costs.

## Side-by-side decision surface

| Criterion | Cloudflare Workers/OpenNext | Vercel |
| --- | --- | --- |
| Current Next 16 pages/routes | Supported by Cloudflare/OpenNext, but adapter-runtime proof required | Native/verified Next.js target; least framework adaptation |
| Cloudflare Pages | Not applicable: static Pages cannot preserve current auth/SSR/mutations | N/A |
| Current local SQLite | Must replace | Must replace |
| Natural auth store | D1 binding; first-party Better Auth support | External Marketplace DB, or relocate auth to Cloudflare |
| Control-backend integration | Existing public fetch works; private zero-extra-request Service Binding becomes possible | Existing public fetch works; remains cross-vendor HTTPS |
| Likely platform floor | Existing $5 Workers Paid account, assuming that is the subscription the owner has | $0 only if Hobby eligibility is confirmed; otherwise $20/month Pro before DB |
| Three app users | Far below listed Worker/D1 allocations in ordinary dashboard usage; validate shared account usage | App users are not seats; deployers determine Pro seat cost |
| Release operations | OpenNext + Wrangler + `workerd` preview; Cloudflare previews/logs/rollback | Zero-config Next integration; strong previews and instant rollback |
| Vendor-consolidation goal | Dashboard, database, and backend can share one platform/account | Keeps a second production vendor by design |
| Principal unresolved risk | Exact Next 16.2.10 + Better Auth Infra + D1 bundle/runtime compatibility | Hobby eligibility and where durable auth lives without adding cost/vendor complexity |

## Decisions now exposed

These are the decisions this research makes sharp enough to ticket; none is answered by choosing a logo on the hosting screen:

1. **Set the durable auth boundary.** Choose between Better Auth+D1 inside the dashboard Worker, Better Auth as a separate Cloudflare auth service, or an external database reached from Vercel. This determines the largest source change and failure boundary.
2. **Classify Vercel Hobby eligibility.** Decide whether this internal Tomo service is personal/non-commercial under Vercel's terms. If not, Vercel cannot satisfy the $5/month destination and can be ruled out on cost before prototyping.
3. **Prove Cloudflare runtime compatibility.** Build a throwaway deployment of the exact dashboard dependency set with OpenNext, D1-backed Better Auth, Better Auth Infra, authenticated SSR, all route methods, password-reset sending, and control API calls. Record compressed bundle size, runtime errors, and `workerd` test results.
4. **Choose the dashboard-to-control trust channel.** Keep the current public HTTPS endpoint and shared API key for the first release, or change both sides to a private Cloudflare Service Binding. The latter is only available if the control backend becomes a Worker-compatible service.
5. **Define preview-origin auth policy.** The current auth configuration trusts one `BETTER_AUTH_URL` ([`dashboard/src/lib/auth.ts`](../../../dashboard/src/lib/auth.ts#L38-L64)). Decide whether preview deployments support sign-in/reset testing, and if so how their generated origins and secrets are admitted without weakening production origin checks.
6. **Set a shared Cloudflare cost guardrail.** Confirm the subscription is Workers Paid, inventory the backend services that will share its request/CPU/storage allocations, then configure spending/CPU limits and alerts. Three users make overage unlikely; they do not make it impossible by definition.

## Remaining fog

- The control backend's eventual Cloudflare shape is not yet settled. Service Binding feasibility depends on whether it becomes a Worker service, a Container fronted by a Worker, or something else.
- Better Auth Infra's `dash()` and reset-email behavior has no located first-party `workerd` support statement; only an actual preview can clear that risk.
- Total cost cannot be calculated from user count alone. Request rate, SSR frequency, auth query shape, and the backend's own CPU/storage design still need measurement.
- Empty-state relaunch removes data-copy work, but the production schema migration and rollback policy remains coupled to the auth-boundary decision.

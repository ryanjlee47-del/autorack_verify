# Deploying Autorack

Three pieces, as in the design doc:

| Piece | Host | Notes |
|---|---|---|
| Database | **Neon** Postgres | Use the *pooled* connection string. Point-in-time restore covers backups. |
| API | **Render**, **Railway** or **Fly.io** | Docker image from `Dockerfile`; runs migrations on start. |
| Frontend | **Cloudflare Pages** | Static files in `frontend/`; `build.sh` points them at the API. |

A single-host setup also works: run the Docker image with `SERVE_FRONTEND=true`
and skip Pages.

Suggested domains: `app.yourdomain.com` (Pages) and `api.yourdomain.com` (API).

## 1. Neon

1. Create a project and database. Copy the **pooled** connection string
   (`...-pooler...neon.tech/...?sslmode=require`).
2. Nothing else. Migrations run when the API starts.

## 2. API (pick one host)

Required environment variables (full list with comments: `backend/.env.example`):

| Variable | Value |
|---|---|
| `ENVIRONMENT` | `production` (set by the Dockerfile) |
| `DATABASE_URL` | Neon pooled URL |
| `SECRET_KEY` | 32+ random chars. **Keep it forever**: it keys worker PIN lookups. |
| `FRONTEND_URL` | `https://app.yourdomain.com` (links in emails and QR codes) |
| `CORS_ORIGINS` | `https://app.yourdomain.com` |
| `EMAIL_BACKEND` | `resend` or `smtp` (+ `RESEND_API_KEY` or `SMTP_*`) |
| `EMAIL_FROM` | `Autorack <login@yourdomain.com>` (a verified sender) |
| `STRIPE_*` | optional until you charge; see step 4 |

The process refuses to start if a production setting is unsafe (dev secret,
console email, non-HTTPS frontend). Check a config without deploying:
`python -m autorack.cli check-config`.

**Render:** New → Blueprint → this repo (`render.yaml`). Fill in the env vars.
**Railway:** New project → Deploy from repo (`railway.json`). Add the env vars.
**Fly.io:** `fly launch --no-deploy` (uses `fly.toml`), `fly secrets set DATABASE_URL=... SECRET_KEY=... ...`, `fly deploy`.

Health check: `GET /api/health`.

## 3. Cloudflare Pages

1. Create a Pages project from this repo.
2. Build command: `sh frontend/build.sh`. Build output directory: `frontend`.
3. Environment variable: `AUTORACK_API_BASE=https://api.yourdomain.com`.
4. Add the custom domain `app.yourdomain.com`.

`build.sh` writes `config.js` (the API base) and adds the API origin to the
Content-Security-Policy in `_headers`. The worker app is at `/w/`, the
dashboard at `/app/`.

## 4. Stripe (when pilots end)

1. Create a Product "Autorack" with two recurring prices: **$29 monthly** and
   **$290 yearly**. Copy the monthly price id into `STRIPE_PRICE_ID` and the
   yearly one into `STRIPE_ANNUAL_PRICE_ID`. (If you leave the yearly id empty,
   checkout builds the yearly price on the same product itself.)
2. Set `STRIPE_SECRET_KEY`.
3. Add a webhook endpoint `https://api.yourdomain.com/api/webhooks/stripe` with
   events: `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `customer.subscription.paused`, `customer.subscription.resumed`,
   `invoice.paid`, `invoice.payment_failed`. Copy its signing secret into
   `STRIPE_WEBHOOK_SECRET`.
4. In Stripe's Customer Portal settings, allow updating payment methods,
   viewing invoices and cancelling. Under **Subscriptions → Customers can
   switch plans**, add both prices, so owners can move between monthly and
   yearly (the dashboard points them to "Update plan").

The yearly plan is what the landing page and billing page show first; owners
can switch the view to monthly.

**Founding customers.** While `FOUNDING_OFFER_OPEN=true` (the default), every
new warehouse records today's prices, and checkout charges those for as long
as it stays subscribed, even after you raise `PLAN_PRICE_CENTS` /
`PLAN_ANNUAL_PRICE_CENTS`. A subscription that is cancelled and runs out loses
the lock. Warehouses that already existed when this shipped are founding
customers at $29 / $290. To end the offer, set `FOUNDING_OFFER_OPEN=false`
and remove the "Founding price" wording from `index.html`, the signup page
and `help.html`. Existing founders keep their price.

To raise prices: create new Stripe prices, update `STRIPE_PRICE_ID` /
`STRIPE_ANNUAL_PRICE_ID` and the two `PLAN_*_CENTS` values, and the landing
page. Existing Stripe subscriptions stay on the price they started with.
A test fails if the landing page and the constants disagree.

**Support messages.** "Help & support" in the dashboard emails
`SUPPORT_EMAIL` (or `OPERATOR_EMAILS` if that's empty), with Reply-To set to
the customer, so you answer by replying. The public help center is
`/help.html`.

## 5. Email

Email carries invitations, alerts, summaries and reports (sign-in is with Google, section 10).
[Resend](https://resend.com) is simplest: verify your domain, create an API
key, set `EMAIL_BACKEND=resend`. Any SMTP relay (Postmark, SES, Mailgun) works
with `EMAIL_BACKEND=smtp`.

## 6. Scheduled emails and housekeeping

The API sends the daily summary, instant alerts, and trial/payment emails from
a background loop that runs every minute (`JOBS_ENABLED=true`, the default). It
also prunes expired sign-in codes and sessions hourly. Every email is recorded
once in `notifications_sent`, so running the jobs twice never double-sends.

**Free hosts that sleep when idle (Render free)** don't run the loop while
asleep. Set `CRON_SECRET` to a random string and have a free external
scheduler (cron-job.org, or a GitHub Actions `schedule`) call, every 10-15 min:

```
curl -X POST https://YOUR-API/api/cron/run -H "X-Cron-Secret: $CRON_SECRET"
```

That wakes the service and runs anything due. The route is hidden (404) while
`CRON_SECRET` is empty. You can also run `python -m autorack.cli run-jobs`.

## 7. Operator console

Set `OPERATOR_EMAILS` (comma-separated) to your own address(es). Sign in at
`/app/login.html` with one of them; you'll land on `/admin/` (or see an
"Operator console" link in the dashboard if you also run a warehouse). The
first sign-in creates the account automatically.

## Checklist before real customers

- [ ] HTTPS on both domains; phones' cameras require it.
- [ ] A test sign-in email arrives (check spam placement).
- [ ] Link a real phone, scan with the camera and with a Bluetooth scanner.
- [ ] Airplane-mode test: open an order, go offline, scan, reconnect, watch it sync.
- [ ] Stripe test mode end-to-end: subscribe, fail a payment (card `4000 0000 0000 0341`), cancel.
- [ ] `pip-audit -r backend/requirements.txt` clean.
- [ ] Neon point-in-time restore enabled; you know how to use it.

## 8. Knowing when it breaks

**Error alerts (built in).** Every server crash, failed background job and
JavaScript error on the dashboard, phones or operator console is recorded
and emailed to `OPERATOR_EMAILS` within about a minute — one digest, at most
hourly for an error that keeps happening. See and resolve them in the
operator console → **Errors**. Nothing to set up beyond `OPERATOR_EMAILS`.

**Uptime monitoring (5 minutes, free).** Error alerts can't tell you the site
is completely down. Use UptimeRobot (uptimerobot.com, free plan):

1. Sign up, then **Add New Monitor** → type **HTTP(s)**.
2. URL: `https://YOUR-APP.onrender.com/api/health`, interval **5 minutes**.
3. Alert contacts: your email, and add the free mobile app for push alerts.

`/api/health` answers 200 only when the app *and* the database respond, and
reports `jobs_last_run` (when the scheduled emails last ran). A side effect on
Render's free plan: the 5-minute check keeps the service from sleeping, so
the job loop keeps running too. (It uses about 720 of the 750 free hours a
month for one service.)

**Sentry (optional).** For richer crash reports (stack traces with context,
release tracking), create a free project at sentry.io (platform: FastAPI),
and set `SENTRY_DSN` on Render. Personal data (emails, IPs) isn't sent.

## 9. Store connections and auto-import

**Store connections (Shopify, ShipStation, WooCommerce, Google Sheets)** need
nothing on the server: each owner connects their own store on the dashboard →
**Connections**, with keys from their store (the page walks them through it).
Keys are encrypted with a key derived from `SECRET_KEY`. If you ever rotate
`SECRET_KEY`, every connection shows "Reconnect" and owners paste their keys
again. The job runner (section 6) pulls orders every 10 minutes and sends
tracking back after the label is scanned, so it must be running.

**Import by email (optional).** Owners get an address like
`orders+<secret>@…`. A CSV attached to an email sent there is imported.
Set it up once with Postmark (postmarkapp.com, free for 100 emails a month):

1. Create a server → **Default Inbound Stream**. Copy its inbound address,
   e.g. `abc123@inbound.postmarkapp.com`.
2. Webhook URL: `https://YOUR-API/api/inbound/email?key=<a long random secret>`.
   Leave "Include raw email content" off.
3. On Render set `INBOUND_EMAIL_ADDRESS` to the inbound address with `+{token}`
   before the @ (`abc123+{token}@inbound.postmarkapp.com`) and
   `INBOUND_EMAIL_SECRET` to the same secret as in the webhook URL.

Mailgun Routes and SendGrid Inbound Parse also work (same URL). They post
multipart forms, which Autorack reads too.

**CSV drop URL and watched folder.** No setup needed. If the API isn't on the
same host as `FRONTEND_URL` (Cloudflare Pages + Render), set `API_PUBLIC_URL`
to the API's address (`https://YOUR-APP.onrender.com`) so the drop URL points
to the right place.

## 10. Sign in with Google (required)

Owners, managers, supervisors and you (the operator) sign in only with Google.
Workers on phones still use their PIN.

1. Google Cloud Console → create a project (e.g. "Autorack") → **APIs & Services
   → OAuth consent screen**: External, app name "Autorack", your support email,
   scopes `openid`, `email`, `profile` (no sensitive scopes, so no Google review).
   Publish the app (status "In production").
2. **Credentials → Create credentials → OAuth client ID** → Web application.
   - Authorized redirect URI: `https://YOUR-API/api/auth/google/callback`
     (the API's address: `API_PUBLIC_URL`, or `FRONTEND_URL` if the API serves
     the site too).
3. On Render set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. The server
   refuses to start in production without them.

People whose work email isn't Gmail/Google Workspace can create a free Google
account for their existing address at accounts.google.com/signup ("use my
current email address instead"). An account is locked to the Google account
that first signs in with it; if someone changes Google accounts, clear
`users.google_sub` for them (or re-invite a new address).

For support and local development, `python -m autorack.cli login-link --email
someone@example.com` still prints a one-time sign-in link.

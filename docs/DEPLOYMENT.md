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

1. Create a Product "Autorack" with a **recurring monthly price of $175**.
   Copy the price id into `STRIPE_PRICE_ID`.
2. Set `STRIPE_SECRET_KEY`.
3. Add a webhook endpoint `https://api.yourdomain.com/api/webhooks/stripe` with
   events: `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `customer.subscription.paused`, `customer.subscription.resumed`,
   `invoice.paid`, `invoice.payment_failed`. Copy its signing secret into
   `STRIPE_WEBHOOK_SECRET`.
4. In Stripe's Customer Portal settings, allow updating payment methods,
   viewing invoices and cancelling.

If you change the price, also change `PLAN_PRICE_CENTS` and the landing page.
A test fails if the landing page and the constant disagree.

## 5. Email

Magic links are the only way owners sign in, so email must work.
[Resend](https://resend.com) is simplest: verify your domain, create an API
key, set `EMAIL_BACKEND=resend`. Any SMTP relay (Postmark, SES, Mailgun) works
with `EMAIL_BACKEND=smtp`.

## 6. Daily housekeeping

Schedule `python -m autorack.cli prune` once a day (Render cron job, Railway
cron, or `fly machine run --schedule daily`). It deletes expired sign-in links,
old sessions and rate-limit rows. Nothing breaks if it doesn't run; tables just grow.

## Checklist before real customers

- [ ] HTTPS on both domains; phones' cameras require it.
- [ ] A test sign-in email arrives (check spam placement).
- [ ] Link a real phone, scan with the camera and with a Bluetooth scanner.
- [ ] Airplane-mode test: open an order, go offline, scan, reconnect, watch it sync.
- [ ] Stripe test mode end-to-end: subscribe, fail a payment (card `4000 0000 0000 0341`), cancel.
- [ ] `pip-audit -r backend/requirements.txt` clean.
- [ ] Neon point-in-time restore enabled; you know how to use it.

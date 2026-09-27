# Incident response

What to do if customer data may have been exposed, altered or lost, or
Autorack is down. Keep it short and calm; write things down as you go.

> Not legal advice. Breach-notification duties depend on what data, whose,
> and where they live (California: Civil Code §1798.82; other states and
> countries differ). Call your lawyer early in any incident involving
> personal information.

## Severity

| Level | Examples | Response |
|---|---|---|
| **SEV1** | Someone outside a warehouse could read or change its data; a leaked `SECRET_KEY`, database URL, Stripe or Resend key; data deleted without backup | Drop everything. Contain within the hour. Lawyer same day. |
| **SEV2** | A bug shows one warehouse's data to another warehouse's user; scanning down for everyone | Contain today. Notify affected customers. |
| **SEV3** | One customer can't scan; emails delayed; a vulnerability report with no sign of use | Fix normally; reply to reporter. |

## 1. Contain (first hour)

Stop it getting worse before working out what happened.

- **Leaked secret** — rotate it in Render → Environment, then redeploy:
  - Database password: reset in Neon, update `DATABASE_URL`.
  - `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET`: roll in the Stripe dashboard.
  - `RESEND_API_KEY`: revoke and recreate in Resend.
  - `CRON_SECRET`: change it here and in cron-job.org.
  - `SECRET_KEY`: **think first.** It keys worker PIN lookups; changing it
    makes every PIN stop working until each worker's PIN is reset. Only rotate
    it if it actually leaked, then reset PINs (Workers → Reset PIN).
- **Sign everyone out** (from a Render shell, in `backend/`):
  - `python -m autorack.cli revoke-sessions --all` for every dashboard user, or
    `revoke-sessions owner@customer.com` for one warehouse's team.
  - Add `--workers` to end every phone's worker session too.
- **A bad deploy** — Render → Events → roll back to the previous deploy.
- **An account being abused** — operator console → the warehouse → Close
  account (stops all scanning; nothing is deleted for 45 days).
- Leave evidence alone: don't delete logs, rows or deploys you might need.

## 2. Understand

- **What happened, when, to whom?** Sources:
  - Each warehouse's activity log (Settings → Activity log, or the operator
    console). It's append-only, so it can be trusted.
  - Render logs (request paths, IPs, errors), Neon's query/branch history,
    Stripe and Resend dashboards.
- **Whose data?** List the affected warehouses. Note what kind of data:
  order contents and customer names, worker names, dashboard users' emails,
  photos. Card data is never on our servers (Stripe).
- Need an old copy? Neon point-in-time restore can branch the database at a
  moment before the incident without touching production.

## 3. Tell people

- **Customers** — operator console → **Notices**. Preview the recipient count,
  then send to all open accounts, or only the affected warehouses' IDs (via
  the API). It emails their owners and records the notice in each warehouse's
  activity log. Say:
  1. what happened, in plain words, and when;
  2. what data was involved, and what wasn't;
  3. what we've done, and anything they should do (e.g. reset PINs);
  4. who to contact, and when we'll update them next.
- **Regulators / individuals** — your lawyer decides whether formal breach
  notices are required and to whom.
- **Security researcher** (if they reported it) — thank them, confirm the fix,
  agree when they can publish.

### Notice template

> **Subject:** Security incident affecting your Autorack account
>
> On [date], we discovered [plain description]. It affected [what data] for
> [which accounts] between [times]. [It did not affect ...]
>
> We have [contained it: what we did] and [what we're changing so it doesn't
> happen again].
>
> [What you should do, if anything.]
>
> We're sorry. We'll update you by [date]. Reply to this email with any
> questions.

## 4. Recover and learn

- Fix the root cause, add a test that would have caught it, deploy.
- Within a week, write a short blameless post-mortem: timeline, cause, what
  worked, what didn't, follow-ups with owners and dates. Keep it with these docs.
- Update `frontend/security.html` if anything it says has changed.

## Reporting channel

The public security page asks researchers to email the security contact
address. Make sure that inbox exists and is watched, then fill it in on
`frontend/security.html` (and consider publishing `/.well-known/security.txt`
with the same address once you have a domain).

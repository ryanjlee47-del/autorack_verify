# Operations

Most day-to-day operator work is in the **operator console** at `/admin/`
(emails listed in `OPERATOR_EMAILS`): overview, trials ending, pilots, per-warehouse
health and setup progress, feature usage, worker photos, activity, and buttons
to make a warehouse a pilot, extend a trial, or cancel. Everything done there is
written to that warehouse's activity log under your email.

The CLI covers the rest.

All operator tasks are CLI commands, run where the API runs (Render shell,
`railway run`, `fly ssh console`), from `backend/`:

```bash
python -m autorack.cli <command> --help
```

| Command | Use it to |
|---|---|
| `create-warehouse --name "Acme" --email owner@acme.com [--pilot]` | Provision a customer by hand. Prints a sign-in link and the phone setup code. |
| `login-link --email owner@acme.com` | Someone can't get their email. Send them this link another way. |
| `set-status owner@acme.com --status pilot` | Make a warehouse a free pilot (or `trialing --trial-days 30` to extend a trial). |
| `list-warehouses` | See every warehouse, its status and whether it can scan. |
| `end-sessions owner@acme.com` | Sign every worker out (e.g. after a security concern). |
| `prune` | Housekeeping (the job loop also does this hourly). |
| `run-jobs` | Send any due emails now (summaries, alerts, trial/payment notices). |
| `check-config` | Validate production settings. |
| `seed-demo` | Demo data for local development. Refuses in production. |

## Onboarding a pilot warehouse

1. `create-warehouse --pilot` (or let them sign up and run `set-status ... --status pilot`).
2. Walk the owner through: add workers (PINs), print the setup poster (Phones →
   Print setup poster), import today's CSV, print pick sheets.
3. On the floor: each worker scans the poster, adds the app to the home
   screen, signs in, and picks one order with you watching.
4. Do the airplane-mode test with them once, so they trust it.
5. After a week, look at Insights with the owner: mistakes caught, the
   most mis-picked items, and anyone flagged "check in".

## Support playbook

- **"The right item scans red."** Open the order → Scan history → "This is
  actually…" to teach the barcode. If it's systematic (e.g. every case label),
  check whether the CSV exports the unit barcode instead of the case barcode.
- **"The phone says it's offline with scans waiting."** They're safe on the
  phone. They sync when the phone reaches the network with the app open.
  Don't unlink the phone until it has synced.
- **"Lost phone."** Phones page → Unlink. Its PIN sessions end immediately.
- **"Forgot PIN."** Workers page → Reset PIN.
- **"Can't sign in."** Check spam; `login-link` as a fallback.
- **"Scanning is paused."** Billing state: `list-warehouses`. Past-due accounts
  have a grace period (`PAST_DUE_GRACE_DAYS`).

## Data

- Backups: Neon point-in-time restore. Test a restore to a branch occasionally.
- Every scan is permanent. Exports (Insights → Export) are the per-customer audit trail.
- Deleting a customer's data is a manual SQL task by design. Scan and audit
  tables are append-only; `TRUNCATE` is the only way to clear them, and
  should be done deliberately.

## License agreement

Every warehouse must e-sign the Application License Agreement: new owners sign
it as step 2 of sign-up (no account is created without it), and warehouses
that existed before, or were created with the CLI, are asked to sign by an
owner on their next sign-in. Until then the dashboard is locked; phones keep
scanning.

- The signed copy (the PDF with the signer's details stamped in, plus a
  signature certificate page: document SHA-256, signer, company, time, IP,
  browser) is stored in `agreement_signatures`, which can't be edited or
  deleted. Owners download it from Settings; you can from the operator console.
- To have Autorack's side of the signature block filled in, set
  `AGREEMENT_COUNTERSIGNER_NAME` and `AGREEMENT_COUNTERSIGNER_TITLE`.
- **Publishing a new version:** add `backend/autorack/legal/license-agreement-v2.pdf`
  and a `-v2.json` next to it (copy v1's; update `sha256`, and the blank
  positions if the layout changed), run
  `python backend/scripts/render_agreement_pages.py v2` (needs `pip install pypdfium2`),
  copy the PDF to `frontend/legal/`, and set `CURRENT_VERSION = "v2"` in
  `services/agreement.py`. Every owner is asked to sign again on next sign-in.
  Never edit a published PDF in place: the app refuses to start signing if a
  file no longer matches its recorded SHA-256.

# Dashboard

React + TypeScript, built with Vite. It reads the ledger and lets an admin
retry a webhook delivery. It cannot create or alter a transaction: the only
paths that write to the ledger are a signature-verified webhook and an admin
retry of one.

## Run it

```bash
npm install
cp .env.example .env          # VITE_API_URL, defaults to http://127.0.0.1:8000
npm run dev                   # http://localhost:5173
```

The backend must be running, and its `CORS_ORIGINS` must include this origin
(the default already does).

| Command | What it does |
| --- | --- |
| `npm run dev` | dev server with hot reload |
| `npm run build` | typecheck (`tsc --noEmit`) then production bundle |
| `npm run typecheck` | types only |
| `npm run preview` | serve the built bundle |

## If the dashboard says it cannot read a response

Almost always CORS, not connectivity. The request shows `200 OK` in the network
tab while the browser refuses to hand the body to JavaScript, because the
backend did not allow this origin.

- The dev server is pinned to port 5173 (`strictPort`). If that port is taken,
  Vite now fails loudly rather than moving to 5174 and landing on an origin the
  backend does not allow.
- To serve the dashboard from somewhere else, add that origin to `CORS_ORIGINS`
  in `backend/.env` and restart the backend.

## What it shows

- **Ledger balances** per account, signed by each account's normal side, with
  the trial balance as the headline. If debits ever stop equalling credits,
  that badge is the first thing you see.
- **Transactions**, newest first, each showing the entries that make it balance
  rather than a single opaque amount. Flagged rows are tinted.
- **Anomaly flags** with the reason text that carries the numbers which tripped
  the rule.
- **Webhook deliveries** with attempts and last error. Admins get a Retry
  button; viewers see a dash.

## Notes

- **Money is never a float.** Amounts arrive as integer minor units (paise) and
  `formatMoney` in `src/format.ts` is the only place they become a decimal
  string, using integer division. Nothing else divides by 100.
- **The role is not trusted from the token.** On boot the app calls `/auth/me`,
  so a demoted or deactivated account loses the admin controls immediately.
- **The token is in `localStorage`**, which survives a refresh and is readable
  by any script on this origin. That is a deliberate trade-off for a demo; in
  production this should be an httpOnly cookie with CSRF protection.
- Any `401` from the API ends the session rather than rendering an error box,
  so an expired token drops you back to the login screen.

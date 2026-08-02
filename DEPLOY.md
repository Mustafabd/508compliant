# Deploying 508Compliant

This app is a single Docker image (FastAPI + static frontend) plus a
Postgres database. It needs no persistent disk — converted PDFs live for
30 minutes and are then deleted.

You'll need accounts on two services before it can accept real payments:
**Stripe** (billing) and **Render** or **Railway** (hosting). Neither can be
set up on your behalf — both require you to create an account and hand over
payment/identity details, so the steps below are things you run yourself.
Nothing in this repo needs your secret keys committed to git; you paste them
into the host's dashboard as environment variables.

## 1. Set up Stripe

1. Create a [Stripe account](https://dashboard.stripe.com/register) if you
   don't have one. Stay in **test mode** (toggle top-right) until you're
   ready to take real payments.
2. **Products** → **Add product** → name it "Pro" → add a **recurring**
   price (e.g. $19/month) → save. Copy the price ID (`price_...`) from the
   product page.
3. **Developers** → **API keys** → copy the **Secret key** (`sk_test_...`).
4. Webhook: **Developers** → **Webhooks** → **Add endpoint**.
   - URL: `https://<your-app-domain>/api/webhooks/stripe` (you'll have this
     once step 2 or 3 below is done — you can add the webhook after and
     come back to it).
   - Events to send: `checkout.session.completed`,
     `customer.subscription.updated`, `customer.subscription.deleted`.
   - Copy the **Signing secret** (`whsec_...`).

You now have three values: `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`,
`STRIPE_WEBHOOK_SECRET`.

## 2a. Deploy to Render

1. Push this repo to GitHub (already done if you're reading this from the
   repo).
2. In the [Render dashboard](https://dashboard.render.com/): **New** →
   **Blueprint** → connect the repo. Render reads `render.yaml` and
   provisions the web service and a Postgres database together.
3. Once created, open the web service's **Environment** tab and fill in the
   variables marked "sync: false" in `render.yaml`:
   - `APP_BASE_URL` — the `https://....onrender.com` URL Render assigned
     this service (shown at the top of the service page).
   - `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`, `STRIPE_WEBHOOK_SECRET` from
     step 1.
   - `ANTHROPIC_API_KEY` (optional) — enables real AI-generated alt text.
4. Save — Render redeploys automatically. Go back to the Stripe webhook you
   created and make sure its URL matches `APP_BASE_URL` +
   `/api/webhooks/stripe`.
5. `SECRET_KEY` and `DATABASE_URL` are already wired up by the blueprint
   (auto-generated / pulled from the attached database) — you don't need to
   touch those.

## 2b. Deploy to Railway (alternative)

1. [Railway dashboard](https://railway.app/): **New Project** → **Deploy
   from GitHub repo** → select this repo. Railway detects `railway.json`
   and builds from the `Dockerfile`.
2. Add a Postgres database: **New** → **Database** → **Add PostgreSQL**
   in the same project. Railway auto-injects `DATABASE_URL` into linked
   services if you reference `${{Postgres.DATABASE_URL}}` in the web
   service's variables — or just copy the connection string manually.
3. On the web service, open **Variables** and set:
   `ENVIRONMENT=production`, `SECRET_KEY` (any long random string),
   `APP_BASE_URL` (Railway's generated `*.up.railway.app` domain, or your
   custom domain), `STRIPE_SECRET_KEY`, `STRIPE_PRICE_ID`,
   `STRIPE_WEBHOOK_SECRET`, and optionally `ANTHROPIC_API_KEY`.
4. **Settings** → **Networking** → **Generate Domain** if you haven't
   already, to get a public URL.

## 3. Test it end to end (test mode)

1. Visit your deployed URL, sign up for an account.
2. Convert 3 PDFs — the 4th should be blocked with an upgrade prompt.
3. Click **Upgrade to Pro**, complete Stripe Checkout using a
   [test card](https://docs.stripe.com/testing) (`4242 4242 4242 4242`,
   any future expiry/CVC).
4. You should land back on `/app.html?checkout=success`; within a few
   seconds (once the webhook fires) the account bar should show "Pro plan —
   unlimited conversions". If it doesn't update, check the webhook's
   delivery log in the Stripe dashboard for errors.
5. Click **Manage billing** to confirm the Stripe customer portal opens and
   you can cancel/change the subscription.

## 4. Go live

Stripe test mode and live mode are entirely separate — repeat step 1 in
live mode (new secret key, new price ID, new webhook + signing secret) and
update the same environment variables with the live values once you're
ready to accept real payments.

## Notes / known limitations

- Sessions and the free-tier usage counter are stored in-process (signed
  cookie + Postgres respectively) and login-attempt throttling is
  in-memory — this is fine for a single instance. If you scale to multiple
  instances, move login throttling to Redis (session cookies and usage
  counts already live in Postgres/the cookie itself, so those are fine).
- There's no email verification or password-reset flow yet — both are
  reasonable next additions before a public launch.
- `Base.metadata.create_all()` runs on startup instead of real migrations
  (Alembic). Fine for the current schema; add Alembic before making
  breaking schema changes to a database with real user data in it.

# NexGame Lite — Deployment Guide (Railway)

Fastest path to a public HTTPS URL for launch week. No manual server
setup, no nginx, no certbot — Railway handles all of that.

## 1. Push to GitHub

Railway deploys from a git repo. If this project isn't already in one:

```bash
cd nexgame_lite
git init
git add .
git commit -m "NexGame Lite — initial launch build"
```

Create a repo on GitHub (private is fine) and push:
```bash
git remote add origin https://github.com/YOUR_USERNAME/nexgame-lite.git
git push -u origin main
```

## 2. Create the Railway project

1. Go to railway.app, sign in with GitHub
2. "New Project" → "Deploy from GitHub repo" → select your repo
3. Railway detects `railway.json` automatically and builds with Nixpacks
4. First deploy will likely fail or half-work — that's expected, env vars aren't set yet (next step)

## 3. ⚠️ CRITICAL — Attach a persistent volume

**Without this step, every customer, every prediction, and every
settled game gets WIPED on every redeploy.** Railway's default
filesystem is ephemeral.

1. In your Railway project → your service → **Settings** → **Volumes**
2. Add a volume, mount path: `/app/data`
3. Update `config.py` and `customers.py` to write the SQLite files
   there instead of the working directory (see step 3a below)

**3a. One-time code change before first deploy:**

In `config.py`:
```python
DB_PATH = "/app/data/nexgame_lite.db"
```

In `customers.py`:
```python
CUSTOMERS_DB = "/app/data/nexgame_lite_customers.db"
```

Locally these paths won't exist, so for local dev keep using the
relative filenames — simplest fix is an environment check:
```python
import os
DB_PATH = os.environ.get("DB_PATH", "nexgame_lite.db")
```
and set `DB_PATH=/app/data/nexgame_lite.db` as a Railway env var
instead of hardcoding it. Same pattern for `CUSTOMERS_DB`.

## 4. Set environment variables

In Railway → your service → **Variables**, add:

| Variable | Value |
|---|---|
| `NEXGAME_SECRET_KEY` | Generate: `python -c "import secrets; print(secrets.token_hex(32))"` — run this locally, paste the output. **Never reuse a key you've shared anywhere.** |
| `DB_PATH` | `/app/data/nexgame_lite.db` |
| `CUSTOMERS_DB_PATH` | `/app/data/nexgame_lite_customers.db` |

Once you have your data provider key, also set:

| Variable | Value |
|---|---|
| `BDL_API_KEY` | Your BallDontLie GOAT-tier key (primary provider, $79.98/mo) |
| `MSF_API_KEY` / `MSF_PASSWORD` | Only if using MySportsFeeds instead (backup option) |
| `GUMROAD_WEBHOOK_SECRET` | From Gumroad Settings → Advanced, once you set up the Ping endpoint |

Also set `DATA_PROVIDER=balldontlie` as an env var, or hardcode it in
`config.py` before deploying.

## 5. Get your public URL

Railway auto-assigns a URL like `nexgame-lite-production.up.railway.app`
under Settings → Networking → "Generate Domain". That URL is live and
HTTPS immediately — usable for launch even before a custom domain.

## 6. (Optional but recommended) Custom domain

1. Buy a domain (Namecheap, Google Domains, etc.) — e.g. `nexgamelite.com`
2. Railway → Settings → Networking → "Custom Domain" → enter your domain
3. Railway gives you a CNAME record → add it at your domain registrar
4. SSL is automatic once DNS propagates (usually under an hour)

## 7. Point the Gumroad webhook at your live URL

Once deployed:
1. Gumroad → Settings → Advanced → Ping endpoint
2. Paste: `https://your-domain-or-railway-url/webhooks/gumroad`
3. Click "Send test ping" to confirm it reaches your server
   (check Railway's deploy logs to see the ping land)

## 8. Smoke test before announcing launch

```
curl https://your-url/api/accuracy
```
Should return JSON, not an error. Then manually walk through:
login page loads → (create a test B2B customer via CLI, get the
license key) → log in → run a prediction → confirm the accuracy
page updates after a settle.

## Cost

Railway's free tier covers small apps like this for the first stretch;
beyond that it's usage-based (roughly $5-10/mo for a service this size
at low traffic) — far cheaper than EC2 setup time would cost you this
week.

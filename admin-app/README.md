# BSR Corridors — Admin app

Private Django app for managing rooms, guests and stays, with a calendar, smart stay extensions,
Excel exports and nightly Google Drive backups. Requirements: [../docs/requirements.md](../docs/requirements.md).

Runs free: **Render** (app) + **Supabase** (Postgres), both in Singapore.

---

## 1. Run on your computer

```bash
cd admin-app
python3 -m venv .venv            # if pip is missing: python3 -m venv --without-pip .venv, then get-pip.py
.venv/bin/pip install -r requirements.txt
cp .env.example .env             # then edit: set DJANGO_DEBUG=1, ADMIN_USERNAME, ADMIN_PASSWORD
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createcachetable
.venv/bin/python manage.py ensure_admin
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000 and sign in. Without `DATABASE_URL`, a local SQLite file is used.

Tests: `.venv/bin/python manage.py test core`

---

## 2. Database — Supabase (free)

1. https://supabase.com → **New project** → Region **Southeast Asia (Singapore)**. Save the database password.
2. **Connect** → **Session pooler** → copy the URI (port **5432**, works over IPv4):
   `postgresql://postgres.xxxx:PASSWORD@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`
3. Add `?sslmode=require` to the end. This is your `DATABASE_URL`.

Free projects pause after 7 days without activity — the keep-awake ping (step 4) prevents that.

---

## 3. App — Render (free)

1. https://render.com → sign in with GitHub → **New → Blueprint** → choose this repo.
   It reads [`../render.yaml`](../render.yaml) (free plan, Singapore, root `admin-app`).
2. Fill in the values it asks for:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | from Supabase (step 2) |
   | `FIELD_ENCRYPTION_KEY` | run `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — **save a copy somewhere safe** (password manager). Without it, ID numbers and backups can't be read. |
   | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | your login (password ≥ 10 characters) |
   | `GOOGLE_*` | leave blank until step 5 |

   `DJANGO_SECRET_KEY` and `BACKUP_TOKEN` are generated automatically.
3. Deploy. Your admin app is at `https://bsr-corridors-admin.onrender.com` (or similar).
   The website's `/admin` forwards there — if Render picks a different address, update the two
   `[[redirects]]` in [`../netlify.toml`](../netlify.toml).

To change your password later: edit `ADMIN_PASSWORD` in Render → **Manual Deploy**.

---

## 4. Keep it awake (free)

Free Render apps sleep after 15 minutes idle (first visit then takes ~30–50 s).

1. https://cron-job.org → free account → **Create cronjob**
2. URL: `https://<your-app>.onrender.com/healthz/` · every **10 minutes** · Save.

This also keeps the Supabase database active.

---

## 5. Google Drive backups

Backups go to the Drive of **bsrcorridors@gmail.com** in a folder called *BSR Corridors Admin*:
`Backups/backup-….json` (last 30 kept, ID numbers encrypted), a **Guest register** Google Sheet
(updated nightly, ID numbers show last 4 digits) and `Exports/` (from the Export page).

**One-time setup (≈10 minutes):**

1. Sign in to https://console.cloud.google.com as **bsrcorridors@gmail.com** → create a project, e.g. "BSR Corridors Admin".
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **OAuth consent screen** → User type **External** → app name "BSR Corridors Admin", `bsrcorridors@gmail.com` as
   support and developer email → Scopes: add `.../auth/drive.file` → Test users: add `bsrcorridors@gmail.com`.
   Then **Publish app** (status "In production"). *Unpublished "Testing" apps get tokens that expire
   after 7 days.* Google may show "unverified app" when you sign in — that's expected for your own app;
   click **Advanced → Go to BSR Corridors Admin**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Copy the client ID and secret.
5. On your computer, put them in `admin-app/.env` as `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`, then run:
   ```bash
   .venv/bin/python manage.py google_auth
   ```
   A browser opens — sign in as **bsrcorridors@gmail.com** and allow access.
   It prints `GOOGLE_REFRESH_TOKEN=…`.
6. In Render, set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` → deploy.
7. In the admin app: **Backups → Back up now**. Check your Drive.

The app can only see files it created (`drive.file` scope) — nothing else in your Drive.

### Nightly schedule (GitHub Actions, free)

The workflow [`../.github/workflows/admin-backup.yml`](../.github/workflows/admin-backup.yml) runs at 02:00 IST.
In GitHub → repo **Settings → Secrets and variables → Actions**, add:

| Secret | Value |
|---|---|
| `ADMIN_APP_URL` | `https://<your-app>.onrender.com` |
| `ADMIN_BACKUP_TOKEN` | the `BACKUP_TOKEN` value from Render → Environment |

Test it: GitHub → **Actions → Admin nightly backup → Run workflow**.

---

## 6. Restore from a backup

1. Download a `backup-….json` from Drive.
2. With `DATABASE_URL` and the **same** `FIELD_ENCRYPTION_KEY` in `.env`:
   ```bash
   .venv/bin/python manage.py migrate
   .venv/bin/python manage.py restore_backup backup-2026-10-06_0200.json --yes
   ```

---

## How it works (for developers)

| Part | Where |
|---|---|
| Models (rooms, guests, stays, audit log) | `core/models.py` |
| Rules: no double booking, extensions, check-in/out | `core/services.py` |
| Encrypted ID numbers | `core/fields.py` |
| Double-booking guard in Postgres (exclusion constraint) | `core/migrations/0002_stay_no_overlap.py` |
| Excel / JSON | `core/exports.py` · Google Drive: `core/gdrive.py` |
| Pages | `core/views/`, `templates/` · styles `static/css/app.css` · htmx for partial updates |

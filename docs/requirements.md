# BSR Corridors — Admin System Requirements

Status: **v1 — agreed, implemented in `admin-app/`** · 2026-10-06 · Owner: Shekhar (sole admin)

Items marked **[Suggested]** were proposed during review and **all accepted** by the owner.

---

## 1. Scope

A private admin app for BSR Corridors to manage rooms, guests and stays, with a calendar view.
The public website (Next.js on Netlify) stays as it is; the admin app is a separate Django project.

**Out of scope for v1:** SMS / WhatsApp notifications (planned for a later phase), online payments,
OTA (Airbnb/Booking.com) sync, multiple users or staff logins.

---

## 2. Users

| Role | Who | Access |
|---|---|---|
| Admin | Owner only (single account) | Everything |

No sign-up, no other roles. The admin account is created from the command line.

---

## 3. Functional requirements

### 3.1 Authentication
- FR-1 Admin signs in with username + password (Django auth, hashed password).
- FR-2 Session expires after inactivity (default 8 h); sign-out button on every page.
- FR-3 Repeated failed logins are throttled (e.g. 5 attempts → 15 min lock).

### 3.2 Room configuration (admin-configurable)
- FR-4 CRUD **room types**: name (e.g. "Luxury AC"), AC / Non-AC, bed type, max guests.
- FR-5 CRUD **rooms**: room number, floor, room type, status (Active / Under maintenance).
- FR-6 A room type or room that has stays cannot be deleted — only deactivated (keeps history intact).
- FR-7 Number of rooms and types is not fixed; all are configured from the admin UI.

### 3.3 Guests & stays (CRUD)
Each **stay** records:

| Field | Required | Notes |
|---|---|---|
| Guest name | ✔ | |
| Phone | ✔ | With country code; used to find returning guests |
| Address | ✔ | As shown on the ID proof |
| Nationality | ✔ | Default "Indian" |
| ID proof type | ✔ | Aadhaar / Driving licence / Passport / Voter ID / Other |
| ID proof number | ✔ | Full number (Aadhaar or other), **encrypted at rest**, shown masked (see §5.4) |
| Room | ✔ | |
| Check-in date | ✔ | |
| Check-out date | ✔ | Must be after check-in |
| Booked via | ✔ | Walk-in / Direct (call, WhatsApp) / Airbnb / Booking.com / MakeMyTrip / Other |
| Booking reference | | OTA confirmation code |
| Total amount | ✔ | Agreed price for the **whole stay** (not per night) |
| Amount paid | ✔ | ₹; can be updated as payments come in |
| Payment mode | [Suggested] | Cash / UPI / Card / Bank transfer |
| Number of guests | [Suggested] | Cannot exceed the room type's max guests |
| Notes | [Suggested] | Free text |

- FR-8 Create, view, edit, cancel and delete stays.
- FR-9 **No double booking:** a room cannot have overlapping stays — enforced in the database, not just the UI.
- FR-10 Search stays/guests by name or phone; filter by date range and room.
- FR-11 **One guest per phone number.** Typing a known number fills the saved details; a **Guests** page searches by
  phone or name and starts a new booking for that guest. Saving a second guest with the same number is blocked.
- FR-12 **Balance due** = total amount − amount paid; highlighted when unpaid.
- FR-13 [Suggested] **Status:** Upcoming → Checked in → Checked out, or Cancelled. One-tap "Check in" / "Check out".

### 3.3a Future bookings, monthly guests, upcoming
- FR-13a **Quick future booking:** only guest name, room and dates are required (amount optional). Phone, address
  and ID can be added later; **at check-in the phone and address are required** (ID stays optional).
- FR-13b **Monthly (open-ended) stays:** no check-out date until the guest leaves; the room stays blocked until then.
  Monthly rent, an "Add month's rent" button (adds to the total) and a refundable **advance / security deposit** kept
  separate from payments. "Advance / amount paid" records money received before or during the stay.
- FR-13c **Upcoming:** Today page shows arrivals **tomorrow** and **the day after**, rooms departing/vacant tomorrow,
  and links to an **Upcoming bookings** page listing all future arrivals by date.
- FR-13d **Extra guests:** optional name, phone, ID type and ID number for guest 2…N (rows follow "Number of guests").

### 3.4 Smart stay extension
- FR-14 "Extend stay" on any active stay: pick a new check-out date, or quick buttons **+1 / +2 / +7 nights**,
  enter the **amount for the extra days** (added to the stay total) and optionally an amount **paid now**.
- FR-15 The system checks the same room for conflicts before saving:
  - **Free →** extends in place; total and balance update automatically.
  - **Booked →** shows who has the room next, and offers:
    (a) move the guest to a free room **of the same type** for the extra nights (split stay, linked together), or
    (b) move the whole stay to a room that is free for the full period, or
    (c) cancel the extension.
- FR-16 Every extension is logged (old date → new date, when), visible on the stay.
- FR-17 [Suggested] **Early checkout:** shorten a stay; recalculates total and frees the room.

### 3.5 Calendar
- FR-18 **Desktop & tablet:** rooms as rows × dates as columns ("tape chart"); stays drawn as bars across their nights;
  week / 2-week / month ranges; previous/next/today navigation.
- FR-19 Filter by room type; colour by status (upcoming / checked in / checked out / unpaid balance).
- FR-20 Tap an empty cell → new stay pre-filled with that room and date. Tap a bar → stay details, edit, extend.
- FR-21 **Mobile:** default to a **Today** view (arriving, in-house, departing, vacant rooms) plus a per-room month view;
  the full grid scrolls horizontally with room names pinned.
- FR-22 [Suggested] **Dashboard:** today's check-ins, check-outs, occupied vs vacant rooms, total unpaid balance.

### 3.6 Google Drive backup
- FR-23 Nightly automatic export of all rooms, guests and stays to the owner's Google Drive:
  - a **JSON** file (full backup, restorable), and
  - a **Google Sheet** (readable on any device).
- FR-24 "Back up now" button in the admin UI.
- FR-25 Keep the last 30 daily JSON backups; older ones are deleted automatically.
- FR-26 Documented restore procedure (JSON → database).

### 3.7 [Suggested] Foreign national guests (Form C)
- FR-27 If nationality is not Indian, also capture passport number, visa number/type and arrival date in India,
  and show a reminder that **Form C must be filed with the FRRO within 24 hours** of check-in.

### 3.8 [Suggested] Export
- FR-28 Download stays for a date range as Excel/CSV (e.g. for police verification or accounts).

---

## 4. Non-functional requirements

### 4.1 Performance ("fast CRUD")
- NFR-1 Page loads and saves respond in **< 500 ms** (server time) once the app is awake.
- NFR-2 Month calendar for up to 50 rooms renders in **< 1 s**.
- NFR-3 Partial page updates (htmx) after create/edit/extend — no full page reloads.
- NFR-4 App server and database in the **same region** (Singapore) to keep each query fast.
- NFR-5 Indexed queries on room + date range and phone; calendar loads in a fixed small number of queries.

### 4.2 Responsive UI
- NFR-6 Works on phones (≥ 360 px), tablets and desktops; touch targets ≥ 44 px; no horizontal page scroll
  except inside the calendar grid.

### 4.3 Security & privacy
- NFR-7 HTTPS only; secure, HttpOnly session cookies; Django CSRF protection on every form.
- NFR-8 All secrets (DB URL, Django secret key, Google credentials) in environment variables — never in Git.
- NFR-9 Admin pages are not indexed by search engines.
- NFR-10 Audit log of create / update / delete on stays.

### 4.4 Reliability
- NFR-11 Database constraint prevents overlapping stays even if two saves happen at once.
- NFR-12 Nightly Drive backup; a failed backup is visible on the dashboard.

### 4.5 Cost
- NFR-13 **Zero running cost** — free tiers only (see §6).

### 4.6 Other
- NFR-14 All dates/times in **IST (Asia/Kolkata)**; dates shown as DD-MM-YYYY.
- NFR-15 Data retention: [Suggested] guest records older than a set period (e.g. 3 years) can be purged from the admin UI.

---

## 5. Constraints & decisions

### 5.1 Stack
- **Backend:** Python 3.12, Django 5, PostgreSQL.
- **Admin UI:** Django templates + **htmx** (fast partial updates, no separate frontend app) + a small amount of JS for the calendar grid.
- **Public website:** unchanged (Next.js static on Netlify). Its `/admin` page will link/redirect to the Django admin app.

### 5.2 Why not Google Drive as the main database
Drive has no queries, no transactions and no locking: every read/write is a slow network call that rewrites the
whole file, concurrent edits overwrite each other, and double bookings can't be prevented. Postgres is the source
of truth; Drive holds backups and a readable Sheet (§3.6).

### 5.3 Free hosting plan
| Part | Service (free tier) | Notes |
|---|---|---|
| Django app | **Render** — Web Service, Singapore | Free instances sleep after 15 min idle (30–50 s cold start). Mitigated by a free uptime ping every 10 min (cron-job.org). 750 free hours/month covers one always-on service. |
| Database | **Supabase** Postgres, Singapore | 500 MB free (enough for many years of guest records). Free projects pause after 7 days of no activity — daily use + the nightly backup keep it active. |
| Nightly backup | **GitHub Actions** scheduled job | Calls the backup endpoint; free for this usage. |
| Drive / Sheets | Google Drive API with the **owner's Gmail (OAuth, `drive.file` scope)** | Free. Service accounts can't store files in a personal Gmail Drive, so the app uses a one-time OAuth refresh token. |

### 5.4 ID numbers
Decision: the owner stores the **full** ID number (Aadhaar or other proof).
Note: under the Aadhaar Act, businesses not authorised to do so generally should not store full Aadhaar numbers
*(general guidance, not legal advice)*. Mitigations implemented:
- ID and passport numbers are **encrypted in the database** (Fernet; key in `FIELD_ENCRYPTION_KEY`).
- Shown as `•••• •••• 1234` everywhere; a **Show** button reveals the full number and is logged.
- JSON backups keep them encrypted; the Drive Sheet and exports show the last 4 digits unless
  "Include full ID numbers" is ticked for a specific export.

---

## 6. Later phases (not in v1)
- SMS / WhatsApp messages to guests (booking confirmation, checkout reminder).
  Note: the official WhatsApp Business API is paid per conversation; a free option is one-tap
  "Send on WhatsApp" links (pre-filled message) from the admin UI.
- Staff logins, OTA calendar sync, online payments.

---

## 7. Open items
1. Room list and types — configured by the owner after launch (Rooms page).
2. Drive backups/exports go to **bsrcorridors@gmail.com** (connected during Google Drive setup).
3. SMS / WhatsApp notifications — next phase.

# BSR Group of Hotels — Requirements v2 (multiple properties)

Status: **APPROVED** (20-11-2026), on condition that the running BSR Corridors app is not affected (§13).
Builds on: [requirements.md](requirements.md) (v1, the BSR Corridors hotel) and [system-design.md](system-design.md).

How to read this document:

- **[Asked]** marks what you asked for.
- **[Suggested]** marks what was added because a project like this normally needs it. **All accepted** except the
  late fee (S16, dropped). See §15.
- **[Decided]** marks an answer you gave to a question in draft 1.

---

## 1. Goal

Today the app runs one hotel, BSR Corridors. v2 turns it into a **group** that can run any number of
**properties** of three types: **Hotel**, **PG** and **Co-living**. Each property keeps its own data, Google
Drive, settings, money password and colour theme, while staff sign in once and see only the properties they are
allowed to see.

### 1.1 Words used in this document [Decided: D1]

| Word | Meaning |
|---|---|
| **Properties** | The top level: *BSR Group of Hotels*, all its properties together. |
| **Property** | One hotel, PG or co-living building in the group. |
| **Property type** | Hotel, PG or Co-living. |
| **Unit** | What is rented out: a **room** in a hotel, a **bed** in a PG or co-living. |
| **Guest** / **Resident** | The person staying: guest in a hotel, resident in a PG or co-living. |
| **Stay** / **Tenancy** | One person in one unit for a period: stay in a hotel, tenancy in a PG or co-living. |

---

## 2. Scope

### 2.1 In scope for v2

1. **Properties home page** listing the properties a user may see, with *Add property* for the admin. [Asked]
2. **Property set-up:** name, type, address, manager, email, phones, photos, theme, and its own configuration
   (database, Google Drive, WhatsApp, UPI, money password). [Asked]
3. **Separate data per property,** with tables created automatically by type (§6). [Asked]
4. **Users and permissions:** one login per person, access given per property. [Asked, Decided: D2]
5. **Hotel type:** works exactly like BSR Corridors today. [Asked]
6. **PG and Co-living types:** Floors → Rooms → Beds, a resident per bed, daily / weekly / monthly rent, advance
   per bed, beds added and removed with history kept. [Asked] Built first. [Decided: D12]
7. **A colour theme per property,** changeable, with a live preview; same layout everywhere. [Asked]
8. **Animations** across the app (§10). [Asked]
9. **Expenses work as they do today,** per property. [Asked]
10. **BSR Corridors becomes the first property** with its data left exactly where it is, and all links and history unchanged (§13).

### 2.2 Later phases (not v2)

- Public websites for the new properties. [Decided]
- Resident self-service app or portal.
- Online booking engine, OTA calendar sync, paid WhatsApp API, payment gateway, SMS.
- Accounting software export.
- Two-step sign-in (OTP by authenticator app). [Decided: D3]

---

## 3. Users, roles and permissions

### 3.1 Sign-in [Decided: D2]

- **One login per person for the whole group.** Someone who works at two properties has one username and
  password and sees both.
- After signing in, users land on the **properties home page**. With access to only one property they go straight
  into it. [Suggested]
- The main admin (you) is created from the environment, as today (`ADMIN_USERNAME` / `ADMIN_PASSWORD`).
- Protections from v1 stay: lockout after 5 failed tries, 8-hour sessions, minimum 10-character passwords.

### 3.2 Roles

Roles are given **per property**: a user can be Manager at one and Front desk at another.

| Role | Scope | Can do |
|---|---|---|
| **Admin** | Whole group | Everything: add, edit, archive properties; change any configuration (database, Drive, links) [Asked]; manage users and permissions; reset any money password [Asked]. |
| **Owner / partner** | One property | Everything in that property, including money pages (with the money password). |
| **Manager** | One property | Day-to-day work, residents/guests, expenses; money pages only with *Money access*. Cannot change configuration. |
| **Front desk / Host** | One property | Bookings, check-in/out, move-in/out, payments, WhatsApp messages. No money pages, expenses or settings. |
| **Accountant** | One property | Money pages, expenses, payments, exports. Read-only for stays and tenancies. |
| **View only** | One property | Sees, changes nothing. |

Extra switches per user per property [Suggested]: **Money access**, **Can see full ID numbers**, **Can delete**.

### 3.3 Money password per property [Asked]

- Each property has **its own money password** (today's revenue password), set while configuring the
  item.
- Money pages (revenue, profit, settlements) need the user's login **and** that property's money password.
- If it's forgotten, the **admin sets a new one** from the property's settings. No Render change or redeploy
  needed (today's `REVENUE_PASSWORD` environment override is no longer needed).
- Stored only as a hash; every change is logged.

### 3.4 Rules

- A user sees **only** properties they have access to: on the home page, in links, in search. Opening a
  item's URL without access shows "no access".
- Removing access takes effect immediately, even for someone already signed in.
- Every action is logged with the user and the property.

---

## 4. Properties home page

[Asked] The first page after signing in.

**Shows:**

- The group name and logo (*BSR Group of Hotels*).
- One **card per property** the user can access, in that property's theme colour:
  - cover photo, name, type badge (Hotel / PG / Co-living), area
  - today at a glance: occupied / total (rooms or beds), arrivals / move-ins and departures / move-outs today
    [Suggested]
  - alerts badge: payments to verify, rent overdue, maintenance due [Suggested]
  - **no money figures** [Decided: D6]. Money is shown inside each property only.
- **Add property** button (admin only).
- **Archived** section (admin only). [Suggested]

**Rules:**

- A property can be **archived** (hidden, read-only, data kept) but not deleted from the app. [Suggested]
- The admin can set the order of the cards. [Suggested]

---

## 5. Adding and configuring a property

### 5.1 Add property form [Asked + Suggested]

**Basic details**

| Field | Required | Notes |
|---|---|---|
| Name | ✔ | e.g. "BSR Corridors", "BSR Ladies PG" |
| Type | ✔ | Hotel / PG / Co-living. **Can't be changed later** (the tables depend on it). |
| Short code | ✔ | 2–6 letters, e.g. `BSRC`. Used in the URL, invoice numbers and file names. |
| Address | ✔ | Street, area, city, state, PIN |
| Map link | | Google Maps link |
| Managed by | ✔ | A user (gets the Manager or Owner role) |
| **Email** | ✔ | [Decided] |
| Phone numbers | ✔ | One or more; one marked as the WhatsApp number |
| Photos | | Cover photo plus gallery (stored in the property's Drive) |
| Logo | | On the header, invoices and receipts |
| Theme colour | ✔ | See §9: changeable later, with live preview |
| For (PG / Co-living) | ✔ | Men / Women / Co-ed |
| Check-in / check-out times | | Hotel; used in messages |
| Notes | | |

**Business details** [Suggested] — **none of these are required**

| Field | Notes |
|---|---|
| Legal / business name | On invoices and receipts |
| GSTIN, PAN, Udyam number | Optional (all business details are optional) [Decided: S5] |
| UPI ID and UPI name | For payment links (today's `UPI_ID` / `UPI_NAME`, now per property) |
| Invoice / receipt prefix and footer | |

**Configuration (the property's own "environment")** [Asked]

| Setting | Notes |
|---|---|
| Database | Where its data lives. See §6. |
| Google Drive | **Connect Google Drive** button: sign in to the property's Google account. Folder name. |
| WhatsApp | Number shown to guests; hotel info used in messages (Wi-Fi, rules, location, contact, review link) |
| Short link base | Defaults to the one website |
| Hosts | Names, as today's Settings page |
| **Partners** | Set while adding or editing the property: name, phone, share % (adding up to 100, or all empty for equal shares), where to pay their share. Used to split the profit when a month is settled, for hotels and PGs alike. [Asked] |
| **Money password** | Set here [Asked]; admin can reset it later |
| Deposit refund rules | Refund within N days of move-out; standard deductions with amounts (e.g. cleaning) [Decided: D10] |

### 5.2 What happens when a property is added

1. The configuration is checked: database reachable, Google Drive connected (if given).
2. **The tables for its type are created** (hotel tables, or PG/co-living tables). [Asked]
3. Default data is added: WhatsApp messages for the type, expense categories, default settings.
4. The *Managed by* user gets access.
5. It appears on the properties home page.

If any step fails, nothing is half-created: the property isn't added and the reason is shown.

### 5.3 Changing a property later

- **All details and configuration can be changed by the admin,** including the Google Drive account, the database
  link and other links. [Asked] Only the **type** can't change.
- Changing the **database** of a property that already has data is a guided move: copy, check counts, switch.
  [Suggested] The app never silently switches to an empty database.
- Secrets (database password, Drive access) are never shown again after saving: only "Connected ✔" and *Replace*.

### 5.4 Future table changes [Asked]

- When a new version of the app changes the tables for a type (new field, new table), the change is applied
  **automatically to every property of that type** on deploy: all hotels together, all PGs and co-livings together.
- The deploy shows which properties were updated. If one fails (e.g. its database is unreachable), the others are
  still updated, that property shows a "needs update" alert, and it is retried on the next deploy or by the admin.
- A property is never opened with out-of-date tables; it shows "being updated" instead.

---

## 6. Databases: separate data, all free

### 6.1 Your questions [Asked, D4]

**"I create multiple Supabase accounts, one per property. Is that okay? I need everything free."**

It would work technically, but I don't recommend it:

- Supabase's free plan is **2 free projects**, and opening extra accounts to get more free projects is against
  Supabase's fair-use terms. They can pause or remove such projects, which would mean losing a property's data.
  Please read their current terms yourself before relying on it.
- Each free project **pauses after 7 days idle** and needs its own keep-awake ping, its own password, and its own
  account to look after.
- A page that shows data from every property has to connect to every database, which is slower and fails if one
  of them is paused.

**"Is 512 MB RAM (Render free) enough?"**

Yes, for this use. Memory is used only while a page is being loaded, not while a page sits open in your browser, so
keeping the page open all day costs almost nothing. Two hours of combined use a day is light. The app today runs well
within 512 MB; the only care needed is not keeping many idle database connections open, which v2 will handle. Render's
free plan gives 750 hours a month, enough for one app awake all month.

**"A page that sees data from every property, without mixing anything up."**

This is easy with the design below: everything in **one free Supabase database**, but each property in its **own
schema**.

### 6.2 Recommended design: one database, one schema per property

A **schema** is a separate, named section inside one database, with its own copy of every table. Two schemas can
both have a `stays` table without the two ever mixing.

```
Supabase project (free, 500 MB)
├── shared tables      → properties, roles, encrypted configuration (new tables next to the logins)
├── schema "public"    → BSR Corridors (hotel): its current tables, untouched
├── schema "bsrpg1"    → BSR Ladies PG: floors, rooms, beds, residents, tenancies, …
└── schema "bsrcl1"    → BSR Co-live: …
```

- **No mix-up:** on every request the app checks the user's permission, then switches to **only** that
  item's schema. Code for one property can't see another's tables. Automated tests check this for every page.
- **Group-wide pages** (§11) read each schema one after another and show them side by side, in one database, in
  one quick query each.
- **Free:** one Supabase project, one keep-awake ping (as today), one backup set-up.
- **Size:** 500 MB is enough for **10+ properties for many years**. A booking or tenancy with its payments is a
  few kilobytes; ID photos, invoices and receipts are in Google Drive, not the database. The app shows database
  usage on the admin page, with a warning at 80 % and an alert on the properties home page for the admin. **Required.**
  [Decided: S7]
- **Moving out later:** if a property grows large, or you start paying for databases, the admin can move it to
  its own database (§5.3). The app supports both kinds side by side.
- The second free Supabase project stays **spare**: for a test copy, or for one property that needs its own
  database.

**[Decided: D13]** One Supabase database, one schema per property.

### 6.3 Configuration lives in the app, not in Render

Render's environment variables need a redeploy to change and the app can't add them itself. So each property's
settings (Drive access, UPI ID, WhatsApp details, its own database link if it has one) are entered **in the app**,
stored **encrypted** in the shared tables, and only these stay in Render: the database link, the **master
encryption key**, and the admin login.

> **The master encryption key protects every property's secrets and ID numbers.** Lose it and they can't be
> read. Keep a copy in a password manager (it is today's `FIELD_ENCRYPTION_KEY`).

### 6.4 Rules

- One property's data **never** appears in another's pages, exports, backups or messages.
- Each property is backed up **separately to its own Google Drive** every night (§12).
- Database space is shown to the **main admin only**. [Asked]

### 6.5 Archive database [Asked]

- The main admin connects a **second free database** (e.g. another Supabase project) once, on the Archive page.
- When the main database fills up: pick a property and a date, **Preview**, then **Move**. Old, finished records
  are copied to the archive, checked, and only then removed from the main database (all or nothing).
- What moves: guests, stays, payments, expenses, logs (hotel); residents, tenancies, rent, bed history, fixed
  complaints, expenses, logs (PG / co-living). Only finished and fully settled ones: nothing with money still to
  collect or a refund waiting.
- What never moves: properties, rooms, floors, beds, room types, messages, settings, users.
- Archived guests and residents are still found with **Find a person**, read-only, with their old stays.

---

## 7. Hotel properties

[Asked] Same as BSR Corridors today. Everything in requirements v1 and the later additions applies per property:
room types created by the user, rooms per floor and room type, stays, calendar, extensions and moves, check-in/out,
payments, UPI payment links, invoices, WhatsApp messages, expenses, maintenance, revenue and settlements, typo checks,
exports, backups, Testing page.

**Changes from today:**

- **Floors become a list** (name and order) instead of a text field, so rooms are grouped by floor everywhere.
  [Asked: "rooms based on floors"]
- Room types get an **optional** default price per night. [Decided: S8]
- The money password moves from Render to the property's settings (§3.3).

---

## 8. PG and Co-living properties (built first)

PG and Co-living work the same; differences are settings (men / women / co-ed, whole-room booking allowed).
[Decided: D5]

### 8.1 Structure [Asked]

```
Property
 └── Floor (Ground, 1st, 2nd …)
      └── Room (201, 202 …)  — type, AC, bathroom, usual advance
           └── Bed (A, B, C …)  — current resident, past residents
```

**Room:**

| Field | Notes |
|---|---|
| Room number | Unique in the property |
| Floor | |
| Room type | User-created, e.g. "2-sharing AC" |
| Number of beds | Set when created; changed with *Add bed* / *Remove bed* |
| AC / Non-AC, attached bathroom, balcony | [Suggested] |
| Usual advance | Suggested at move-in, can be changed there [Suggested] |
| Rent | **Not set on the room or bed.** Rent differs bed to bed and person to person: it is agreed with each resident at move-in (§8.4). [Decided 24-11-2026] |
| Status | Active / Under maintenance |

**Bed:**

| Field | Notes |
|---|---|
| Bed label | A, B, C … (automatic, editable) |
| Status | Vacant / Occupied / Reserved / Under maintenance / **Removed** |
| Notes | |

### 8.2 Adding and removing beds [Asked]

- **Add bed:** adds the next label to the room.
- **Remove bed:** only when the bed is **vacant** (no current or future resident). The bed is **not deleted**: it
  moves to the room's **Removed beds** list with removed date, removed by, reason and its full resident history.
- A removed bed can be **restored**. [Suggested]
- Reports for past months still count a removed bed for the time it was in use.

### 8.3 Residents [Asked + Suggested]

One resident per phone number (like hotel guests).

| Field | Required | Notes |
|---|---|---|
| Name, phone (WhatsApp) | ✔ | |
| Permanent address | ✔ at move-in | |
| ID proof type and number | ✔ at move-in | Encrypted, masked; ID photos in Drive |
| Photo | [Suggested] | |
| Date of birth, gender | [Suggested] | |
| Student / working, college or company | [Suggested] | |
| Emergency contact: name, relation, phone | [Suggested] | Standard for PGs |
| Police verification done (date) | [Suggested] | Many cities require it |
| Vehicle number | [Suggested] | For parking |

### 8.4 Tenancy: one resident in one bed

| Field | Notes |
|---|---|
| Resident, bed | |
| **Billing cycle** | **Daily, weekly or monthly** [Asked] |
| **Rent due date** | **Chosen per tenancy at move-in**, agreed between manager and resident [Decided: D7]: either *on the move-in date each cycle* (e.g. the 14th every month) or *a fixed date* (e.g. the 1st, first period pro-rated) |
| Rent per cycle | **Agreed with this resident** and typed in at move-in (no default from the room or bed) [Decided 24-11-2026]. **Includes electricity and food**; no separate charges [Decided: D8, D9] |
| Advance / security deposit | [Asked] Refundable, kept apart from rent |
| Move-in date | |
| Move-out date | Empty while staying |
| Notice period | e.g. 30 days for monthly [Suggested] |
| Agreement | Photo or PDF of the rental agreement [Suggested] |
| Status | Reserved → Staying → On notice → Moved out, or Cancelled |

**Rules**

- **One resident per bed at a time,** enforced by the database (like the hotel's no-double-booking rule).
- A resident can **shift beds** (same or another room) without a new tenancy; history shows both. [Suggested]
- **Reserve a bed** for a future move-in date, with a token advance. [Suggested]

### 8.5 Rent and payments

- **Rent dues are created automatically** on each due date, by the tenancy's cycle and chosen due date.
- Payments recorded against dues: cash, UPI, card, bank. **Part payments** allowed; balance carries forward.
- **Rent reminders on WhatsApp** (click-to-send): due today, overdue. [Suggested]
- **UPI payment links** (as the hotel's), prefilled with the amount due. [Suggested]
- **Rent receipts** (PDF) for residents who need them, e.g. for HRA claims. [Suggested]
- No electricity or food charges. [Decided: D8, D9]

### 8.6 Moving out [Decided: D10]

1. Resident gives notice → **On notice**, last day set.
2. On the last day: final dues worked out (rent until the last day).
3. **Deposit settlement:** deposit − unpaid dues − deductions (standard ones from the property's settings plus
   any extra, each with a reason) = refund, due within the property's refund period. Some or all of the rent owed
   can be **waived** at move-out, with a reason; waived rent is never counted as rent received. [Asked]
4. The refund is recorded when paid; until then it shows on the dashboard as *deposit to refund*.
5. Bed becomes vacant; the move-out WhatsApp message opens.
6. The tenancy and settlement stay in the bed's and resident's history.

### 8.7 Screens [Asked]

1. **Floors page:** a card per floor: rooms, beds occupied / total, vacant beds.
2. **Floor → Rooms:** a tile per room: number, type, a dot per bed (green vacant, blue occupied, orange on notice,
   grey maintenance), rent-due flag.
3. **Room → Beds:** a card per bed: current resident, since when, rent, dues. Below: **Removed beds** with date
   and reason.
4. **Bed → Details:** current resident with dues, payments and actions (collect rent, give notice, shift bed, move
   out), and **past residents** with dates.
5. [Suggested] **Today page:** vacant beds, move-ins and move-outs today, rent due today, overdue rent, notices ending
   this week, deposits to refund.
6. [Suggested] **Residents list** with search by name, phone, room.
7. [Suggested] **Vacancy view** to share with enquiries: vacant beds by room type and rent.

### 8.8 Money and expenses

- **Revenue** for PG/co-living counts rent for days stayed and paid, like the hotel's (received / held / pending),
  per property, behind its money password.
- **Expenses** work exactly as today, per property: categories, receipts in Drive, monthly fixed expenses,
  settlements. [Asked]

### 8.9 Complaints [Suggested]

A simple list per property: room, issue (water, Wi-Fi, cleaning…), raised date, status, fixed by. Linked to
maintenance: a complaint can put its bed or room under maintenance until it is fixed (put back in use then). When
fixed, a WhatsApp message to the resident is one tap away.

---

## 9. Theme per property [Asked]

- Each property has a **theme colour**, chosen when it's created and **changeable any time**.
- While choosing, a **live preview** shows the header, buttons, cards and a bed/room tile in that colour, in light
  and dark mode, before saving. [Asked]
- Colours come from a set of tested colours (readable, enough contrast in light and dark mode). [Suggested]
- Same layout everywhere; only colours, logo and name change.
- The header always shows the **property's logo, name and type badge**, so staff always know where they are.
  [Asked]
- A **switcher** in the header for users with more than one property. [Suggested]
- The properties home page uses a neutral theme.
- Invoices and receipts use the property's logo and colour.

---

## 10. Animations [Asked]

Light and quick, so the app feels smooth on a phone without slowing it down:

| Where | Animation |
|---|---|
| Properties home | Cards fade and rise in one after another; lift slightly on hover / tap. |
| Opening a property | The card's colour expands into the page header (theme transition). |
| Floors → Rooms → Beds | Each level slides in from the right; *Back* slides it out. |
| Bed tiles | Colour fades when status changes (vacant → occupied); a short pulse on overdue rent. |
| Add / remove bed | New bed pops in; removed bed folds away into *Removed beds*. |
| Numbers (occupancy, dues) | Count up when the page opens. |
| Saving | Button shows a spinner, then a tick; success messages slide in and fade out. |
| Theme preview | Colours change smoothly as you pick. |
| Loading | Skeleton placeholders instead of blank space. |

Rules:

- Each animation lasts 150–400 ms. Nothing blocks a tap.
- Phones with **"reduce motion"** switched on get no movement, only instant changes.
- Built with CSS only (no animation library), so pages stay light.

---

## 11. Group-wide pages [Asked + Suggested]

| Page | Who | Shows |
|---|---|---|
| Properties home | Everyone | §4: no money |
| **Properties overview** | Admin only | Occupancy per property (rooms or beds), vacancies, alerts, backup status, database usage. No money. [Decided: D6] |
| Person search | Admin only | Find a guest or resident by phone in any property. [Decided: S20] |
| Activity log | Admin only | Properties added, users and permissions changed, configuration changed. |

Money (revenue, profit, settlements) appears **only inside each property**, behind its money password.
[Decided: D6]

---

## 11a. Money privacy: the property owner's money [Asked, 2026-11-28]

Each property is a client: its amounts belong to its owner.

- **Every amount is hidden** (shown as ₹ ••••) — revenue, profit, expenses, a guest's bill and balance, rent,
  advances, settlements, logs and messages on screen — for anyone who hasn't opened that property's money.
  Guest pages (payment link, invoice, receipt) always show the guest's own amounts.
- Opening it needs **money access** for that property, its **money password** and a 6-digit code from the person's own
  **authenticator app** (Google / Microsoft Authenticator). It stays open 30 minutes, for that property only. A person
  with money access to two properties has two entries in their app.
- **The main admin is no exception.** He can't set or reset a property's money password, and Users & access no longer
  gives money access.
- **Money access page** (in each property): only an **owner** who has set up their authenticator can give or remove
  money access or make someone an owner, confirming with a code. Until a property has such an owner, the main admin
  sets it up once — that's how the first owner is named.
- The main admin **can reset someone's authenticator** (e.g. a lost phone) [Asked]. It is written in the property's log
  and the group's activity log, and the person sees it at their next unlock.
- Money actions need the unlock too: expenses, exports, invoice / receipt PDFs for staff, PG move-out.
- **Limits, said plainly:** whoever controls the hosting (Render), the database (Supabase) or a Drive backup can still
  read the data directly; WhatsApp messages carry the amounts the guest must see. The app makes every money view
  personal and logged; for stronger protection the owner should hold the Drive account of their property.

## 11b. Same-day re-booking, and files [Asked, 2026-11-28]

- A guest who checks out **on the day they arrived** keeps (and pays for) their night, but the room is **free again
  from that moment** for another guest the same night. Check-in and check-out times show on the stay.
- Guest IDs, expense receipts and resident documents can be **photos, PDFs, videos or any file**, up to 25 MB each.
  Photos, PDFs and videos open in the viewer; other files only download. Files wait in a list until **Upload all**;
  earlier files are reused by ticking them (**Select all**, **Use selected**).

---

## 12. Backups and restore

- Each property is backed up **every night to its own Google Drive** (7 daily, 4 weekly, 12 monthly), as today.
- The shared tables (users, permissions, encrypted configuration) are backed up to the **admin's** Google Drive.
- **Restore one property** without touching the others.
- The backups page shows the last result for every property; a failure shows an alert on the properties home page.
- The nightly GitHub Action triggers all backups, one property after another.

---

## 13. Protecting BSR Corridors [Decided]

**The owner's condition for approval: v2 must not affect the running BSR Corridors app.** These rules hold for the
whole build:

### 13.1 While v2 is being built

1. **v2 is built on a separate git branch** (`v2-cluster`). Render and Netlify deploy only `main`, so nothing on the
   branch reaches the live app.
2. **v2 is tested on a separate database**: the spare free Supabase project, filled with a copy of BSR Corridors'
   data (from a backup), never the live database. The live database is never written to by v2 code before the
   switch-over.
3. Fixes and small changes to BSR Corridors carry on as usual on `main`, and are brought into the v2 branch so
   nothing is lost.
4. You can try v2 at each phase on the test copy (locally, or as a separate test address) while BSR Corridors keeps
   running normally.

### 13.2 BSR Corridors' data stays where it is

- BSR Corridors' tables **stay in their current place** (the database's `public` schema). **No data is moved or
  renamed.** v2 only **adds** new shared tables (`cluster_items`, `cluster_memberships`) next to them, and new schemas for new properties.
- Its rooms, guests, stays, payments, expenses, history, backups and Google Drive files are untouched.
- Its Render settings (Drive, UPI, short link base, revenue password) are read as they are; they are copied into the
  property settings only at switch-over, and the old ones keep working as a fallback.

### 13.3 Switch-over (only when you say so)

The live app changes **once**, after all v2 phases you want are finished, tested on the copy, and approved by you:

1. Full backup to Google Drive, checked.
2. Merge `v2-cluster` into `main`; Render deploys. The only database changes are **additions** (the shared tables
   and new tables); no existing table is dropped or rewritten.
3. Check: every count and total (rooms, guests, stays, payments, expenses, revenue per month) matches the backup.
4. **Old links keep working:** the existing admin address, invoice, payment and short links already sent to guests.
5. Same logins, same passwords; BSR Corridors opens as the first property.

### 13.4 If anything goes wrong

- **Roll back** by redeploying the previous version in Render (one click). Because v2 only adds tables, the old
  version works on the same database without any restore.
- The backup from step 1 is the last resort.

## 14. Non-functional requirements

| Area | Requirement |
|---|---|
| Cost | **Free only** [Decided]: one Render free app, one Supabase free project, Google Drive, GitHub Actions. Any paid option is proposed with its price first. |
| Phone first | Every screen works at phone width; tiles are easy to tap. |
| Speed | Pages open in under 2 seconds on 4G when the app is awake. |
| Memory | Fits in Render free (512 MB) with all properties. |
| Isolation | Tests prove no page, export, backup or message can show another property's data. |
| Privacy | ID numbers and secrets encrypted; masked on screen; access logged. |
| Reliability | One property failing (e.g. Drive disconnected) doesn't affect the others. |
| Auditability | Every change logged with user, property and time. |
| Compatibility | All current BSR Corridors features keep working throughout. |
| Motion | Animations respect "reduce motion" (§10). |

---

## 15. Decisions

| # | Question | Answer |
|---|---|---|
| D1 | Names | Properties / Property (renamed from Cluster / Cluster item on 24-11-2026) ✔ |
| D2 | Logins | One login per person, access per property ✔ |
| D3 | OTP sign-in | Not now ✔ |
| D4 | Databases | Free only; see D13 |
| D5 | PG vs Co-living | Same features, different settings ✔ |
| D6 | Money on properties home | No; inside each property only ✔ |
| D7 | Rent due date | Chosen per tenancy at move-in ✔ |
| D8 | Electricity | Included in rent ✔ |
| D9 | Food | No charges ✔ |
| D10 | Deposit refund rules | Yes: refund period and standard deductions per property ✔ |
| D11 | [Suggested] items | All yes except S16 (late fee: no) ✔ — see below |
| D12 | Built first | PG and Co-living ✔ |
| D13 | Database design | One Supabase database, one schema per property ✔ |

**D11 answers**

| # | Suggested item | Section | Answer |
|---|---|---|---|
| S1 | Go straight into the property when a user has only one | 3.1 | Yes |
| S2 | Per-user switches: Money access, Can see full ID numbers, Can delete | 3.2 | Yes |
| S3 | Today's numbers and alert badges on properties home cards | 4 | Yes |
| S4 | Archive properties; card order | 4 | Yes |
| S5 | Business details: legal name, GSTIN, PAN, Udyam, invoice prefix | 5.1 | Yes, all optional |
| S6 | Guided database move | 5.3 | Yes |
| S7 | Database usage display and 80 % warning | 6.2 | Yes, required |
| S8 | Default price per night for hotel room types | 7 | Yes, optional field |
| S9 | Room extras (AC, bathroom, balcony), usual advance per room (rent: agreed per resident instead of per bed, changed 24-11-2026) | 8.1 | Yes |
| S10 | Restore removed beds | 8.2 | Yes |
| S11 | Resident extras: photo, DOB, gender, student/working, emergency contact, police verification, vehicle | 8.3 | Yes |
| S12 | Notice period, agreement upload | 8.4 | Yes |
| S13 | Shift beds; reserve a bed with token advance | 8.4 | Yes |
| S14 | Rent reminders on WhatsApp; UPI payment links for rent | 8.5 | Yes |
| S15 | Rent receipts (PDF) | 8.5 | Yes |
| S16 | ~~Optional late fee~~ | — | **No** (removed) |
| S17 | PG Today page, residents list, vacancy view | 8.7 | Yes |
| S18 | Complaints list | 8.9 | Yes |
| S19 | Header switcher between properties | 9 | Yes |
| S20 | Properties overview, person search, activity log | 11 | Yes, admin only |

---

## 16. Build order [Decided: approved]

All phases are built on the `v2-cluster` branch and tested on the copy database (§13). Each phase ends with tests
and your approval before the next. **The live BSR Corridors app changes only at the switch-over (§13.3), when you
approve it.**

| Phase | Delivers |
|---|---|
| **1. Foundation** | Properties, a schema per property, BSR Corridors registered as the first property (data untouched), users with per-property roles, per-property money password, properties home page, themes with preview, animations base. |
| **2. Property set-up** | Add / edit property, encrypted configuration, Connect Google Drive, tables created by type, table changes applied to all properties of a type, per-property backups. |
| **3. PG / Co-living core** | Floors, rooms, beds (add / remove / restore), residents, tenancies with chosen due dates, the four-level screens, move-in and move-out with deposit settlement. |
| **4. Rent and money** | Automatic rent dues, part payments, reminders, UPI links, receipts, PG revenue and expenses (as today). |
| **5. Extras** | Complaints list, vacancy view, person search, activity log, properties overview, and any accepted item not finished in earlier phases. |

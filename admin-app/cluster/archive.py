"""The archive database: a second (free) Postgres database where old records go when the main one fills up.

Only records that are finished go: stays checked out / cancelled before a date and fully paid (with their payments,
other guests and ID photo links), guests with nothing left, expenses, logs; for a PG: moved-out tenancies with no
rent owed and no refund waiting (with their beds' history, rent periods, payments, links), residents with nothing
left, fixed complaints. Standard records — properties, rooms, floors, beds, messages, settings — never go: they stay
in the main database (the archive only refers to them by number).

Each record is kept as JSON (the same format as the backups, ID numbers still encrypted), in one table:
    bsr_archive(property, model, pk, day, search, data)
so it doesn't depend on the app's table layout. A move copies first, checks every record arrived, and only then
deletes from the main database — in one transaction, so it's all or nothing.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime

import dj_database_url
from django.core import serializers
from django.db import connections, transaction
from django.db.models.deletion import Collector

from . import current
from .models import Item

ALIAS = "archive"
LIMIT = 500  # top-level records per move (press again for more), so a move never runs too long
TABLE_SQL = """
CREATE TABLE IF NOT EXISTS bsr_archive (
    id bigserial PRIMARY KEY,
    property varchar(12) NOT NULL,
    model varchar(40) NOT NULL,
    pk varchar(40) NOT NULL,
    day date,
    search text NOT NULL DEFAULT '',
    data jsonb NOT NULL,
    archived_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (property, model, pk)
);
CREATE INDEX IF NOT EXISTS bsr_archive_kind ON bsr_archive (property, model);
"""


class ArchiveError(Exception):
    pass


# ── The connection ────────────────────────────────────────────────────────────


def store():
    from .models import ArchiveStore
    return ArchiveStore.objects.first()


def _alias() -> str:
    """The archive's connection name (tests point it at their own database)."""
    s = store()
    if s is None or not s.db_url:
        raise ArchiveError("No archive database connected yet.")
    _register(s.db_url)
    return ALIAS


def _register(url: str, name: str = ALIAS) -> str:
    if name in connections.settings:
        return name
    base = dict(connections["default"].settings_dict)
    own = dj_database_url.parse(url)
    base.update({k: own[k] for k in ("ENGINE", "NAME", "USER", "PASSWORD", "HOST", "PORT") if k in own})
    base["OPTIONS"] = own.get("OPTIONS", {})
    base.update({"CONN_MAX_AGE": 0, "ATOMIC_REQUESTS": False, "TEST": {**base.get("TEST", {}), "NAME": None}})
    base.pop(current.SCHEMA_KEY, None)
    connections.settings[name] = base
    return name


def _forget(name: str = ALIAS) -> None:
    conn = getattr(connections._connections, name, None)
    if conn is not None:
        conn.close()
        try:
            delattr(connections._connections, name)
        except AttributeError:
            pass
    connections.settings.pop(name, None)


def connect(url: str, by: str = "") -> None:
    """Check the link works and is Postgres, create the archive table, and remember the link (encrypted)."""
    from .models import ArchiveStore
    url = (url or "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        raise ArchiveError("Use a Postgres connection link (postgresql://…).")
    test = _register(url, "archive_check")
    try:
        with connections[test].cursor() as cur:
            cur.execute("SELECT 1")
            cur.execute(TABLE_SQL)
    except Exception as exc:
        raise ArchiveError(f"Can’t connect to that database: {exc}") from exc
    finally:
        _forget("archive_check")
    _forget()
    s = store() or ArchiveStore()
    s.db_url, s.connected_by = url, by
    s.save()


def disconnect() -> None:
    from .models import ArchiveStore
    _forget()
    ArchiveStore.objects.all().delete()


def usage() -> dict | None:
    """The archive database's size (free Supabase: 500 MB) and records per property."""
    from django.conf import settings
    try:
        alias = _alias()
        with connections[alias].cursor() as cur:
            cur.execute("SELECT pg_database_size(current_database())")
            used = cur.fetchone()[0] / 1024 / 1024
            cur.execute("SELECT property, count(*) FROM bsr_archive GROUP BY property")
            per = dict(cur.fetchall())
    except ArchiveError:
        return None
    except Exception as exc:
        return {"error": str(exc)[:200]}
    limit = getattr(settings, "DB_LIMIT_MB", 500)
    return {"used": round(used, 1), "limit": limit, "pct": min(round(used / limit * 100), 100),
            "warn": used / limit >= 0.8, "per": per}


# ── What goes ─────────────────────────────────────────────────────────────────


@dataclass
class Plan:
    """Records to move (deleted from the main database after copying), in the order they're deleted, and records
    only copied (e.g. a guest who still has newer stays, so their archived stays keep their name)."""
    move: list = field(default_factory=list)  # [(label, [objects])] children first
    copy: list = field(default_factory=list)
    more: bool = False  # more than LIMIT: press again

    @property
    def counts(self) -> list[tuple[str, int]]:
        return [(label, len(objs)) for label, objs in self.move if objs]

    @property
    def total(self) -> int:
        return sum(len(objs) for _, objs in self.move)


def plan(item: Item, before: date) -> Plan:
    with current.using(item):
        return _plan_hotel(before) if item.kind == Item.Kind.HOTEL else _plan_pg(before)


def _plan_hotel(before: date) -> Plan:
    from django.db.models import Q
    from core.models import (AuditLog, BackupRun, Expense, Guest, IdPhoto, MaintenanceLog, Payment, PaymentRequest,
                             Stay, StayGuest, StayIdPhoto)
    S = Stay.Status
    done = {S.CHECKED_OUT, S.CANCELLED, S.TRANSFERRED}
    old = (Stay.objects.filter(status__in=done)
           .filter(Q(check_out__lt=before) | Q(check_out__isnull=True, check_in__lt=before)))
    old_ids = set(old.values_list("pk", flat=True))
    stays, seen, roots = [], set(), 0
    p = Plan()
    for s in old.filter(linked_to__isnull=True).order_by("check_in", "pk"):
        chain = s.chain()  # a stay moved between rooms goes together, or not at all
        if any(x.pk not in old_ids for x in chain) or sum((x.balance for x in chain if not x.is_void), 0) > 0:
            continue  # still running somewhere, or money still to collect
        if roots >= LIMIT:
            p.more = True
            break
        roots += 1
        for x in chain:
            if x.pk not in seen:
                seen.add(x.pk)
                stays.append(x)
    ids = [s.pk for s in stays]
    guest_ids = {s.guest_id for s in stays}
    keep = set(Stay.objects.filter(guest_id__in=guest_ids).exclude(pk__in=ids).values_list("guest_id", flat=True))
    gone = [g for g in Guest.objects.filter(pk__in=guest_ids - keep)]
    photos = list(IdPhoto.objects.filter(guest__in=gone))
    p.move = [
        ("ID photo links", list(StayIdPhoto.objects.filter(Q(stay_id__in=ids) | Q(photo__in=photos)))),
        ("Other guests on stays", list(StayGuest.objects.filter(stay_id__in=ids))),
        ("Payment links", list(PaymentRequest.objects.filter(stay_id__in=ids))),
        ("Payments", list(Payment.objects.filter(stay_id__in=ids))),
        ("Stays", sorted(stays, key=lambda s: (s.linked_to_id is None, s.pk))),  # continuations first
        ("ID photos", photos),
        ("Guests", gone),
        ("Expenses", list(Expense.objects.filter(date__lt=before)[:LIMIT * 10])),
        ("Room upkeep (done)", list(MaintenanceLog.objects.filter(date__lt=before, done=True)[:LIMIT * 10])),
        ("Activity log", list(AuditLog.objects.filter(at__date__lt=before)[:LIMIT * 20])),
        ("Backup runs", list(BackupRun.objects.filter(started_at__date__lt=before)[:LIMIT * 10])),
    ]
    p.copy = list(Guest.objects.filter(pk__in=keep))
    return p


def _plan_pg(before: date) -> Plan:
    from django.db.models import Q
    from core.models import AuditLog, BackupRun, Expense
    from pg import money
    from pg.models import BedSpell, Complaint, RentDue, RentPayment, RentRequest, Resident, Tenancy
    T = Tenancy.Status
    p = Plan()
    tenancies = []
    qs = (Tenancy.objects.filter(Q(status=T.MOVED_OUT, move_out__lt=before) | Q(status=T.CANCELLED, move_in__lt=before))
          .order_by("move_in", "pk"))
    for t in qs:
        if t.refund_pending or money.balance(t) > 0:
            continue  # money still to settle
        if len(tenancies) >= LIMIT:
            p.more = True
            break
        tenancies.append(t)
    ids = [t.pk for t in tenancies]
    res_ids = {t.resident_id for t in tenancies}
    keep = set(Tenancy.objects.filter(resident_id__in=res_ids).exclude(pk__in=ids).values_list("resident_id", flat=True))
    p.move = [
        ("Complaints (fixed)", list(Complaint.objects.filter(status=Complaint.Status.FIXED, fixed_on__lt=before)
                                    .filter(Q(tenancy__isnull=True) | Q(tenancy_id__in=ids))[:LIMIT * 10])),
        ("Payment links", list(RentRequest.objects.filter(tenancy_id__in=ids))),
        ("Rent payments", list(RentPayment.objects.filter(tenancy_id__in=ids))),
        ("Rent periods", list(RentDue.objects.filter(tenancy_id__in=ids))),
        ("Bed history", list(BedSpell.objects.filter(tenancy_id__in=ids))),
        ("Tenancies", tenancies),
        ("Residents", list(Resident.objects.filter(pk__in=res_ids - keep))),
        ("Expenses", list(Expense.objects.filter(date__lt=before)[:LIMIT * 10])),
        ("Activity log", list(AuditLog.objects.filter(at__date__lt=before)[:LIMIT * 20])),
        ("Backup runs", list(BackupRun.objects.filter(started_at__date__lt=before)[:LIMIT * 10])),
    ]
    p.copy = list(Resident.objects.filter(pk__in=keep))
    return p


# ── Moving ────────────────────────────────────────────────────────────────────

DAY_FIELDS = ("check_in", "move_in", "date", "paid_on", "start", "raised_on", "at", "started_at")


def _row(code: str, d: dict, obj) -> tuple:
    day = None
    for name in DAY_FIELDS:
        value = getattr(obj, name, None)
        if value:
            day = value.date() if isinstance(value, datetime) else value
            break
    search = ""
    if d["model"] in ("core.guest", "pg.resident"):
        digits = "".join(c for c in (getattr(obj, "phone", "") or "") if c.isdigit())
        search = f"{obj.name.lower()} {digits}"
    return code, d["model"], str(d["pk"]), day, search, json.dumps(d)


def _write(alias: str, code: str, objs: list) -> set:
    if not objs:
        return set()
    data = json.loads(serializers.serialize("json", objs))  # ID numbers stay encrypted (as in backups)
    rows = [_row(code, d, o) for d, o in zip(data, objs)]
    with connections[alias].cursor() as cur:
        cur.executemany(
            "INSERT INTO bsr_archive (property, model, pk, day, search, data) VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (property, model, pk) DO UPDATE SET day = EXCLUDED.day, search = EXCLUDED.search, "
            "data = EXCLUDED.data, archived_at = now()", rows)
    return {(r[1], r[2]) for r in rows}


def _present(alias: str, code: str, keys: set) -> int:
    if not keys:
        return 0
    with connections[alias].cursor() as cur:
        cur.execute("SELECT count(*) FROM bsr_archive WHERE property = %s AND (model, pk) IN "
                    "(SELECT * FROM unnest(%s::text[], %s::text[]))",
                    [code, [k[0] for k in keys], [k[1] for k in keys]])
        return cur.fetchone()[0]


def move(item: Item, before: date) -> Plan:
    """Copy the old records to the archive, check every one arrived, then delete them here. Returns what moved."""
    if before > date.today():
        raise ArchiveError("Pick a date that isn't in the future.")
    alias = _alias()
    p = plan(item, before)
    if not p.total:
        return p
    with current.using(item):
        here = current.alias()
        with transaction.atomic(using=alias):
            with connections[alias].cursor() as cur:
                cur.execute(TABLE_SQL)
            keys = set()
            for _, objs in p.move:
                keys |= _write(alias, item.code, objs)
            _write(alias, item.code, p.copy)
            if _present(alias, item.code, keys) != len(keys):
                raise ArchiveError("Not every record reached the archive — nothing was deleted.")
        # Copied and checked: now delete here, children first. Anything a delete would take with it must have been
        # copied too, or nothing is deleted.
        with transaction.atomic(using=here):
            for _, objs in p.move:
                if not objs:
                    continue
                c = Collector(using=here)
                c.collect(objs)
                doomed = {(o._meta.label_lower, str(o.pk)) for objs_ in c.data.values() for o in objs_}
                for qs in c.fast_deletes:
                    doomed |= {(qs.model._meta.label_lower, str(pk)) for pk in qs.values_list("pk", flat=True)}
                if not doomed <= keys:
                    raise ArchiveError("Deleting would also remove records that weren’t archived — nothing was deleted.")
                c.delete()
    return p


# ── Reading the archive ───────────────────────────────────────────────────────


def find_people(q: str, digits: str) -> list[dict]:
    """Archived guests and residents matching a phone (4+ digits) or name."""
    try:
        alias = _alias()
    except ArchiveError:
        return []
    like = f"%{digits[-10:]}%" if digits else f"%{q.lower()}%"
    with connections[alias].cursor() as cur:
        cur.execute("SELECT id, property, model, data FROM bsr_archive WHERE model IN ('core.guest', 'pg.resident') "
                    "AND search LIKE %s ORDER BY id DESC LIMIT 50", [like])
        rows = cur.fetchall()
    out = []
    for rid, prop, model, data in rows:
        d = data if isinstance(data, dict) else json.loads(data)
        out.append({"id": rid, "property": prop, "model": model, "name": d["fields"].get("name", ""),
                    "phone": d["fields"].get("phone", "")})
    return out


def person(rid: int) -> dict | None:
    """An archived guest / resident with their archived stays or tenancies (and payments)."""
    alias = _alias()
    with connections[alias].cursor() as cur:
        cur.execute("SELECT property, model, pk, data FROM bsr_archive WHERE id = %s", [rid])
        row = cur.fetchone()
        if row is None or row[1] not in ("core.guest", "pg.resident"):
            return None
        prop, model, pk, data = row
        child, key = ("core.stay", "guest") if model == "core.guest" else ("pg.tenancy", "resident")
        cur.execute("SELECT pk, data FROM bsr_archive WHERE property = %s AND model = %s "
                    "AND data->'fields'->>%s = %s ORDER BY day", [prop, child, key, pk])
        children = cur.fetchall()
        pay_model, pay_key = ("core.payment", "stay") if model == "core.guest" else ("pg.rentpayment", "tenancy")
        paid = {}
        if children:
            cur.execute("SELECT data FROM bsr_archive WHERE property = %s AND model = %s "
                        "AND data->'fields'->>%s = ANY(%s)", [prop, pay_model, pay_key, [c[0] for c in children]])
            for (pd,) in cur.fetchall():
                pd = pd if isinstance(pd, dict) else json.loads(pd)
                paid.setdefault(str(pd["fields"][pay_key]), []).append(pd["fields"])
    load = lambda v: v if isinstance(v, dict) else json.loads(v)  # noqa: E731
    d = load(data)
    item = Item.objects.filter(code=prop).first()
    return {"property": item, "code": prop, "model": model, "fields": d["fields"],
            "children": [{"pk": c[0], "fields": load(c[1])["fields"], "payments": paid.get(c[0], [])}
                         for c in children]}

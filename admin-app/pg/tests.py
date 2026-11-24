"""PG / co-living: beds, residents, tenancies, move-in → notice → move-out with deposit settlement, and the pages.

The rent calendar tests need no database; everything else needs Postgres (PG tables live in the item's own schema).
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase

from cluster import current, provision
from cluster.models import Item, Membership
from core.services import today

from . import services
from .models import Bed, BedSpell, Floor, Resident, Room, RoomType, Tenancy

T = Tenancy.Status
R = Membership.Role

if connection.vendor == "postgresql":
    current.register(Item(code="pgthree", schema=provision.schema_name("pgthree")))


class RentCalendarTests(SimpleTestCase):
    def t(self, **kw):
        base = {"rent": Decimal("9000"), "move_in": date(2026, 1, 31), "cycle": "monthly", "due_rule": "move_in"}
        return Tenancy(**{**base, **kw})

    def test_monthly_on_move_in_date_keeps_the_31st(self):
        starts = [p.start for p in services.periods(self.t(), date(2026, 4, 30))]
        self.assertEqual(starts, [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])

    def test_fixed_date_first_period_pro_rated(self):
        t = self.t(move_in=date(2026, 3, 21), due_rule="fixed", due_day=1)
        p = services.periods(t, date(2026, 5, 1))
        self.assertEqual((p[0].start, p[0].end, p[0].part), (date(2026, 3, 21), date(2026, 4, 1), True))
        self.assertEqual(p[0].amount, Decimal("3194"))  # 11 of March's 31 days × 9000
        self.assertEqual([(x.start, x.amount) for x in p[1:]], [(date(2026, 4, 1), Decimal("9000")),
                                                               (date(2026, 5, 1), Decimal("9000"))])

    def test_last_period_charged_by_the_day(self):
        t = self.t(move_in=date(2026, 1, 10), last_day=date(2026, 3, 20))
        p = services.periods(t, date(2026, 12, 31))
        self.assertEqual(p[-1].start, date(2026, 3, 10))
        self.assertEqual(p[-1].end, date(2026, 3, 20))
        self.assertTrue(p[-1].part)
        self.assertEqual(p[-1].amount, Decimal("2903"))  # 10 of 31 days

    def test_weekly_and_daily(self):
        w = services.periods(self.t(cycle="weekly", rent=Decimal("2100"), move_in=date(2026, 1, 1)), date(2026, 1, 20))
        self.assertEqual([p.start.day for p in w], [1, 8, 15])
        d = services.periods(self.t(cycle="daily", rent=Decimal("400"), move_in=date(2026, 1, 1),
                                    move_out=date(2026, 1, 4)), date(2026, 2, 1))
        self.assertEqual(len(d), 3)

    def test_standard_deductions(self):
        item = Item(deductions="Cleaning: 500\nKey lost: ₹300\nnonsense\nZero: 0")
        self.assertEqual(services.standard_deductions(item),
                         [{"reason": "Cleaning", "amount": "500"}, {"reason": "Key lost", "amount": "300"}])


@skipUnless(connection.vendor == "postgresql", "PG tables live in an item's own schema (Postgres)")
class PgTests(TransactionTestCase):
    serialized_rollback = True
    databases = {"default", "item_pgthree"} if connection.vendor == "postgresql" else {"default"}

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        provision.drop(Item(code="pgthree", schema=provision.schema_name("pgthree")))

    def setUp(self):
        self.item = Item.objects.create(code="pgthree", name="BSR Ladies PG", kind=Item.Kind.PG, theme="rose",
                                        refund_days=10, deductions="Cleaning: 500")
        provision.provision(self.item)
        self.ctx = current.using(self.item)
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.floor = Floor.objects.create(name="1st", sort_order=1)
        self.type = RoomType.objects.create(name="2-sharing AC")
        self.room = Room.objects.create(number="201", floor=self.floor, room_type=self.type, deposit=10000)
        self.a, self.b = services.add_beds(self.room, 2)
        self.ria = Resident.objects.create(name="Ria", phone="+91 90000 11111", address="Hyderabad", id_number="1234 5678 9012")
        self.admin = get_user_model().objects.create_superuser("boss", password="boss-password-1")

    def tenancy(self, bed=None, move_in=None, resident=None, **kw):
        t = Tenancy(resident=resident or self.ria, bed=bed or self.a, move_in=move_in or today(), rent=Decimal("9000"),
                    deposit=Decimal("10000"), deposit_received=Decimal("10000"), **kw)
        return services.move_in(t, by="test")

    def state(self, bed):
        return services.bed_views([bed])[0].state

    # ── Rules ──

    def test_pg_tables_only_in_the_items_schema(self):
        with connections["default"].cursor() as cur:
            cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'pg\\_%%'")
            self.assertEqual(cur.fetchone()[0], 0)  # BSR Corridors' place is untouched
        self.assertEqual(Bed.objects.count(), 2)

    def test_beds_get_letters_and_states(self):
        self.assertEqual([self.a.label, self.b.label], ["A", "B"])
        self.tenancy()
        self.assertEqual(self.state(self.a), "occupied")
        self.assertEqual(self.state(self.b), "vacant")
        later = Resident.objects.create(name="Sam", phone="+91 90000 22222")
        self.tenancy(bed=self.b, resident=later, move_in=today() + timedelta(days=5))
        self.assertEqual(self.state(self.b), "reserved")

    def test_move_in_needs_address_and_id_but_a_reservation_doesnt(self):
        new = Resident.objects.create(name="New", phone="+91 90000 33333")
        with self.assertRaises(ValidationError):
            self.tenancy(resident=new)
        t = self.tenancy(resident=new, move_in=today() + timedelta(days=3))
        self.assertEqual(t.status, T.RESERVED)
        with self.assertRaises(ValidationError):
            services.confirm_move_in(t)  # still no ID

    def test_one_resident_per_bed(self):
        self.tenancy()
        other = Resident.objects.create(name="Sam", phone="+91 90000 22222", address="x", id_number="9")
        with self.assertRaises(ValidationError):
            self.tenancy(resident=other)
        # Even if the app's own check were skipped, the database refuses an overlap.
        t2 = Tenancy.objects.create(resident=other, bed=self.a, move_in=today(), rent=1)
        with self.assertRaises(IntegrityError), transaction.atomic(using=current.alias()):
            BedSpell.objects.create(tenancy=t2, bed=self.a, start=today() + timedelta(days=10))

    def test_same_resident_cant_take_two_beds(self):
        self.tenancy()
        with self.assertRaises(ValidationError):
            self.tenancy(bed=self.b)

    def test_shift_keeps_history_in_both_beds(self):
        t = self.tenancy(move_in=today() - timedelta(days=20))
        services.shift(t, self.b, today())
        self.assertEqual(self.state(self.a), "vacant")
        self.assertEqual(self.state(self.b), "occupied")
        self.assertEqual(list(t.spells.values_list("bed__label", flat=True)), ["A", "B"])
        self.assertEqual(self.a.spells.count(), 1)

    def test_remove_only_vacant_beds_and_restore(self):
        self.tenancy()
        with self.assertRaises(ValidationError):
            services.remove_bed(self.a, "broken")
        services.remove_bed(self.b, "Room made single", by="boss")
        self.b.refresh_from_db()
        self.assertTrue(self.b.is_removed)
        self.assertEqual(services.counts(services.bed_views(Bed.objects.all()))["total"], 1)
        self.assertEqual(self.room.next_label(), "C")  # B is kept for its history
        services.restore_bed(self.b)
        self.assertEqual(self.state(self.b), "vacant")

    def test_notice_move_out_and_deposit_settlement(self):
        t = self.tenancy(move_in=today() - timedelta(days=60))
        services.give_notice(t, today(), None)
        self.assertEqual(t.last_day, today() + timedelta(days=30))
        self.assertEqual(self.state(self.a), "notice")
        services.move_out(t, today(), unpaid_rent=Decimal("2000"),
                          deductions=[{"reason": "Cleaning", "amount": "500"}, {"reason": "", "amount": "0"}])
        t.refresh_from_db()
        self.assertEqual(t.status, T.MOVED_OUT)
        self.assertEqual(t.refund, Decimal("7500"))
        self.assertEqual(t.refund_due_by, today() + timedelta(days=10))  # the item's refund period
        self.assertEqual(t.deductions, [{"reason": "Cleaning", "amount": "500"}])
        self.assertEqual(self.state(self.a), "vacant")
        self.assertTrue(t.refund_pending)
        services.refund_paid(t, today(), "UPI")
        self.assertFalse(Tenancy.objects.get(pk=t.pk).refund_pending)

    def test_owing_more_than_the_deposit(self):
        t = self.tenancy(move_in=today() - timedelta(days=60))
        services.move_out(t, today(), unpaid_rent=Decimal("12000"))
        self.assertEqual(t.refund, Decimal("-2000"))
        self.assertIsNone(t.refund_due_by)

    def test_cancel_reservation_frees_the_bed(self):
        t = self.tenancy(move_in=today() + timedelta(days=4))
        services.cancel(t, "changed plans")
        self.assertEqual(self.state(self.a), "vacant")
        self.tenancy(move_in=today() + timedelta(days=4))  # someone else can book the same dates

    # ── Pages ──

    def test_pg_pages_and_hotel_pages_stay_apart(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get("/p/pgthree/"), "/p/pgthree/pg/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/p/pgthree/stays/"), "/p/pgthree/pg/", fetch_redirect_response=False)
        home = self.client.get("/p/pgthree/pg/")
        self.assertContains(home, "0/2")  # beds taken / total
        self.assertContains(home, 'href="/p/pgthree/pg/floor/')
        self.assertEqual(self.client.get("/pg/").status_code, 404)  # BSR Corridors is a hotel
        self.assertContains(self.client.get(f"/p/pgthree/pg/room/{self.room.pk}/"), "Bed A")

    def test_move_in_page_end_to_end(self):
        self.client.force_login(self.admin)
        r = self.client.post(f"/p/pgthree/pg/bed/{self.b.pk}/move-in/", {
            "r-phone": "+91 98765 43210", "r-name": "Anu", "r-address": "Vizag", "r-id_type": "aadhaar",
            "r-id_number": "1111 2222 3333", "t-move_in": today().isoformat(), "t-cycle": "monthly",
            "t-due_rule": "fixed", "t-due_day": "5", "t-rent": "9000", "t-deposit": "10000",
            "t-deposit_received": "10000", "t-notice_days": "30"})
        t = Tenancy.objects.get(resident__name="Anu")
        self.assertRedirects(r, f"/p/pgthree/pg/tenancy/{t.pk}/", fetch_redirect_response=False)
        self.assertEqual((t.status, t.due_day, t.created_by), (T.STAYING, 5, "boss"))
        page = self.client.get(f"/p/pgthree/pg/tenancy/{t.pk}/")
        self.assertContains(page, "On the 5 of every month")
        self.assertNotContains(page, "1111 2222 3333")  # full ID never on the page
        # Move out with the standard deduction filled in.
        mo = self.client.get(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/")
        self.assertContains(mo, 'value="Cleaning"')
        self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/", {"day": today().isoformat(), "unpaid_rent": "0",
                                                                    "reason": ["Cleaning", ""], "amount": ["500", ""]})
        t.refresh_from_db()
        self.assertEqual((t.status, t.refund), (T.MOVED_OUT, Decimal("9500")))

    def test_rent_is_agreed_per_resident(self):
        self.client.force_login(self.admin)
        base = {"r-phone": "+91 98765 00001", "r-name": "Anu", "r-address": "Vizag", "r-id_type": "aadhaar",
                "r-id_number": "1111", "t-move_in": today().isoformat(), "t-cycle": "monthly", "t-due_rule": "move_in",
                "t-deposit": "10000", "t-deposit_received": "0", "t-notice_days": "30"}
        form = self.client.get(f"/p/pgthree/pg/bed/{self.b.pk}/move-in/")
        self.assertContains(form, 'value="10000.00"')  # the room's usual advance is suggested…
        self.assertNotContains(form, 'name="t-rent" value=')  # …but no rent: it's agreed with each resident
        r = self.client.post(f"/p/pgthree/pg/bed/{self.b.pk}/move-in/", base)
        self.assertEqual(r.status_code, 200)  # rent missing
        self.assertFalse(Tenancy.objects.exists())
        self.client.post(f"/p/pgthree/pg/bed/{self.b.pk}/move-in/", {**base, "t-rent": "8500"})
        self.tenancy()  # bed A, same room, 9000
        self.assertEqual(sorted(Tenancy.objects.values_list("rent", flat=True)), [Decimal("8500"), Decimal("9000")])

    def test_roles(self):
        user = get_user_model().objects.create_user("desk", password="x" * 12)
        Membership.objects.create(user=user, item=self.item, role=R.DESK)
        self.client.force_login(user)
        self.assertEqual(self.client.get("/p/pgthree/pg/").status_code, 200)
        self.assertEqual(self.client.post(f"/p/pgthree/pg/bed/{self.b.pk}/remove/", {"reason": "x"}).status_code, 403)
        self.assertEqual(self.client.post(f"/p/pgthree/pg/residents/{self.ria.pk}/reveal-id/").status_code, 403)
        t = self.tenancy(move_in=today() - timedelta(days=40))
        services.move_out(t, today())
        self.assertEqual(self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/refund/",
                                          {"f-paid_on": today().isoformat(), "f-mode": "UPI"}).status_code, 302)
        viewer = get_user_model().objects.create_user("viewer", password="x" * 12)
        Membership.objects.create(user=viewer, item=self.item, role=R.VIEWER)
        self.client.force_login(viewer)
        self.assertEqual(self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/notice/", {}).status_code, 403)

    def test_backup_has_the_pg_tables(self):
        from core.exports import backup_json
        self.tenancy()
        dump = backup_json().decode()
        for model in ("pg.floor", "pg.room", "pg.bed", "pg.resident", "pg.tenancy", "pg.bedspell"):
            self.assertIn(f'"model": "{model}"', dump)
        self.assertNotIn("1234 5678 9012", dump)  # ID numbers stay encrypted

    def test_cluster_home_counts_beds(self):
        self.tenancy()
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/properties/"), "beds taken")

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

from . import defaults, messages, money, services
from .models import Bed, BedSpell, Floor, PgMessage, Resident, Room, RoomType, Tenancy

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
        defaults.ensure_messages()  # the test runner empties the tables between tests
        self.floor = Floor.objects.create(name="1st", sort_order=1)
        self.type = RoomType.objects.create(name="2-sharing AC")
        self.room = Room.objects.create(number="201", floor=self.floor, room_type=self.type, deposit=10000)
        self.a, self.b = services.add_beds(self.room, 2)
        self.ria = Resident.objects.create(name="Ria", phone="+91 90000 11111", address="Hyderabad", id_number="1234 5678 9012")
        self.admin = get_user_model().objects.create_superuser("boss", password="boss-password-1")

    def tenancy(self, bed=None, move_in=None, resident=None, rent=Decimal("9000"), **kw):
        t = Tenancy(resident=resident or self.ria, bed=bed or self.a, move_in=move_in or today(), rent=rent,
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
        t = self.tenancy(move_in=today() - timedelta(days=14), cycle="weekly", rent=Decimal("2100"))
        self.assertEqual(money.balance(t), Decimal("6300"))  # weeks starting 14 and 7 days ago, and today
        services.give_notice(t, today(), today() + timedelta(days=3))
        self.assertEqual(money.balance(t), Decimal("5100"))  # this week charged by the day: 3 of 7 days = 900
        services.withdraw_notice(t)
        self.assertEqual(money.balance(t), Decimal("6300"))  # back to a full week
        services.give_notice(t, today(), None)
        self.assertEqual(t.last_day, today() + timedelta(days=30))
        self.assertEqual(self.state(self.a), "notice")
        money.record(t, Decimal("1000"), mode="cash")
        services.move_out(t, today(), deductions=[{"reason": "Cleaning", "amount": "500"}, {"reason": "", "amount": "0"}])
        t.refresh_from_db()
        self.assertEqual(t.status, T.MOVED_OUT)
        self.assertEqual(t.unpaid_rent, Decimal("3200"))  # two weeks (today's week dropped: they leave today) − 1000
        self.assertEqual(t.refund, Decimal("6300"))  # 10000 − 3200 − 500
        self.assertEqual(money.balance(t), 0)  # the rent was taken from the advance
        self.assertEqual(t.refund_due_by, today() + timedelta(days=10))  # the property's refund period
        self.assertEqual(t.deductions, [{"reason": "Cleaning", "amount": "500"}])
        self.assertEqual(self.state(self.a), "vacant")
        self.assertTrue(t.refund_pending)
        services.refund_paid(t, today(), "UPI")
        self.assertFalse(Tenancy.objects.get(pk=t.pk).refund_pending)

    def test_owing_more_than_the_deposit(self):
        t = self.tenancy(move_in=today() - timedelta(days=14), cycle="weekly", rent=Decimal("6000"))
        services.move_out(t, today())
        self.assertEqual(t.refund, Decimal("-2000"))
        self.assertIsNone(t.refund_due_by)
        self.assertEqual(money.balance(t), Decimal("2000"))  # still on the Today page as owing
        self.assertEqual([o.tenancy for o in money.owing()], [t])

    def test_rent_paid_ahead_comes_back_with_the_advance(self):
        t = self.tenancy(move_in=today() - timedelta(days=3), cycle="weekly", rent=Decimal("2100"))
        money.record(t, Decimal("2100"), mode="upi")
        services.move_out(t, today())
        self.assertEqual(t.unpaid_rent, Decimal("-1200"))  # 3 days used of the 2100 paid
        self.assertEqual(t.refund, Decimal("11200"))
        self.assertEqual(money.balance(t), 0)

    def test_waive_rent_at_move_out(self):
        t = self.tenancy(move_in=today() - timedelta(days=14), cycle="weekly", rent=Decimal("2100"))
        money.record(t, Decimal("1000"), mode="cash")
        with self.assertRaises(ValidationError):
            services.move_out(t, today(), waive=Decimal("500"))  # needs a reason
        with self.assertRaises(ValidationError):
            services.move_out(t, today(), waive=Decimal("5000"), waive_reason="Too much")  # only 3200 owed
        t.refresh_from_db()
        self.assertEqual(t.status, T.STAYING)  # nothing changed
        services.move_out(t, today(), waive=Decimal("1200"), waive_reason="Water problem")
        t.refresh_from_db()
        self.assertEqual((t.rent_waived, t.waive_reason), (Decimal("1200"), "Water problem"))
        self.assertEqual(t.unpaid_rent, Decimal("2000"))  # 4200 − 1000 paid − 1200 waived
        self.assertEqual(t.refund, Decimal("8000"))
        self.assertEqual(money.balance(t), 0)
        self.assertEqual(sum(ln.waived for ln in money.lines(t)), Decimal("1200"))
        # The waiver is neither rent received nor pending, and isn't cash in.
        first = today().replace(day=1)
        months = [money.month(first)] + ([money.month((first - timedelta(days=1)).replace(day=1))]
                                         if t.move_in < first else [])
        self.assertEqual(sum(m.received for m in months), Decimal("3000"))  # 1000 cash + 2000 from the advance
        self.assertEqual(sum(m.pending for m in months), 0)
        self.assertEqual(sum(m.collected for m in months), Decimal("1000"))
        self.assertEqual(months[0].waived, Decimal("1200"))
        self.assertIn("Rent waived: ₹1,200", messages.settlement_text(t))

    def test_dues_payments_and_rent_changes(self):
        t = self.tenancy(move_in=today() - timedelta(days=8), cycle="weekly", rent=Decimal("2000"))
        self.assertEqual(money.balance(t), Decimal("4000"))
        money.record(t, Decimal("3000"), mode="upi")
        states = [ln.state for ln in money.lines(t)]
        self.assertEqual(states, ["paid", "part"])  # oldest first
        self.assertEqual(money.unpaid_since(t), today() - timedelta(days=1))
        t.rent = Decimal("2500")  # agreed new rent: only periods from now on
        t.save()
        money.sync(t, today() + timedelta(days=6))
        self.assertEqual([d.amount for d in t.dues.all()], [Decimal("2000"), Decimal("2000"), Decimal("2500")])
        self.assertEqual(money.balance(t), Decimal("3500"))
        # The advance is the sum of advance payments.
        money.record(t, Decimal("500"), kind="deposit", mode="cash")
        t.refresh_from_db()
        self.assertEqual(t.deposit_received, Decimal("10500"))
        with self.assertRaises(ValidationError):
            money.record(t, Decimal("0"))

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
        self.assertContains(page, "On the 5th of every month")
        self.assertContains(page, "Payment received")  # the property's messages, as buttons
        self.assertNotContains(page, "1111 2222 3333")  # full ID never on the page
        # Move out with the standard deduction filled in.
        mo = self.client.get(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/")
        self.assertContains(mo, 'value="Cleaning"')
        self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/", {"day": today().isoformat(),
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
        from django.apps import apps
        money.record(self.tenancy(), Decimal("500"), mode="cash")
        dump = backup_json().decode()
        for model in apps.get_app_config("pg").get_models():  # every PG table with rows is in the backup
            if model.objects.exists():
                self.assertIn(f'"model": "{model._meta.label_lower}"', dump)
        for model in ("pg.bedspell", "pg.rentdue", "pg.rentpayment", "pg.pgmessage"):
            self.assertIn(f'"model": "{model}"', dump)
        self.assertNotIn("1234 5678 9012", dump)  # ID numbers stay encrypted

    def test_cluster_home_counts_beds(self):
        self.tenancy()
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/properties/"), "beds taken")

    # ── Rent and money pages ──

    def test_collect_payment_receipt_and_upi_link(self):
        Item.objects.filter(pk=self.item.pk).update(upi_id="bsrpg@ybl", upi_name="BSR Ladies PG")
        t = self.tenancy(move_in=today() - timedelta(days=2), cycle="weekly", rent=Decimal("2100"))
        self.client.force_login(self.admin)
        r = self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/rent/pay/", {"amount": "1000", "kind": "rent", "mode": "cash",
                                                                         "paid_on": today().isoformat(), "send_receipt": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("https://web.whatsapp.com/send?phone=919000011111"))
        self.assertIn("received", __import__("urllib.parse").parse.unquote(r["Location"]))
        p = t.payments.get(kind="rent")
        pdf = self.client.get(f"/p/pgthree/pg/payment/{p.pk}/receipt/")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        # A UPI link for the rest, paid by the resident (no login), then checked by staff.
        link = self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/rent/message/",
                                {"message": PgMessage.objects.get(use="pay_link").pk, "amount": "1100"})
        text = __import__("urllib.parse").parse.unquote(link["Location"])
        self.assertIn("₹1,100", text)
        req = t.requests.get()
        self.client.logout()
        token = money.pay_token(req)
        page = self.client.get(f"/p/pgthree/pay/rent/{token}/")
        self.assertContains(page, "₹1,100")
        self.assertEqual(self.client.get(f"/p/pgthree/pay/rent/{token}x/").status_code, 404)
        self.client.post(f"/p/pgthree/pay/rent/{token}/", {"paid": "1", "reference": "4123 4567 8901"})
        req.refresh_from_db()
        self.assertEqual((req.status, req.reference), ("claimed", "412345678901"))
        public = self.client.get(f"/p/pgthree/pay/receipt/{money.receipt_token(p)}/")
        self.assertTrue(public.content.startswith(b"%PDF"))
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/p/pgthree/pg/"), "Says paid")
        self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/rent/link-ok/", {"request": req.pk})
        self.assertEqual(money.balance(t), 0)

    def test_welcome_message_opens_after_move_in(self):
        self.client.force_login(self.admin)
        r = self.client.post(f"/p/pgthree/pg/bed/{self.b.pk}/move-in/", {
            "r-phone": "+91 98765 43210", "r-name": "Anu Rao", "r-address": "Vizag", "r-id_type": "aadhaar",
            "r-id_number": "1111", "t-move_in": today().isoformat(), "t-cycle": "monthly", "t-due_rule": "move_in",
            "t-rent": "8000", "t-deposit": "8000", "t-deposit_received": "8000", "t-deposit_mode": "upi",
            "t-notice_days": "30"}, HTTP_X_REQUESTED_WITH="fetch")
        d = r.json()
        self.assertTrue(d["ok"])
        text = __import__("urllib.parse").parse.unquote(d["wa"])
        self.assertIn("Welcome to BSR Ladies PG", text)
        self.assertIn("₹8,000 (monthly)", text)
        t = Tenancy.objects.get(resident__name="Anu Rao")
        self.assertEqual(t.payments.get().mode, "upi")  # the advance, as a payment
        self.assertEqual(money.balance(t), Decimal("8000"))  # first month due on the move-in day

    def test_money_page_needs_the_money_password(self):
        t = self.tenancy(move_in=today().replace(day=1), cycle="daily", rent=Decimal("300"))
        money.record(t, Decimal("300"), mode="cash")
        self.client.force_login(self.admin)
        r = self.client.get("/p/pgthree/pg/revenue/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/p/pgthree/revenue/", r["Location"])
        self.client.post("/p/pgthree/revenue/", {"password": "money-pass-1", "confirm": "money-pass-1"})
        self.assertRedirects(self.client.get("/p/pgthree/revenue/"), "/p/pgthree/pg/revenue/", fetch_redirect_response=False)
        page = self.client.get("/p/pgthree/pg/revenue/")
        self.assertContains(page, "Ria")
        m = money.month(today().replace(day=1))
        days = today().day
        self.assertEqual(m.received, Decimal("300"))  # day one paid
        self.assertEqual(m.pending, Decimal(300 * (days - 1)))  # the other days so far not paid
        self.assertEqual(m.collected, Decimal("300"))
        # Profit shared by the partners set on the property's page.
        from cluster.models import Partner
        from core.models import Settlement
        Partner.objects.create(item=self.item, name="Asha", share=Decimal("75"))
        Partner.objects.create(item=self.item, name="Bina", share=Decimal("25"))
        page = self.client.get("/p/pgthree/pg/revenue/")
        self.assertContains(page, "Asha <b>₹225</b> (75%) · Bina <b>₹75</b> (25%)")
        self.client.post("/p/pgthree/pg/revenue/settle/", {"month": today().replace(day=1).isoformat(),
                                                            "amount": "300", "notes": "Shared – Asha 75%, Bina 25%"})
        s = Settlement.objects.get()
        self.assertEqual((s.amount, s.received_total), (Decimal("300"), Decimal("300")))
        self.assertContains(self.client.get("/p/pgthree/pg/revenue/"), "₹0 left")
        self.assertRedirects(self.client.get("/p/pgthree/whatsapp/"), "/p/pgthree/pg/messages/",
                             fetch_redirect_response=False)


    # ── Phase 5: complaints, vacancies, the waiver on the move-out page, group pages reaching into a PG ──

    def test_complaints_hold_and_release_maintenance(self):
        from . import complaints as cs
        from .models import Complaint
        c1 = cs.raise_complaint(room=self.room, bed=self.b, category="water", details="No water", hold="bed")
        c2 = cs.raise_complaint(room=self.room, bed=self.b, category="furniture", hold="bed")
        self.assertEqual(self.state(self.b), "maintenance")
        self.assertEqual((c1.hold, c2.hold), ("bed", ""))  # the second found it already under maintenance
        cs.start(c1)
        with self.assertRaises(ValidationError):
            cs.start(c1)
        cs.fix(c2, fixed_by="Carpenter")
        self.assertEqual(self.state(self.b), "maintenance")  # still held by the first
        cs.fix(c1, fixed_by="Plumber", note="Pipe changed")
        self.assertEqual(self.state(self.b), "vacant")
        cs.reopen(c1)
        self.assertEqual(self.state(self.b), "maintenance")
        cs.fix(c1)
        # A whole room, while someone lives in it: the resident stays; the free bed can't be given meanwhile.
        self.tenancy()
        c3 = cs.raise_complaint(room=self.room, category="wifi", hold="room")
        self.assertEqual((self.state(self.a), self.state(self.b)), ("occupied", "maintenance"))
        cs.fix(c3)
        self.assertEqual(self.state(self.b), "vacant")
        with self.assertRaises(ValidationError):
            cs.raise_complaint(room=self.room, category="other", hold="bed")  # which bed?
        self.assertEqual(Complaint.objects.exclude(status="fixed").count(), 0)

    def test_complaint_pages(self):
        from .models import Complaint
        t = self.tenancy()
        self.client.force_login(self.admin)
        form = self.client.get(f"/p/pgthree/pg/complaints/new/?place=r{self.room.pk}")
        self.assertContains(form, "Bed 201-A — Ria")
        r = self.client.post("/p/pgthree/pg/complaints/new/", {"place": f"b{self.a.pk}", "category": "ac",
                                                               "details": "AC not cooling", "raised_on": today().isoformat()})
        self.assertRedirects(r, "/p/pgthree/pg/complaints/", fetch_redirect_response=False)
        c = Complaint.objects.get()
        self.assertEqual((c.tenancy, c.bed, c.room), (t, self.a, self.room))  # linked to who is in the bed
        self.assertContains(self.client.get("/p/pgthree/pg/"), "AC not cooling")  # on Today
        self.assertContains(self.client.get(f"/p/pgthree/pg/room/{self.room.pk}/"), "AC not cooling")
        self.assertContains(self.client.get("/p/pgthree/pg/complaints/"), "AC not cooling")
        self.client.post(f"/p/pgthree/pg/complaints/{c.pk}/fix/", {"fixed_by": "AC mechanic"})
        c.refresh_from_db()
        self.assertEqual((c.status, c.fixed_by, c.fixed_on), ("fixed", "AC mechanic", today()))
        self.assertNotContains(self.client.get("/p/pgthree/pg/complaints/"), "AC not cooling")  # open only
        self.assertContains(self.client.get("/p/pgthree/pg/complaints/?show=fixed"), "AC mechanic")
        wa = self.client.post(f"/p/pgthree/pg/complaints/{c.pk}/message/", HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertIn("has been fixed", wa["text"])
        self.assertIn("ac / geyser, bed 201-a", wa["text"])
        # View only: can look, can't raise or fix.
        viewer = get_user_model().objects.create_user("viewer", password="x" * 12)
        Membership.objects.create(user=viewer, item=self.item, role=R.VIEWER)
        self.client.force_login(viewer)
        self.assertEqual(self.client.get("/p/pgthree/pg/complaints/").status_code, 200)
        self.assertEqual(self.client.get("/p/pgthree/pg/complaints/new/").status_code, 403)
        self.assertEqual(self.client.post(f"/p/pgthree/pg/complaints/{c.pk}/reopen/").status_code, 403)

    def test_vacancy_view(self):
        c = services.add_beds(self.room, 1)[0]
        t = self.tenancy()
        services.give_notice(t, today(), today() + timedelta(days=5))
        groups = services.vacancies()
        self.assertEqual([(g.room_type.name, len(g.now), len(g.soon)) for g in groups], [("2-sharing AC", 2, 1)])
        self.assertEqual(groups[0].advance, Decimal("10000"))
        text = services.vacancy_text(groups, "BSR Ladies PG", self.item)
        self.assertIn("2 beds now — room 201", text)
        self.assertIn(f"1 bed free from {today() + timedelta(days=5):%d %b} — room 201", text)
        self.assertNotIn("Ria", text)  # no names
        self.assertNotIn("9,000", text)  # no rents
        self.client.force_login(self.admin)
        page = self.client.get("/p/pgthree/pg/vacancy/")
        self.assertContains(page, "wa.me/?text=")
        self.assertContains(page, str(c))

    def test_waive_on_the_move_out_page(self):
        t = self.tenancy(move_in=today() - timedelta(days=14), cycle="weekly", rent=Decimal("2100"))
        self.client.force_login(self.admin)
        page = self.client.get(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/")
        self.assertContains(page, "Waive some of the rent")
        r = self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/", {"day": today().isoformat(), "waive": "200"})
        self.assertContains(r, "Give a reason for waiving the rent.")
        self.client.post(f"/p/pgthree/pg/tenancy/{t.pk}/move-out/", {"day": today().isoformat(), "waive": "200",
                                                                    "waive_reason": "Fan broken a week"})
        t.refresh_from_db()
        self.assertEqual((t.status, t.rent_waived, t.unpaid_rent, t.refund),
                         (T.MOVED_OUT, Decimal("200"), Decimal("4000"), Decimal("6000")))
        tp = self.client.get(f"/p/pgthree/pg/tenancy/{t.pk}/")
        self.assertContains(tp, "Rent waived")
        self.assertContains(tp, "Fan broken a week")

    def test_group_pages_reach_the_pg(self):
        from cluster.models import Activity
        self.tenancy()
        from .complaints import raise_complaint
        raise_complaint(room=self.room, category="cleaning")
        self.client.force_login(self.admin)
        found = self.client.get("/properties/search/?q=11111")
        self.assertContains(found, "Ria")
        self.assertContains(found, "BSR Ladies PG")
        self.assertContains(found, f"/p/pgthree/pg/residents/{self.ria.pk}/")
        self.assertContains(self.client.get("/properties/search/?q=12"), "at least 4 digits")
        ov = self.client.get("/properties/overview/")
        self.assertContains(ov, "BSR Ladies PG")
        self.assertContains(ov, "1 open complaint")
        self.assertNotContains(ov, "9,000")  # no money (D6)
        self.client.post("/properties/pgthree/theme/", {"theme": "ocean"})
        self.assertTrue(Activity.objects.filter(action="property", item=self.item, summary__contains="Ocean").exists())
        self.assertContains(self.client.get("/properties/activity/"), "theme changed to Ocean")
        # Admin only.
        desk = get_user_model().objects.create_user("desk", password="x" * 12)
        Membership.objects.create(user=desk, item=self.item, role=R.OWNER)
        self.client.force_login(desk)
        for url in ("/properties/overview/", "/properties/search/?q=11111", "/properties/activity/"):
            self.assertRedirects(self.client.get(url), "/properties/", fetch_redirect_response=False)

    def test_move_old_tenancies_to_the_archive(self):
        from unittest import mock
        from cluster import archive
        from cluster.models import ArchiveStore
        from .models import RentPayment
        ArchiveStore.objects.create(db_url="postgresql://test/test")
        with connection.cursor() as cur:
            cur.execute(archive.TABLE_SQL)
        self.addCleanup(lambda: connection.cursor().execute("DROP TABLE IF EXISTS bsr_archive"))
        old = self.tenancy(move_in=today() - timedelta(days=800), cycle="monthly", rent=Decimal("9000"))
        services.move_out(old, today() - timedelta(days=790))  # settled from the advance, refund waiting
        mira = Resident.objects.create(name="Mira", phone="+91 90000 22222", address="Vizag", id_number="1")
        now = self.tenancy(bed=self.b, resident=mira)
        before = today() - timedelta(days=365)
        with mock.patch.object(archive, "_alias", lambda: "default"):
            self.assertEqual(archive.plan(self.item, before).total, 0)  # refund not paid yet: stays
            services.refund_paid(old, today(), "UPI")
            p = archive.plan(self.item, before)
            self.assertEqual(dict(p.counts)["Tenancies"], 1)
            archive.move(self.item, before)
        self.assertFalse(Tenancy.objects.filter(pk=old.pk).exists())
        self.assertFalse(Resident.objects.filter(pk=self.ria.pk).exists())
        self.assertFalse(RentPayment.objects.filter(tenancy_id=old.pk).exists())
        self.assertEqual(Tenancy.objects.get(), now)  # current ones untouched
        self.assertEqual(Bed.objects.count(), 2)  # beds and rooms never move
        self.assertEqual(self.state(self.a), "vacant")
        with connection.cursor() as cur:
            cur.execute("SELECT DISTINCT model FROM bsr_archive WHERE property = 'pgthree'")
            models = {r[0] for r in cur.fetchall()}
        self.assertTrue({"pg.tenancy", "pg.resident", "pg.rentpayment", "pg.rentdue", "pg.bedspell"} <= models)
        self.assertFalse(models & {"pg.bed", "pg.room", "pg.floor"})

    def test_settings_page_has_no_hotel_parts(self):
        self.client.force_login(self.admin)
        page = self.client.get("/p/pgthree/settings/")
        self.assertContains(page, "Partners")
        self.assertNotContains(page, "<h2>Hosts</h2>")
        self.assertNotContains(page, "Delete old records")
        r = self.client.post("/p/pgthree/settings/", {"confirm": "PURGE", "years": "1"})
        self.assertEqual(r.status_code, 404)

    def test_own_drive_folder_even_on_a_shared_google_account(self):
        from unittest import mock
        from django.test import override_settings
        from core import gdrive
        from core.models import AppSetting

        class FakeDrive:  # the Drive API, just enough: folders by id, names
            def __init__(self):
                self.names, self.n = {}, 0

            def files(self):
                return self

            def create(self, body, fields):
                self.n += 1
                fid = f"folder{self.n}"
                self.names[fid] = body["name"]
                return mock.Mock(execute=lambda: {"id": fid})

            def get(self, fileId, fields):
                return mock.Mock(execute=lambda: {"id": fileId, "name": self.names[fileId], "trashed": False})

            def update(self, fileId, body):
                self.names[fileId] = body["name"]
                return mock.Mock(execute=lambda: {})

        drive = FakeDrive()  # one Google account shared by two properties
        self.item.drive_token = "token"
        self.item.save()
        with override_settings(GOOGLE_WEB_CLIENT_ID="id", GOOGLE_WEB_CLIENT_SECRET="secret"), \
                mock.patch.object(gdrive, "_service", lambda: drive):
            self.assertEqual(gdrive.folder_url(), "")
            url = gdrive.ensure_folder()
            self.assertEqual(url, "https://drive.google.com/drive/folders/folder1")
            self.assertEqual(drive.names["folder1"], "BSR Ladies PG")  # named after the property
            self.assertEqual(gdrive.ensure_folder(), url)  # remembered, not made twice
            self.client.force_login(self.admin)
            page = self.client.get("/properties/pgthree/")
            self.assertContains(page, url)
            self.assertContains(page, "Open BSR Ladies PG’s folder in Google Drive")
            # Its own folder name, set on the property's page: the folder is renamed, files stay in it.
            self.item.drive_folder = "Ladies PG files"
            self.item.save()
            gdrive.ensure_folder()
            self.assertEqual(drive.names["folder1"], "Ladies PG files")
        # The folder is remembered per property: another property (here BSR Corridors) on the same Google account
        # doesn't see it, and makes its own.
        with current.using(Item.objects.get(is_default=True)):
            self.assertEqual(AppSetting.get("gdrive_root", ""), "")
        self.assertEqual(AppSetting.get("gdrive_root"), "folder1")

    def test_drive_sign_in_keeps_its_code_verifier(self):
        """Google's sign-in (PKCE): the code verifier made at Connect must be sent back when Google returns."""
        from unittest import mock
        from urllib.parse import parse_qs, urlparse
        from django.test import override_settings
        from google_auth_oauthlib.flow import Flow
        sent = {}

        def fake_fetch(flow, **kw):
            sent["verifier"] = flow.code_verifier
            raise RuntimeError("stop here")  # (no real Google in tests)

        self.client.force_login(self.admin)
        with override_settings(GOOGLE_WEB_CLIENT_ID="id", GOOGLE_WEB_CLIENT_SECRET="secret"), \
                mock.patch.object(Flow, "fetch_token", fake_fetch):
            r = self.client.get("/properties/pgthree/drive/")
            query = parse_qs(urlparse(r["Location"]).query)
            self.assertEqual(query["code_challenge_method"], ["S256"])
            made = self.client.session["drive-connect-verifier"]["verifier"]
            self.client.get("/properties/drive/callback/", {"state": query["state"][0], "code": "abc"})
        self.assertEqual(sent["verifier"], made)
        self.assertTrue(made)

    def test_logo_business_details_and_footer_on_paper_and_screen(self):
        import io
        from unittest import mock
        from PIL import Image
        from cluster import branding
        from core.invoice import _PDF
        from . import receipt
        buf = io.BytesIO()
        Image.new("RGB", (40, 40), "red").save(buf, "JPEG")
        jpeg = buf.getvalue()
        t = self.tenancy()
        pay = money.record(t, Decimal("9000"), mode="upi")
        printed, images = [], []
        real_put, real_image = _PDF.put, _PDF.image

        def put(pdf, x, y, w, h, txt, *a, **kw):
            printed.append(txt)
            return real_put(pdf, x, y, w, h, txt, *a, **kw)

        def image(pdf, src, *a, **kw):
            images.append(src)
            return real_image(pdf, src, *a, **kw)

        with mock.patch.object(_PDF, "put", put), mock.patch.object(_PDF, "image", image):
            receipt.pdf(pay)  # nothing filled in: no logo (not BSR Corridors'), no extra lines
            self.assertEqual(images, [])
            self.assertFalse(any("GSTIN" in x for x in printed))
            self.item.legal_name, self.item.gstin, self.item.pan = "Royal Stays LLP", "36abcde1234f1z5", ""
            self.item.invoice_footer, self.item.logo_id = "Thank you for choosing us!", "drive-file-1"
            self.item.save()
            printed.clear()
            with current.using(self.item), mock.patch.object(branding, "photo", lambda item, which="logo": jpeg):
                receipt.pdf(pay)
        self.assertEqual(len(images), 1)  # its own logo
        self.assertIn("Royal Stays LLP", printed)
        self.assertIn("GSTIN: 36ABCDE1234F1Z5", printed)  # PAN empty: left out
        self.assertIn("Thank you for choosing us!", printed)
        # On screen: its logo in the header.
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/p/pgthree/pg/"), 'src="/properties/pgthree/photo/logo/"')
        self.assertEqual(branding.business_lines(None), [])


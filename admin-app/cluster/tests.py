"""Cluster: items keep their data apart, pages and links stay inside the item, roles are enforced, BSR Corridors
keeps working exactly as before. Postgres only (items live in their own schemas)."""

from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase

from core import services
from core.models import Guest, Room, RoomType, Stay

from . import current, provision
from .models import Item, Membership

R = Membership.Role

# The test runner sets up every connection a test class names before it starts, so the PG's connection is registered
# when this file loads. It points at the same test database (Django makes it a mirror of "default").
if connection.vendor == "postgresql":
    current.register(Item(code="pgone", schema=provision.schema_name("pgone")))
    current.register(Item(code="newpg", schema=provision.schema_name("newpg")))  # made through the Add page


@skipUnless(connection.vendor == "postgresql", "cluster items need Postgres schemas")
class ClusterTests(TransactionTestCase):
    serialized_rollback = True  # keeps BSR Corridors' cluster item (made by a migration) between tests
    databases = {"default", "item_pgone", "item_newpg"} if connection.vendor == "postgresql" else {"default"}

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        for code in ("pgone", "newpg"):
            provision.drop(Item(code=code, schema=provision.schema_name(code)))

    def setUp(self):
        self.bsr = Item.objects.get(is_default=True)
        # A BSR Corridors booking, made before the new item exists.
        rt = RoomType.objects.create(name="Deluxe AC")
        self.bsr_room = Room.objects.create(number="101", room_type=rt)
        self.bsr_guest = Guest.objects.create(name="Hotel Guest", phone="+91 90000 00001")
        t = services.today()
        self.bsr_stay = Stay.objects.create(guest=self.bsr_guest, room=self.bsr_room, check_in=t,
                                            check_out=t + timedelta(days=3), total_amount=Decimal("4500"))
        # A second item using the hotel pages (PG pages have their own tests in pg/tests.py).
        self.pg = Item.objects.create(code="pgone", name="BSR Ladies PG", kind=Item.Kind.HOTEL, theme="teal")
        provision.provision(self.pg)
        with current.using(self.pg):
            prt = RoomType.objects.create(name="2-sharing")
            self.pg_room = Room.objects.create(number="201", room_type=prt)
            self.pg_guest = Guest.objects.create(name="PG Resident", phone="+91 90000 00002")
        User = get_user_model()
        self.admin = User.objects.create_superuser("boss", password="boss-password-1")

    def login(self, role=None, item=None, **switches):
        user = get_user_model().objects.create_user(f"u-{role}-{getattr(item, 'code', 'none')}", password="x" * 12)
        if role:
            Membership.objects.create(user=user, item=item, role=role, **switches)
        self.client.force_login(user)
        return user

    def test_new_item_doesnt_touch_bsr_corridors(self):
        # Creating the item ran the hotel migrations (some rewrite data) — on the item's tables only.
        self.bsr_stay.refresh_from_db()
        self.assertEqual(self.bsr_stay.total_amount, Decimal("4500"))
        self.assertEqual(list(Room.objects.values_list("number", flat=True)), ["101"])
        with current.using(self.pg):
            self.assertEqual(list(Room.objects.values_list("number", flat=True)), ["201"])
            self.assertFalse(Stay.objects.exists())

    def test_pages_show_only_their_own_items_data(self):
        self.client.force_login(self.admin)
        bsr = self.client.get("/guests/?q=Guest")
        self.assertContains(bsr, "Hotel Guest")
        self.assertNotContains(bsr, "PG Resident")
        pg = self.client.get("/p/pgone/guests/?q=Resident")
        self.assertContains(pg, "PG Resident")
        self.assertNotContains(pg, "Hotel Guest")
        rooms = self.client.get("/p/pgone/rooms/")
        self.assertContains(rooms, "201")
        self.assertNotContains(rooms, ">101<")

    def test_links_stay_inside_the_item_and_theme_shows(self):
        self.client.force_login(self.admin)
        r = self.client.get("/p/pgone/")
        self.assertContains(r, 'href="/p/pgone/stays/"')
        self.assertContains(r, "--gold: #0f8a83")  # teal
        self.assertContains(r, "BSR Ladies PG")
        # BSR Corridors: same addresses and colours as before.
        r = self.client.get("/")
        self.assertContains(r, 'href="/stays/"')
        self.assertContains(r, "--gold: #b88a44")
        # A cluster link made on an item page goes to the cluster page.
        self.assertRedirects(self.client.get("/p/pgone/properties/"), "/properties/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/p/nothere/").status_code, 404)

    def test_access_only_to_given_items(self):
        self.login(R.DESK, self.pg)
        self.assertRedirects(self.client.get("/properties/"), "/p/pgone/", fetch_redirect_response=False)  # S1
        self.assertEqual(self.client.get("/p/pgone/stays/").status_code, 200)
        self.assertRedirects(self.client.get("/"), "/properties/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/stays/").status_code, 403)  # BSR Corridors: no access
        self.assertEqual(self.client.get(f"/stays/{self.bsr_stay.pk}/").status_code, 403)

    def test_roles(self):
        self.login(R.DESK, self.pg)
        self.assertEqual(self.client.get("/p/pgone/revenue/").status_code, 403)
        self.assertEqual(self.client.get("/p/pgone/expenses/").status_code, 403)
        self.assertEqual(self.client.post("/p/pgone/settings/", {"action": "hosts", "hosts": "A"}).status_code, 403)
        page = self.client.get("/p/pgone/")
        self.assertNotContains(page, "/p/pgone/revenue/")  # not in the menu either
        self.client.logout()
        self.login(R.VIEWER, self.pg)
        self.assertEqual(self.client.get("/p/pgone/stays/").status_code, 200)
        self.assertEqual(self.client.post("/p/pgone/stays/new/", {}).status_code, 403)
        self.client.logout()
        self.login(R.MANAGER, self.pg, money_access=True)
        self.assertNotEqual(self.client.get("/p/pgone/revenue/").status_code, 403)
        self.assertNotEqual(self.client.get("/p/pgone/expenses/").status_code, 403)  # asks for the money password

    def test_home_lists_only_my_items(self):
        user = self.login(R.DESK, self.pg)
        Membership.objects.create(user=user, item=self.bsr, role=R.VIEWER)
        r = self.client.get("/properties/")
        self.assertContains(r, "BSR Ladies PG")
        self.assertContains(r, self.bsr.name)
        self.assertNotContains(r, "₹")  # no money on the cluster home page (D6)
        self.client.force_login(self.admin)
        Item.objects.create(code="other", name="Someone Else's PG", kind=Item.Kind.PG)  # no tables yet
        r = self.client.get("/properties/")
        self.assertContains(r, "Someone Else")
        self.assertContains(r, "Can’t read this property’s data")  # refused, never BSR Corridors' tables
        self.client.force_login(user)
        self.assertNotContains(self.client.get("/properties/"), "Someone Else")

    def test_theme_admin_only(self):
        self.login(R.OWNER, self.pg)
        self.client.post("/properties/pgone/theme/", {"theme": "rose"})
        self.pg.refresh_from_db()
        self.assertEqual(self.pg.theme, "teal")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/properties/pgone/theme/"), "data-preview")
        self.client.post("/properties/pgone/theme/", {"theme": "rose"})
        self.pg.refresh_from_db()
        self.assertEqual(self.pg.theme, "rose")

    def test_money_password_is_per_item(self):
        self.client.force_login(self.admin)
        self.client.post("/revenue/", {"password": "hotel-money", "confirm": "hotel-money"})  # set + unlock
        self.assertNotContains(self.client.get("/revenue/"), "Protected by a separate password")
        # The PG has its own money password: not set yet, and BSR Corridors' unlock doesn't open it.
        r = self.client.get("/p/pgone/revenue/")
        self.assertContains(r, "Protected by a separate password")
        self.assertContains(r, 'name="confirm"')

    # ── Phase 2 ──

    @staticmethod
    def partner_rows(*rows, initial=0):
        """The partners part of the property form: rows of (name, phone, share)."""
        data = {"partners-TOTAL_FORMS": str(len(rows)), "partners-INITIAL_FORMS": str(initial),
                "partners-MIN_NUM_FORMS": "0", "partners-MAX_NUM_FORMS": "1000"}
        for i, row in enumerate(rows):
            pk, name, phone, share = row if len(row) == 4 else (None, *row)
            data.update({f"partners-{i}-name": name, f"partners-{i}-phone": phone, f"partners-{i}-share": share,
                         f"partners-{i}-pay_to": ""})
            if pk:
                data[f"partners-{i}-id"] = pk
        return data

    def add_item(self, partners=(("Meena", "", "60"), ("Ravi", "+91 90000 33333", "40")), **extra):
        data = {"name": "New Ladies PG", "code": "newpg", "kind": "pg", "email": "pg@example.com",
                "managed_by": self.manager.pk, "theme": "plum", "sort_order": 0, "refund_days": 7,
                "money_password": "pg-money-1", "money_confirm": "pg-money-1", **self.partner_rows(*partners), **extra}
        return self.client.post("/properties/new/", data)

    def test_add_cluster_item_creates_its_tables(self):
        self.manager = get_user_model().objects.create_user("meena", password="x" * 12)
        self.client.force_login(self.admin)
        r = self.add_item(email="")  # email required
        self.assertContains(r, "This field is required")
        r = self.add_item(partners=(("Meena", "", "60"), ("Ravi", "", "30")))
        self.assertContains(r, "The shares add up to 90%")
        r = self.add_item(partners=(("Meena", "", "60"), ("Ravi", "", "")))
        self.assertContains(r, "Give every partner a share")
        self.assertFalse(Item.objects.filter(code="newpg").exists())
        r = self.add_item()
        self.assertRedirects(r, "/properties/newpg/", fetch_redirect_response=False)
        item = Item.objects.get(code="newpg")
        self.assertEqual((item.kind, item.schema, item.theme), ("pg", "item_newpg", "plum"))
        with current.using(item):
            self.assertEqual(Room.objects.count(), 0)  # its own, empty tables
            from core.models import AppSetting, MessageTemplate
            self.assertTrue(MessageTemplate.objects.exists())  # default WhatsApp messages
            self.assertTrue(AppSetting.get("revenue_password_hash"))  # money password set
        self.assertEqual(Membership.objects.get(user=self.manager, item=item).role, R.MANAGER)
        self.assertEqual([(p.name, p.phone, p.share) for p in item.partners.all()],
                         [("Meena", "", Decimal("60")), ("Ravi", "+91 90000 33333", Decimal("40"))])
        with current.using(item):
            from core.services import partner_shares, share_text
            shares = partner_shares()
            self.assertEqual([s.of(1000) for s in shares], [Decimal("600"), Decimal("400")])
            self.assertEqual(share_text(shares), "Shared – Meena 60%, Ravi 40%")
        meena, ravi = item.partners.all()
        # The type and code can't change afterwards; the money password can be reset. Partners: Ravi leaves.
        self.client.post("/properties/newpg/", {"name": "Renamed PG", "code": "other", "kind": "hotel",
                                                   "email": "pg@example.com", "managed_by": self.manager.pk,
                                                   "theme": "plum", "sort_order": 1, "refund_days": 10,
                                                   "upi_id": "newpg@ybl", "money_password": "new-money-2",
                                                   "money_confirm": "new-money-2",
                                                   **self.partner_rows((meena.pk, "Meena", "", ""),
                                                                       (ravi.pk, "Ravi", "", ""), initial=2),
                                                   "partners-1-DELETE": "on"})
        self.assertEqual([(p.name, p.share) for p in item.partners.all()], [("Meena", None)])
        from .models import Activity
        self.assertTrue(Activity.objects.filter(summary__contains="partners Meena 60%, Ravi 40% → Meena").exists())
        item.refresh_from_db()
        self.assertEqual((item.name, item.code, item.kind, item.upi_id), ("Renamed PG", "newpg", "pg", "newpg@ybl"))
        from django.contrib.auth.hashers import check_password
        with current.using(item):
            from core.models import AppSetting
            self.assertTrue(check_password("new-money-2", AppSetting.get("revenue_password_hash")))
            self.assertEqual(current.upi(), ("newpg@ybl", "Renamed PG"))  # its own UPI
        self.assertEqual(current.upi()[0], "")  # BSR Corridors: still from Render (none in tests)

    def test_only_admin_adds_items(self):
        self.login(R.OWNER, self.pg)
        self.assertRedirects(self.client.get("/properties/new/"), "/properties/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/properties/pgone/"), "/properties/", fetch_redirect_response=False)

    def test_archived_item_is_hidden_and_read_only(self):
        user = self.login(R.OWNER, self.pg)
        self.client.force_login(self.admin)
        self.client.post("/properties/pgone/", {"action": "archive"})
        self.pg.refresh_from_db()
        self.assertTrue(self.pg.archived)
        self.assertEqual(self.client.get("/p/pgone/rooms/").status_code, 200)  # the admin can still look…
        self.assertEqual(self.client.post("/p/pgone/settings/", {"action": "hosts", "hosts": "A"}).status_code, 403)
        self.client.force_login(user)
        self.assertEqual(self.client.get("/p/pgone/").status_code, 404)  # …others don't see it at all
        self.client.force_login(self.admin)
        self.assertContains(self.client.post("/properties/bsrc/", {"action": "archive"}, follow=True),
                            "can’t be archived")

    def test_item_short_links_open_the_items_page(self):
        from core.shortlinks import shorten
        with current.using(self.pg):
            short = shorten("https://app.example/p/pgone/pay/abc123/")
        self.assertRegex(short, r"/s/pgone-[A-Za-z0-9]{7}$")
        code = short.rsplit("/", 1)[1]
        self.assertRedirects(self.client.get(f"/s/{code}"), "/p/pgone/pay/abc123/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/s/nothere-Abc1234").status_code, 404)

    def test_nightly_job_backs_up_each_item_on_its_own(self):
        from unittest import mock
        from django.test import override_settings
        from core import gdrive
        from core.models import BackupRun
        seen = []

        def fake_backup():
            seen.append(current.alias())
            return BackupRun.objects.create(ok=True, message=f"saved {current.alias()}")

        with override_settings(BACKUP_TOKEN="t0ken"), mock.patch.object(gdrive, "run_backup", fake_backup), \
                mock.patch.object(gdrive, "is_configured", lambda: current.is_default()), \
                mock.patch.object(gdrive, "save_last_months_report_if_missing"):
            r = self.client.post("/backup/run/", headers={"X-Backup-Token": "t0ken"})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(seen, ["default"])  # the PG has no Drive connected: skipped, not failed
        self.assertIn("not connected", d["items"]["pgone"])
        # The PG's own run history is untouched by BSR Corridors' backup.
        with current.using(self.pg):
            self.assertFalse(BackupRun.objects.exists())

    def test_database_space_shown_to_the_admin(self):
        self.client.force_login(self.admin)
        r = self.client.get("/properties/")
        self.assertContains(r, "Database space")
        self.assertContains(r, "of 500 MB")
        self.login(R.OWNER, self.pg)
        self.assertNotContains(self.client.get("/properties/?all"), "Database space")

    def test_move_refused_leaves_item_unchanged(self):
        from .move import MoveFailed, move
        with self.assertRaises(MoveFailed):
            move(self.pg, "mysql://nope")
        with self.assertRaises(MoveFailed):
            move(self.bsr, "postgresql://x")
        self.pg.refresh_from_db()
        self.assertEqual((self.pg.schema, self.pg.db_url, self.pg.archived), ("item_pgone", "", False))



@skipUnless(connection.vendor == "postgresql", "the archive is a Postgres database")
class ArchiveTests(TransactionTestCase):
    """Old, finished records move to the archive database (here: a table in the test database); standard records
    stay; people can still be found."""
    serialized_rollback = True

    def setUp(self):
        from unittest import mock
        from . import archive
        from .models import ArchiveStore
        ArchiveStore.objects.create(db_url="postgresql://test/test")
        patcher = mock.patch.object(archive, "_alias", lambda: "default")
        patcher.start()
        self.addCleanup(patcher.stop)
        with connection.cursor() as cur:
            cur.execute(archive.TABLE_SQL)
        self.addCleanup(self._drop)
        self.bsr = Item.objects.get(is_default=True)
        self.admin = get_user_model().objects.create_superuser("boss", password="boss-password-1")
        rt = RoomType.objects.create(name="Deluxe AC")
        self.room = Room.objects.create(number="101", room_type=rt)

    @staticmethod
    def _drop():
        with connection.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS bsr_archive")

    def old_stay(self, guest, years_ago, paid=True, **kw):
        from core.models import Payment
        self.n = getattr(self, "n", 0) + 1  # one room: each stay on its own dates
        start = services.today() - timedelta(days=365 * years_ago + 5 * self.n)
        s = Stay.objects.create(guest=guest, room=self.room, check_in=start, check_out=start + timedelta(days=2),
                                total_amount=Decimal("3000"), amount_paid=Decimal("3000") if paid else 0,
                                status=Stay.Status.CHECKED_OUT, **kw)
        if paid:
            Payment.objects.create(stay=s, amount=Decimal("3000"), date=start)
        return s

    def test_move_old_hotel_records(self):
        from core.models import Expense, Guest, Payment
        from . import archive
        gone = Guest.objects.create(name="Old Guest", phone="+91 90000 77777", id_number="1234 5678 9012")
        owes = Guest.objects.create(name="Still Owes", phone="+91 90000 88888")
        back = Guest.objects.create(name="Came Back", phone="+91 90000 99999")
        self.old_stay(gone, 3)
        self.old_stay(owes, 3, paid=False)  # money still to collect: stays
        self.old_stay(back, 3)
        now = Stay.objects.create(guest=back, room=self.room, check_in=services.today(),
                                  check_out=services.today() + timedelta(days=1), total_amount=Decimal("1500"))
        Expense.objects.create(date=services.today() - timedelta(days=800), category="other", amount=Decimal("500"),
                               reason="Old paint")
        before = services.today() - timedelta(days=365)
        p = archive.plan(self.bsr, before)
        self.assertEqual(dict(p.counts)["Stays"], 2)
        self.assertEqual(dict(p.counts)["Guests"], 1)
        archive.move(self.bsr, before)
        self.assertFalse(Guest.objects.filter(pk=gone.pk).exists())  # moved
        self.assertEqual(Stay.objects.filter(guest=back).get(), now)  # newer stay stays, guest kept
        self.assertTrue(Stay.objects.filter(guest=owes).exists())  # unpaid: kept
        self.assertEqual(Payment.objects.count(), 0)
        self.assertFalse(Expense.objects.exists())
        self.assertTrue(Room.objects.filter(pk=self.room.pk).exists())  # standard records never move
        with connection.cursor() as cur:
            cur.execute("SELECT model, count(*) FROM bsr_archive GROUP BY model")
            got = dict(cur.fetchall())
            cur.execute("SELECT data::text FROM bsr_archive WHERE model = 'core.guest'")
            raw = " ".join(r[0] for r in cur.fetchall())
        self.assertEqual((got["core.stay"], got["core.payment"], got["core.guest"]), (2, 2, 2))  # + Came Back copied
        self.assertNotIn("core.room", got)
        self.assertNotIn("1234 5678 9012", raw)  # ID numbers stay encrypted
        # Found again, with the stay.
        self.client.force_login(self.admin)
        found = self.client.get("/properties/search/?q=77777")
        self.assertContains(found, "In the archive")
        rid = archive.find_people("", "9000077777")[0]["id"]
        page = self.client.get(f"/properties/archive/{rid}/")
        self.assertContains(page, "Old Guest")
        self.assertContains(page, "paid ₹3000")
        self.assertContains(page, ">101<")  # the room number, from the main database
        self.assertEqual(archive.plan(self.bsr, before).total, 0)  # moving again: nothing left

    def test_archive_page_is_admin_only_and_moves(self):
        from core.models import Guest
        from .models import Activity
        g = Guest.objects.create(name="Old Guest", phone="+91 90000 77777")
        self.old_stay(g, 3)
        self.client.force_login(self.admin)
        page = self.client.get("/properties/archive/")
        self.assertContains(page, "Move old records")
        before = (services.today() - timedelta(days=365)).isoformat()
        prev = self.client.post("/properties/archive/", {"action": "preview", "item": self.bsr.code, "before": before})
        self.assertContains(prev, "Move 3 records to the archive")  # stay, payment, guest
        self.client.post("/properties/archive/", {"action": "move", "item": self.bsr.code, "before": before})
        self.assertFalse(Guest.objects.filter(pk=g.pk).exists())
        self.assertTrue(Activity.objects.filter(action="archive", summary__contains="1 stays").exists())
        owner = get_user_model().objects.create_user("owner", password="x" * 12)
        Membership.objects.create(user=owner, item=self.bsr, role=R.OWNER, money_access=True)
        self.client.force_login(owner)
        self.assertRedirects(self.client.get("/properties/archive/"), "/properties/", fetch_redirect_response=False)
        home = self.client.get("/properties/?all")
        self.assertNotContains(home, "Database space")  # space is the main admin's concern only

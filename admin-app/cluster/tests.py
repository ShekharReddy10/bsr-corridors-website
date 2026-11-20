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


@skipUnless(connection.vendor == "postgresql", "cluster items need Postgres schemas")
class ClusterTests(TransactionTestCase):
    serialized_rollback = True  # keeps BSR Corridors' cluster item (made by a migration) between tests
    databases = {"default", "item_pgone"} if connection.vendor == "postgresql" else {"default"}

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        provision.drop(Item(code="pgone", schema=provision.schema_name("pgone")))

    def setUp(self):
        self.bsr = Item.objects.get(is_default=True)
        # A BSR Corridors booking, made before the new item exists.
        rt = RoomType.objects.create(name="Deluxe AC")
        self.bsr_room = Room.objects.create(number="101", room_type=rt)
        self.bsr_guest = Guest.objects.create(name="Hotel Guest", phone="+91 90000 00001")
        t = services.today()
        self.bsr_stay = Stay.objects.create(guest=self.bsr_guest, room=self.bsr_room, check_in=t,
                                            check_out=t + timedelta(days=3), total_amount=Decimal("4500"))
        self.pg = Item.objects.create(code="pgone", name="BSR Ladies PG", kind=Item.Kind.PG, theme="teal")
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
        self.assertRedirects(self.client.get("/p/pgone/cluster/"), "/cluster/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/p/nothere/").status_code, 404)

    def test_access_only_to_given_items(self):
        self.login(R.DESK, self.pg)
        self.assertRedirects(self.client.get("/cluster/"), "/p/pgone/", fetch_redirect_response=False)  # S1
        self.assertEqual(self.client.get("/p/pgone/stays/").status_code, 200)
        self.assertRedirects(self.client.get("/"), "/cluster/", fetch_redirect_response=False)
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
        r = self.client.get("/cluster/")
        self.assertContains(r, "BSR Ladies PG")
        self.assertContains(r, self.bsr.name)
        self.assertNotContains(r, "₹")  # no money on the cluster home page (D6)
        self.client.force_login(self.admin)
        Item.objects.create(code="other", name="Someone Else's PG", kind=Item.Kind.PG)  # no tables yet
        r = self.client.get("/cluster/")
        self.assertContains(r, "Someone Else")
        self.assertContains(r, "Can’t read this item’s data")  # refused, never BSR Corridors' tables
        self.client.force_login(user)
        self.assertNotContains(self.client.get("/cluster/"), "Someone Else")

    def test_theme_admin_only(self):
        self.login(R.OWNER, self.pg)
        self.client.post("/cluster/items/pgone/theme/", {"theme": "rose"})
        self.pg.refresh_from_db()
        self.assertEqual(self.pg.theme, "teal")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/cluster/items/pgone/theme/"), "data-preview")
        self.client.post("/cluster/items/pgone/theme/", {"theme": "rose"})
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

import json
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings

from core import services
from core.exports import backup_json, build_workbook
from core.models import AuditLog, Guest, Room, RoomType, Stay

T = services.today()


def make_rooms():
    ac = RoomType.objects.create(name="Luxury AC", is_ac=True, max_guests=2)
    non = RoomType.objects.create(name="Luxury Non-AC", is_ac=False, max_guests=2)
    rooms = {
        "101": Room.objects.create(number="101", room_type=ac, sort_order=1),
        "102": Room.objects.create(number="102", room_type=ac, sort_order=2),
        "103": Room.objects.create(number="103", room_type=ac, sort_order=3),
        "201": Room.objects.create(number="201", room_type=non, sort_order=4),
    }
    return ac, non, rooms


def make_guest(name="Ravi Kumar", phone="+91 98765 43210", aadhaar="123412341234"):
    return Guest.objects.create(name=name, phone=phone, address="Gachibowli, Hyderabad", id_number=aadhaar)


def stay(guest, room, start_offset, nights, **kw):
    s = Stay(guest=guest, room=room, check_in=T + timedelta(days=start_offset),
             check_out=T + timedelta(days=start_offset + nights), total_amount=Decimal(1500 * nights), **kw)
    return services.save_stay(s, action="create", summary="test")


class EncryptionTests(TestCase):
    def test_id_number_encrypted_at_rest_and_in_backup(self):
        g = make_guest()
        with connection.cursor() as cur:
            cur.execute(f"SELECT id_number FROM {Guest._meta.db_table} WHERE id = %s", [g.pk])
            raw = cur.fetchone()[0]
        self.assertTrue(raw.startswith("enc:"))
        self.assertNotIn("123412341234", raw)
        self.assertEqual(Guest.objects.get(pk=g.pk).id_number, "123412341234")
        self.assertEqual(g.id_masked, "•••• •••• 1234")
        dump = backup_json().decode()
        self.assertNotIn("123412341234", dump)
        self.assertIn("enc:", dump)

    def test_backup_round_trip(self):
        _, _, rooms = make_rooms()
        g = make_guest()
        stay(g, rooms["101"], 0, 2)
        data = backup_json()
        Stay.objects.all().delete()
        Guest.objects.all().delete()
        from django.core import serializers
        for obj in serializers.deserialize("json", data):
            obj.save()
        self.assertEqual(Guest.objects.get().id_number, "123412341234")
        self.assertEqual(Stay.objects.count(), 1)


class AvailabilityTests(TestCase):
    def setUp(self):
        _, _, self.rooms = make_rooms()
        self.g = make_guest()

    def test_overlap_rejected_back_to_back_allowed(self):
        stay(self.g, self.rooms["101"], 0, 3)
        with self.assertRaises(services.StayConflict):
            stay(make_guest("Anita", "+91 90000 00001"), self.rooms["101"], 2, 2)
        stay(make_guest("Anita", "+91 90000 00011"), self.rooms["101"], 3, 2)  # arrives on checkout day
        self.assertEqual(Stay.objects.count(), 2)

    def test_cancelled_stay_frees_room(self):
        s = stay(self.g, self.rooms["101"], 0, 3)
        services.cancel(s)
        stay(make_guest("Anita", "+91 90000 00001"), self.rooms["101"], 1, 1)

    @skipUnless(connection.vendor == "postgresql", "exclusion constraint is Postgres-only")
    def test_database_blocks_overlap_even_without_service(self):
        stay(self.g, self.rooms["101"], 0, 3)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Stay.objects.create(guest=self.g, room=self.rooms["101"], check_in=T + timedelta(days=1),
                                check_out=T + timedelta(days=2))

    def test_free_rooms(self):
        stay(self.g, self.rooms["101"], 0, 3)
        free = set(services.free_rooms(T, T + timedelta(days=1)).values_list("number", flat=True))
        self.assertEqual(free, {"102", "103", "201"})


class GuestPhoneTests(TestCase):
    def test_one_guest_per_number(self):
        make_guest(phone="+91 98765 43210")
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_guest("Other", phone="098765 43210")  # same last 10 digits
        self.assertEqual(Guest.objects.get().phone_key, "9876543210")


class ExtensionTests(TestCase):
    def setUp(self):
        self.ac, _, self.rooms = make_rooms()
        self.g = make_guest()

    def test_extend_in_place_when_free(self):
        s = stay(self.g, self.rooms["101"], 0, 2)
        plan = services.plan_extension(s, s.check_out + timedelta(days=2))
        self.assertTrue(plan.free)
        services.extend_in_place(s, plan.new_check_out, Decimal("2500"), Decimal("1000"), "upi")
        s.refresh_from_db()
        self.assertEqual(s.nights, 4)
        self.assertEqual((s.total, s.amount_paid, s.balance), (Decimal("5500"), Decimal("1000"), Decimal("4500")))
        self.assertTrue(AuditLog.objects.filter(stay=s, action="extend").exists())

    def test_blocked_offers_same_type_rooms_and_split(self):
        s = stay(self.g, self.rooms["101"], 0, 2)
        stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 2, 3)  # room taken after
        stay(make_guest("Busy", "+91 90000 00003"), self.rooms["102"], 2, 1)  # 102 busy on first extra night
        plan = services.plan_extension(s, s.check_out + timedelta(days=2))
        self.assertFalse(plan.free)
        self.assertEqual([r.number for r in plan.split_rooms], ["103"])  # same type only, and free
        self.assertEqual(plan.move_rooms[0].room_type, self.ac)  # same type first
        cont = services.extend_split(s, plan.new_check_out, self.rooms["103"], Decimal("2000"))
        self.assertEqual(cont.check_in, s.check_out)
        self.assertEqual(cont.total, Decimal("2000"))
        self.assertEqual(cont.linked_to, s)
        self.assertEqual(len(s.chain()), 2)

    def test_move_mid_stay_splits_at_today(self):
        s = stay(self.g, self.rooms["101"], -2, 3)  # arrived 2 days ago, leaves tomorrow
        services.check_in(s)
        stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 1, 3)
        plan = services.plan_extension(s, T + timedelta(days=4))
        self.assertEqual(plan.move_start, T)
        cont = services.extend_move(s, plan.new_check_out, self.rooms["102"], plan.move_start)
        s.refresh_from_db()
        self.assertEqual(s.check_out, T)
        self.assertEqual(s.status, Stay.Status.CHECKED_OUT)
        self.assertEqual((cont.room.number, cont.check_in, cont.status), ("102", T, Stay.Status.CHECKED_IN))

    def test_early_checkout_shortens(self):
        s = stay(self.g, self.rooms["101"], -1, 4)
        services.check_in(s)
        services.check_out(s)
        s.refresh_from_db()
        self.assertEqual((s.check_out, s.status), (T, Stay.Status.CHECKED_OUT))

    def test_payment(self):
        s = stay(self.g, self.rooms["101"], 0, 2)
        services.add_payment(s, Decimal("1000"), "upi")
        s.refresh_from_db()
        self.assertEqual((s.amount_paid, s.balance, s.payment_mode), (Decimal("1000"), Decimal("2000"), "upi"))


class ExportTests(TestCase):
    def test_workbook_masks_ids_by_default(self):
        _, _, rooms = make_rooms()
        stay(make_guest(), rooms["101"], 0, 1)
        import io

        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(build_workbook(T, T + timedelta(days=5))))
        values = [c.value for row in wb["Stays"].iter_rows() for c in row]
        self.assertIn("••••1234", values)
        self.assertNotIn("123412341234", values)
        self.assertEqual(wb.sheetnames, ["Stays", "Guests", "Rooms"])


class ViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user("test-admin", password="test-pass-123456")
        self.client.force_login(self.user)
        self.ac, _, self.rooms = make_rooms()

    def _new_stay_post(self, **over):
        data = {
            "g-phone": "+91 98765 43210", "g-name": "Ravi Kumar", "g-address": "Hyderabad", "g-nationality": "Indian",
            "g-id_type": "aadhaar", "g-id_number": "1234 1234 1234",
            "s-room": self.rooms["101"].pk, "s-check_in": T.isoformat(), "s-check_out": (T + timedelta(days=2)).isoformat(),
            "s-num_guests": 2, "s-total_amount": "3000", "s-amount_paid": "1000", "s-payment_mode": "upi",
            "s-source": "airbnb", "s-source_ref": "HMABC123",
        }
        data.update(over)
        return self.client.post("/stays/new/", data)

    def test_pages_render(self):
        s = stay(make_guest(), self.rooms["101"], 0, 2)
        for url in ["/", "/calendar/", "/calendar/?days=31", f"/calendar/room/{self.rooms['101'].pk}/", "/stays/",
                    "/stays/new/", f"/stays/{s.pk}/", f"/stays/{s.pk}/edit/", f"/stays/{s.pk}/extend/", "/rooms/",
                    "/rooms/type/new/", "/rooms/room/new/", f"/rooms/room/{self.rooms['101'].pk}/", "/export/",
                    "/backups/", "/settings/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.get("/stays/?q=ravi", HTTP_HX_REQUEST="true")
        self.assertContains(r, "Ravi Kumar")
        self.assertNotContains(r, "<html")

    def test_create_stay_and_returning_guest(self):
        r = self._new_stay_post()
        s = Stay.objects.get()
        self.assertRedirects(r, f"/stays/{s.pk}/")
        self.assertEqual(s.guest.id_number, "123412341234")
        self.assertEqual(s.balance, Decimal("2000"))
        self.assertEqual((s.source, s.source_ref), ("airbnb", "HMABC123"))

        # Same number without picking the saved guest → asked to use the saved guest
        r = self._new_stay_post(**{"g-name": "Someone Else", "s-room": self.rooms["103"].pk})
        self.assertContains(r, "already saved for Ravi Kumar")

        lookup = self.client.get("/guests/lookup/?phone=9876543210").json()
        self.assertTrue(lookup["found"])
        self.assertNotIn("123412341234", json.dumps(lookup))

        # Returning guest, ID left blank → stored ID kept, same guest record
        r = self._new_stay_post(**{"g-guest_id": lookup["guest_id"], "g-id_number": "", "s-room": self.rooms["102"].pk})
        self.assertEqual(Guest.objects.count(), 1)
        self.assertEqual(Guest.objects.get().id_number, "123412341234")

    def test_conflict_shows_error(self):
        self._new_stay_post()
        r = self._new_stay_post(**{"g-phone": "+91 90000 00009", "g-name": "Other"})
        self.assertContains(r, "already booked")
        self.assertEqual(Stay.objects.count(), 1)

    def test_validation(self):
        r = self._new_stay_post(**{"g-id_number": "1234"})
        self.assertContains(r, "Aadhaar number must be 12 digits")
        r = self._new_stay_post(**{"g-id_number": "", "g-phone": "+91 90000 00077"})  # ID number optional
        self.assertEqual(Stay.objects.get().guest.id_number, "")
        self.assertContains(self.client.get(f"/stays/{Stay.objects.get().pk}/"), "Not provided")
        Stay.objects.all().delete(); Guest.objects.all().delete()
        r = self._new_stay_post(**{"g-nationality": "British", "g-id_type": "passport", "g-id_number": "X1"})
        self.assertContains(r, "Required for foreign nationals")
        r = self._new_stay_post(**{"s-num_guests": 3})
        self.assertContains(r, "allows up to 2 guests")
        self.assertEqual(Stay.objects.count(), 0)

    def test_extend_flow(self):
        s = stay(make_guest(), self.rooms["101"], 0, 2)
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (s.check_out + timedelta(days=1)).isoformat(),
                                                         "amount": "1200", "paid_now": "1200", "payment_mode": "cash"})
        self.assertRedirects(r, f"/stays/{s.pk}/")
        s.refresh_from_db()
        self.assertEqual((s.total, s.amount_paid), (Decimal("4200"), Decimal("1200")))
        stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 3, 2)
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (T + timedelta(days=5)).isoformat(), "amount": "2000"})
        self.assertContains(r, "is booked")
        self.assertContains(r, 'name="amount" value="2000')
        self.assertContains(r, "Move to <b>Room 102</b>")
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (T + timedelta(days=5)).isoformat(), "amount": "2000",
                                                         "choice": "split", "room": self.rooms["102"].pk})
        cont = Stay.objects.get(linked_to=s)
        self.assertEqual((cont.total, cont.source), (Decimal("2000"), s.source))

    def test_actions_and_reveal(self):
        s = stay(make_guest(), self.rooms["101"], 0, 2)
        self.client.post(f"/stays/{s.pk}/check-in/")
        self.client.post(f"/stays/{s.pk}/payment/", {"amount": "500", "mode": "cash"})
        s.refresh_from_db()
        self.assertEqual((s.status, s.amount_paid), (Stay.Status.CHECKED_IN, Decimal("500")))
        r = self.client.post(f"/stays/{s.pk}/reveal-id/", {"field": "id_number"})
        self.assertContains(r, "123412341234")
        self.assertTrue(AuditLog.objects.filter(action="reveal_id").exists())

    def test_guest_search_and_rebook(self):
        g = make_guest()
        stay(g, self.rooms["101"], -10, 2)
        r = self.client.get("/guests/?q=98765", HTTP_HX_REQUEST="true")
        self.assertContains(r, "Ravi Kumar")
        self.assertContains(r, f"/stays/new/?guest={g.pk}")
        self.assertContains(self.client.get(f"/guests/{g.pk}/"), "New booking for Ravi Kumar")
        r = self.client.get(f"/stays/new/?guest={g.pk}")
        self.assertContains(r, "Booking for <b>Ravi Kumar</b>")
        self.assertContains(r, f'name="g-guest_id" value="{g.pk}"')
        self.assertEqual(self.client.get("/guests/").status_code, 200)

    def test_room_with_history_is_deactivated_not_deleted(self):
        stay(make_guest(), self.rooms["101"], 0, 1)
        self.client.post(f"/rooms/room/{self.rooms['101'].pk}/delete/")
        self.assertFalse(Room.objects.get(pk=self.rooms["101"].pk).is_active)
        self.client.post(f"/rooms/room/{self.rooms['103'].pk}/delete/")
        self.assertFalse(Room.objects.filter(pk=self.rooms["103"].pk).exists())

    def test_export_download(self):
        stay(make_guest(), self.rooms["101"], 0, 1)
        r = self.client.post("/export/", {"start": T.isoformat(), "end": T.isoformat(), "action": "download"})
        self.assertEqual(r["Content-Type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_purge(self):
        old = stay(make_guest(), self.rooms["101"], -2000, 2)
        old.status = Stay.Status.CHECKED_OUT
        old.save()
        self.client.post("/settings/", {"years": "3", "confirm": "PURGE"})
        self.assertFalse(Stay.objects.exists())
        self.assertFalse(Guest.objects.exists())


class SecurityTests(TestCase):
    def setUp(self):
        cache.clear()
        get_user_model().objects.create_user("test-admin", password="test-pass-123456")

    def test_login_required(self):
        self.assertRedirects(self.client.get("/stays/"), "/login/?next=/stays/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_login_and_lockout(self):
        r = self.client.post("/login/", {"username": "test-admin", "password": "test-pass-123456"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.client.logout()
        for _ in range(5):
            self.assertContains(self.client.post("/login/", {"username": "test-admin", "password": "wrong"}),
                                "Incorrect username or password")
        r = self.client.post("/login/", {"username": "test-admin", "password": "test-pass-123456"})
        self.assertContains(r, "Too many failed attempts")
        self.assertNotIn("_auth_user_id", self.client.session)

    @override_settings(BACKUP_TOKEN="secret-token")
    def test_backup_hook_requires_token(self):
        self.assertEqual(self.client.post("/backup/run/").status_code, 403)
        self.assertEqual(self.client.post("/backup/run/", HTTP_X_BACKUP_TOKEN="nope").status_code, 403)
        r = self.client.post("/backup/run/", HTTP_X_BACKUP_TOKEN="secret-token")  # Drive not configured → 500 + message
        self.assertEqual(r.status_code, 500)
        self.assertIn("not connected", r.json()["message"])

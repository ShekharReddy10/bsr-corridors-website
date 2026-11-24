import io
import json
from datetime import date, timedelta
from decimal import Decimal
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings

from core import services
from core.exports import backup_json, build_workbook
from core.models import AppSetting, AuditLog, Expense, Guest, MessageTemplate, Room, RoomType, Settlement, Stay, StayGuest, StayIdPhoto, IdPhoto

T = services.today()



def staff(username, password="test-pass-123456"):
    """A login with full access to BSR Corridors — what every existing login gets when the cluster arrives."""
    from cluster.models import Item, Membership
    user = get_user_model().objects.create_user(username, password=password)
    Membership.objects.create(user=user, item=Item.objects.get(is_default=True), role=Membership.Role.OWNER,
                              money_access=True, see_full_ids=True, can_delete=True)
    return user

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

    def test_transfer_on_arrival_frees_room(self):
        s = stay(self.g, self.rooms["201"], 0, 3)
        services.transfer_out(s, "  Hotel Sitara  ", "Wanted AC room")
        s.refresh_from_db()
        self.assertEqual(s.status, Stay.Status.TRANSFERRED)
        self.assertEqual((s.transferred_to, s.transfer_reason), ("Hotel Sitara", "Wanted AC room"))
        self.assertIn("201", services.free_rooms(T, T + timedelta(days=3)).values_list("number", flat=True))
        stay(make_guest("Anita", "+91 90000 00001"), self.rooms["201"], 0, 3)  # DB constraint lets it through too
        self.assertIn("Hotel Sitara", AuditLog.objects.filter(action="transfer").get().summary)

    def test_transfer_after_nights_stayed_keeps_them(self):
        s = stay(self.g, self.rooms["201"], -2, 5, status=Stay.Status.CHECKED_IN)
        services.transfer_out(s, "Hotel Sitara")
        s.refresh_from_db()
        self.assertEqual((s.status, s.check_out, s.transferred_to), (Stay.Status.CHECKED_OUT, T, "Hotel Sitara"))
        stay(make_guest("Anita", "+91 90000 00001"), self.rooms["201"], 0, 2)
        with self.assertRaises(services.StayConflict):
            stay(make_guest("Vijay", "+91 90000 00002"), self.rooms["201"], -1, 1)

    def test_undo_early_check_out_restores_booked_date(self):
        s = stay(self.g, self.rooms["201"], -1, 4, status=Stay.Status.CHECKED_IN)
        booked = s.check_out
        services.check_out(s)
        self.assertEqual((s.check_out, services.booked_check_out(s)), (T, booked))
        services.undo_check_out(s, services.booked_check_out(s))
        s.refresh_from_db()
        self.assertEqual((s.status, s.check_out, s.checked_out_at), (Stay.Status.CHECKED_IN, booked, None))

    def test_undo_check_out_blocked_if_room_rebooked(self):
        s = stay(self.g, self.rooms["201"], -1, 4, status=Stay.Status.CHECKED_IN)
        booked = s.check_out
        services.check_out(s)
        stay(make_guest("Anita", "+91 90000 00001"), self.rooms["201"], 1, 1)
        with self.assertRaises(services.StayConflict):
            services.undo_check_out(s, booked)

    def test_undo_monthly_check_out_reopens(self):
        s = Stay(guest=self.g, room=self.rooms["201"], kind=Stay.Kind.MONTHLY, check_in=T - timedelta(days=5),
                 status=Stay.Status.CHECKED_IN)
        services.save_stay(s, action="create", summary="test")
        services.check_out(s)
        self.assertIsNone(services.booked_check_out(s))
        services.undo_check_out(s, None)
        s.refresh_from_db()
        self.assertEqual((s.status, s.check_out), (Stay.Status.CHECKED_IN, None))

    def test_undo_check_in(self):
        s = stay(self.g, self.rooms["201"], 0, 2)
        services.check_in(s)
        services.undo_check_in(s)
        s.refresh_from_db()
        self.assertEqual((s.status, s.checked_in_at), (Stay.Status.UPCOMING, None))

    def test_transfer_needs_hotel_name(self):
        s = stay(self.g, self.rooms["201"], 0, 1)
        with self.assertRaises(ValueError):
            services.transfer_out(s, "  ")
        s.refresh_from_db()
        self.assertEqual(s.status, Stay.Status.UPCOMING)

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

    def test_type_has_space_moves_future_booking(self):
        """Room 101 is booked after; another AC room is free → extend in 101 and move the future booking."""
        s = stay(self.g, self.rooms["101"], 0, 2)
        nxt = stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 2, 3)
        stay(make_guest("Busy", "+91 90000 00003"), self.rooms["102"], 2, 1)  # 102 busy on first extra night
        plan = services.plan_extension(s, s.check_out + timedelta(days=2))
        self.assertFalse(plan.free)
        self.assertEqual(plan.full_nights, [])
        self.assertTrue(plan.can_extend_in_place)
        self.assertEqual([(b.pk, r.number) for b, r in plan.moves], [(nxt.pk, "103")])  # 103 free for Next's whole stay
        services.extend_with_moves(s, plan.new_check_out, plan.moves, Decimal("3000"))
        s.refresh_from_db(); nxt.refresh_from_db()
        self.assertEqual((s.room.number, s.nights, s.total), ("101", 4, Decimal("6000")))
        self.assertEqual(nxt.room.number, "103")
        self.assertTrue(AuditLog.objects.filter(stay=nxt, action="move").exists())

    def test_type_full_offers_options(self):
        """All AC rooms taken on an extra night → no automatic extension; split/move options offered."""
        s = stay(self.g, self.rooms["101"], 0, 2)
        stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 2, 3)
        stay(make_guest("B2", "+91 90000 00003"), self.rooms["102"], 2, 1)
        stay(make_guest("B3", "+91 90000 00004"), self.rooms["103"], 2, 1)
        plan = services.plan_extension(s, s.check_out + timedelta(days=2))
        self.assertEqual(plan.full_nights, [T + timedelta(days=2)])
        self.assertFalse(plan.can_extend_in_place)
        self.assertEqual(plan.split_rooms, [])  # no AC room free for the extra nights
        self.assertEqual([r.number for r in plan.move_rooms], ["201"])  # other type offered
        cont = services.extend_split(s, plan.new_check_out, self.rooms["201"], Decimal("2000"))
        self.assertEqual((cont.check_in, cont.total, cont.linked_to), (s.check_out, Decimal("2000"), s))
        self.assertEqual(len(s.chain()), 2)

    def test_in_house_guest_never_moved_automatically(self):
        s = stay(self.g, self.rooms["101"], 0, 2)
        other = stay(make_guest("In", "+91 90000 00005"), self.rooms["102"], 0, 5)
        services.check_in(other)
        # nothing blocks 101 itself → simple extension
        self.assertTrue(services.plan_extension(s, s.check_out + timedelta(days=1)).free)

    def test_move_mid_stay_splits_at_today(self):
        s = stay(self.g, self.rooms["101"], -2, 3)  # arrived 2 days ago, leaves tomorrow
        services.check_in(s)
        stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 1, 3)
        cont = services.extend_move(s, T + timedelta(days=4), self.rooms["102"], T)
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
        self.assertEqual(wb.sheetnames, ["Stays", "Guests", "Rooms", "Expenses", "Payments", "Monthly summary"])


class ViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = staff("test-admin", password="test-pass-123456")
        self.client.force_login(self.user)
        self.ac, _, self.rooms = make_rooms()

    def _new_stay_post(self, **over):
        return self.client.post("/stays/new/", {**self._post_data(edit=False), **over})

    def _post_data(self, edit=True):
        stay = Stay.objects.first() if edit else None
        data = {
            "g-phone": "+91 98765 43210", "g-name": "Ravi Kumar", "g-address": "Hyderabad", "g-nationality": "Indian",
            "g-id_type": "aadhaar", "g-id_number": "1234 1234 1234",
            "s-room": self.rooms["101"].pk, "s-check_in": T.isoformat(), "s-check_out": (T + timedelta(days=2)).isoformat(),
            "s-num_guests": 2, "s-total_amount": "3000", "s-amount_paid": "1000", "s-payment_mode": "upi",
            "s-source": "airbnb", "s-source_ref": "HMABC123", "s-host": services.host_names()[0],
            "o-TOTAL_FORMS": "2", "o-INITIAL_FORMS": "0", "o-MIN_NUM_FORMS": "0", "o-MAX_NUM_FORMS": "9",
            "o-0-id_type": "aadhaar", "o-1-id_type": "aadhaar",
        }
        if stay:  # editing: keep the same guest record
            data["g-guest_id"] = stay.guest_id
        return data

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

    def test_clash_popup_moves_existing_booking(self):
        # 101 booked for 2 nights; a new 4-night stay in 101 clashes. 102 is busy, 103 (same type) is free.
        old = stay(make_guest("Old Guest", "+91 90000 00001"), self.rooms["101"], 0, 2)
        stay(make_guest("Busy", "+91 90000 00002"), self.rooms["102"], 0, 4)
        new = {"g-phone": "+91 90000 00009", "g-name": "New Guest",
               "s-check_out": (T + timedelta(days=4)).isoformat()}
        r = self._new_stay_post(**new)
        self.assertContains(r, 'id="clash"')
        self.assertEqual([(b, room.number) for b, room in r.context["clash"].moves], [(old, "103")])
        self.assertEqual([room.number for room in r.context["clash"].rooms], ["103"])
        self.assertFalse(Guest.objects.filter(name="New Guest").exists())  # nothing saved yet

        r = self._new_stay_post(**new, resolve="move")
        created = Stay.objects.get(guest__name="New Guest")
        self.assertRedirects(r, f"/stays/{created.pk}/", fetch_redirect_response=False)
        old.refresh_from_db()
        self.assertEqual((created.room.number, old.room.number), ("101", "103"))
        self.assertTrue(AuditLog.objects.filter(action="move", stay=old).exists())

    def test_clash_popup_other_room_for_new_stay(self):
        stay(make_guest("Old Guest", "+91 90000 00001"), self.rooms["101"], 0, 2)
        new = {"g-phone": "+91 90000 00009", "g-name": "New Guest"}
        r = self._new_stay_post(**new, resolve=f"room:{self.rooms['102'].pk}")
        self.assertEqual(Stay.objects.get(guest__name="New Guest").room.number, "102")
        # No same-type room free and nothing can be moved → the popup says so.
        stay(make_guest("C", "+91 90000 00003"), self.rooms["103"], 0, 2)
        r = self._new_stay_post(**{"g-phone": "+91 90000 00010", "g-name": "Third"})
        self.assertContains(r, "No other Luxury AC room is free")
        r = self._new_stay_post(**{"g-phone": "+91 90000 00010", "g-name": "Third"}, resolve="move")
        self.assertContains(r, "already booked")
        self.assertFalse(Stay.objects.filter(guest__name="Third").exists())

    def test_validation(self):
        r = self._new_stay_post(**{"g-id_number": "1234"})
        self.assertContains(r, "Aadhaar number must be 12 digits")
        r = self._new_stay_post(**{"g-id_number": "", "g-phone": "+91 90000 00077"})  # ID number optional
        self.assertEqual(Stay.objects.get().guest.id_number, "")
        self.assertContains(self.client.get(f"/stays/{Stay.objects.get().pk}/"), "Not provided")
        Stay.objects.all().delete(); Guest.objects.all().delete()
        # Foreign national details are required at check-in (not when booking ahead)
        r = self._new_stay_post(**{"g-nationality": "British", "g-id_type": "passport", "g-id_number": "X1",
                                   "g-phone": "+44 7700 900111", "checkin": "1"})
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
        # Room 101 booked by a future guest, but other AC rooms are free → extended, future booking moved
        nxt = stay(make_guest("Next", "+91 90000 00002"), self.rooms["101"], 3, 2)
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (T + timedelta(days=5)).isoformat(), "amount": "2000"},
                             follow=True)
        self.assertContains(r, "Next’s booking moved from Room 101 to Room 102")
        s.refresh_from_db(); nxt.refresh_from_db()
        self.assertEqual((s.room.number, s.check_out, nxt.room.number), ("101", T + timedelta(days=5), "102"))

        # Every AC room taken on the next extra night → options page instead
        stay(make_guest("Block1", "+91 90000 00006"), self.rooms["101"], 5, 2)
        stay(make_guest("Block2", "+91 90000 00007"), self.rooms["102"], 5, 2)
        stay(make_guest("Block3", "+91 90000 00008"), self.rooms["103"], 5, 2)
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (T + timedelta(days=6)).isoformat(), "amount": "1500"})
        self.assertContains(r, "All Luxury AC rooms are booked")
        self.assertContains(r, 'name="amount" value="1500')
        self.assertContains(r, "Room <b>201</b>")
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (T + timedelta(days=6)).isoformat(), "amount": "1500",
                                                         "choice": "split", "room": self.rooms["201"].pk})
        cont = Stay.objects.get(linked_to=s)
        self.assertEqual((cont.room.number, cont.total, cont.source), ("201", Decimal("1500"), s.source))

    def test_actions_and_reveal(self):
        s = stay(make_guest(), self.rooms["101"], 0, 2)
        self.client.post(f"/stays/{s.pk}/check-in/")
        self.client.post(f"/stays/{s.pk}/payment/", {"amount": "500", "mode": "cash"})
        s.refresh_from_db()
        self.assertEqual((s.status, s.amount_paid), (Stay.Status.CHECKED_IN, Decimal("500")))
        r = self.client.post(f"/stays/{s.pk}/reveal-id/", {"field": "id_number"})
        self.assertContains(r, "123412341234")
        self.assertTrue(AuditLog.objects.filter(action="reveal_id").exists())

    def test_other_guests_ids_optional(self):
        RoomType.objects.filter(pk=self.ac.pk).update(max_guests=3)
        # 2 guests: second guest's ID saved (encrypted), third row ignored
        r = self._new_stay_post(**{"o-0-name": "Sita Kumar", "o-0-phone": "+91 91234 56789", "o-0-id_number": "5678 5678 5678",
                                   "o-1-name": "Ignored", "o-1-id_number": "1111"})
        s = Stay.objects.get()
        others = list(s.other_guests.all())
        self.assertEqual([(o.position, o.name, o.phone, o.id_number) for o in others],
                         [(2, "Sita Kumar", "+91 91234 56789", "567856785678")])
        with connection.cursor() as cur:
            cur.execute(f"SELECT id_number FROM {StayGuest._meta.db_table}")
            self.assertTrue(cur.fetchone()[0].startswith("enc:"))
        page = self.client.get(f"/stays/{s.pk}/")
        self.assertContains(page, "Sita Kumar")
        self.assertContains(page, 'href="tel:+91 91234 56789"')
        self.assertNotContains(page, "567856785678")
        r = self.client.post(f"/stays/{s.pk}/reveal-id/", {"field": f"other:{others[0].pk}"})
        self.assertContains(r, "567856785678")

        # Edit: 3 guests, guest 2 ID left blank → kept; guest 3 name only (ID optional)
        o = others[0]
        r = self.client.post(f"/stays/{s.pk}/edit/", {
            **{k: v for k, v in self._post_data().items()}, "s-num_guests": 3,
            "o-TOTAL_FORMS": "2", "o-INITIAL_FORMS": "1", "o-0-id": o.pk, "o-0-stay": s.pk, "o-0-name": "Sita Kumar",
            "o-0-phone": "+91 91234 56789", "o-0-id_type": "aadhaar", "o-0-id_number": "",
            "o-1-name": "", "o-1-phone": "+91 99999 00000", "o-1-id_type": "aadhaar", "o-1-id_number": "",
        })
        self.assertRedirects(r, f"/stays/{s.pk}/")
        self.assertEqual([(x.position, x.name, x.id_number) for x in s.other_guests.all()],
                         [(2, "Sita Kumar", "567856785678"), (3, "", "")])
        self.assertEqual(s.other_guests.get(position=3).phone, "+91 99999 00000")  # phone only is enough

        # Bad Aadhaar on an extra guest is rejected
        r = self.client.post(f"/stays/{s.pk}/edit/", {**self._post_data(), "s-num_guests": 2, "o-TOTAL_FORMS": "1",
                                                      "o-INITIAL_FORMS": "0", "o-0-name": "X", "o-0-id_type": "aadhaar",
                                                      "o-0-id_number": "12"})
        self.assertContains(r, "Aadhaar number must be 12 digits")
        r = self.client.post(f"/stays/{s.pk}/edit/", {**self._post_data(), "s-num_guests": 2, "o-TOTAL_FORMS": "1",
                                                      "o-INITIAL_FORMS": "0", "o-0-phone": "123"})
        self.assertContains(r, "at least 10 digits")

        # Back to 1 guest → extra guests removed
        r = self.client.post(f"/stays/{s.pk}/edit/", {**self._post_data(), "s-num_guests": 1})
        self.assertFalse(StayGuest.objects.exists())

    def test_dashboard_tomorrow(self):
        leaving = stay(make_guest(), self.rooms["101"], -1, 2)           # leaves tomorrow → 101 free tomorrow night
        stay(make_guest("Anita", "+91 90000 00021"), self.rooms["102"], 0, 3)   # stays through tomorrow
        stay(make_guest("Kiran", "+91 90000 00022"), self.rooms["103"], 1, 2)   # arrives tomorrow
        r = self.client.get("/")
        self.assertEqual([s.pk for s in r.context["departing_tomorrow"]], [leaving.pk])
        self.assertEqual(sorted(x.number for x in r.context["vacant_tomorrow"]), ["101", "201"])
        self.assertContains(r, "Vacant tomorrow night")

    def _quick(self, name, room, start, nights=None, **over):
        data = {"g-name": name, "g-nationality": "Indian", "g-id_type": "aadhaar",
                "s-source": "airbnb", "s-host": services.host_names()[0], "s-kind": "daily", "s-room": room.pk, "s-check_in": (T + timedelta(days=start)).isoformat(),
                "s-check_out": (T + timedelta(days=start + nights)).isoformat() if nights else "",
                "s-num_guests": 1, "s-total_amount": "2000", "s-amount_paid": "500", "o-TOTAL_FORMS": "0", "o-INITIAL_FORMS": "0"}
        data.update(over)
        return self.client.post("/stays/new/", data)

    def test_quick_future_booking_then_check_in(self):
        self._quick("Meena", self.rooms["101"], 5, 2)
        self._quick("Arun", self.rooms["102"], 1, 3)          # second guest without phone: allowed
        meena = Stay.objects.get(guest__name="Meena")
        self.assertEqual((meena.guest.phone, meena.guest.phone_key, meena.amount_paid), ("", None, Decimal("500")))
        # Check in without details → asked to add them
        r = self.client.post(f"/stays/{meena.pk}/check-in/")
        self.assertRedirects(r, f"/stays/{meena.pk}/edit/?checkin=1", fetch_redirect_response=False)
        page = self.client.get(f"/stays/{meena.pk}/edit/?checkin=1")
        self.assertContains(page, "Save &amp; check in")
        base = {**self._quick_data(meena), "checkin": "1"}
        r = self.client.post(f"/stays/{meena.pk}/edit/", base)
        self.assertContains(r, "This field is required")
        r = self.client.post(f"/stays/{meena.pk}/edit/", {**base, "g-phone": "+91 90000 12345", "g-address": "Kukatpally"})
        meena.refresh_from_db()
        self.assertEqual(meena.status, Stay.Status.CHECKED_IN)

    def _quick_data(self, stay):
        return {"g-guest_id": stay.guest_id, "g-name": stay.guest.name, "g-nationality": "Indian", "g-id_type": "aadhaar",
                "s-source": stay.source, "s-host": stay.host or services.host_names()[0], "s-kind": stay.kind,
                "s-room": stay.room_id, "s-check_in": stay.check_in.isoformat(),
                "s-check_out": stay.check_out.isoformat() if stay.check_out else "", "s-num_guests": 1,
                "s-total_amount": str(stay.total_amount), "s-amount_paid": str(stay.amount_paid),
                "o-TOTAL_FORMS": "0", "o-INITIAL_FORMS": "0"}

    def test_monthly_open_ended(self):
        r = self._quick("Long Stay", self.rooms["201"], -10, **{"s-kind": "monthly", "s-monthly_rent": "12000",
                                                               "s-deposit_amount": "5000", "s-total_amount": "12000"})
        m = Stay.objects.get()
        self.assertIsNone(m.check_out)
        self.assertEqual((m.nights, m.deposit_amount), (10, Decimal("5000")))
        # Daily stay needs a check-out
        r = self._quick("No Date", self.rooms["103"], 1)
        self.assertContains(r, "choose “Monthly”")
        # Room blocked far in the future while open-ended
        r = self._quick("Later", self.rooms["201"], 60, 2)
        self.assertContains(r, "already booked")
        self.assertIn(self.rooms["201"].pk, [b.room_id for b in Stay.objects.live().overlapping(T + timedelta(days=400))])
        # Pages render with an open-ended stay
        for url in ["/", "/calendar/", f"/calendar/room/{self.rooms['201'].pk}/", "/stays/", f"/stays/{m.pk}/", "/upcoming/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(f"/stays/{m.pk}/check-in/")  # no phone → redirected, still upcoming
        m.guest.phone, m.guest.address = "+91 90000 33333", "Hyd"; m.guest.save()
        self.client.post(f"/stays/{m.pk}/check-in/")
        self.client.post(f"/stays/{m.pk}/add-rent/")
        m.refresh_from_db()
        self.assertEqual((m.status, m.total), (Stay.Status.CHECKED_IN, Decimal("24000")))
        r = self.client.post("/export/", {"start": T.isoformat(), "end": T.isoformat(), "action": "download"})
        self.assertEqual(r.status_code, 200)
        self.client.post(f"/stays/{m.pk}/check-out/")
        m.refresh_from_db()
        self.assertEqual((m.check_out, m.status), (T, Stay.Status.CHECKED_OUT))

    def test_upcoming_lists(self):
        a = self._quick("Tomorrow Guest", self.rooms["101"], 1, 2)
        self._quick("Day After", self.rooms["102"], 2, 1)
        self._quick("Next Month", self.rooms["103"], 30, 3)
        r = self.client.get("/")
        self.assertEqual([s.guest.name for s in r.context["arriving_tomorrow"]], ["Tomorrow Guest"])
        self.assertEqual([s.guest.name for s in r.context["arriving_day_after"]], ["Day After"])
        self.assertEqual(r.context["upcoming_count"], 3)
        r = self.client.get("/upcoming/")
        for name in ["Tomorrow Guest", "Day After", "Next Month"]:
            self.assertContains(r, name)

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
        staff("test-admin", password="test-pass-123456")

    def test_login_required(self):
        self.assertRedirects(self.client.get("/stays/"), "/login/?next=/stays/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/healthz/").status_code, 200)

    def test_login_and_lockout(self):
        r = self.client.post("/login/", {"username": "test-admin", "password": "test-pass-123456"})
        self.assertRedirects(r, "/properties/", fetch_redirect_response=False)
        # Only BSR Corridors: straight in, to the same Today page as before.
        self.assertRedirects(self.client.get("/properties/"), "/", fetch_redirect_response=False)
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


class BackupRetentionTests(TestCase):
    def test_keeps_newest_of_each_day_week_and_month(self):
        from datetime import datetime, timedelta, timezone as tz
        from core.gdrive import backups_to_keep
        start = datetime(2026, 11, 5, 21, 0, tzinfo=tz.utc)  # 02:30 IST on 6 Nov
        created = [start - timedelta(days=d) for d in range(400)] + [start - timedelta(days=1, hours=1)]
        created.sort(reverse=True)
        kept = sorted(created[i] for i in backups_to_keep(created))
        days = {c.date() for c in kept}
        self.assertTrue({(start - timedelta(days=d)).date() for d in range(7)} <= days)  # last 7 days
        self.assertEqual(len(kept), len(set(kept)))
        self.assertLessEqual(len(kept), 7 + 4 + 12)
        self.assertGreater(start - kept[0], timedelta(days=300))  # reaches back about a year
        self.assertEqual(len({(c.year, c.month) for c in kept}), 12)

    def test_restore_reads_compressed_backup(self):
        import gzip, tempfile, os
        from django.core.management import call_command
        g = make_guest()
        data = gzip.compress(backup_json())
        g.delete()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "backup_06-11-2026_02-00AM.json.gz")
            open(path, "wb").write(data)
            call_command("restore_backup", path, yes=True, stdout=io.StringIO())
        self.assertEqual(Guest.objects.get().id_number, "123412341234")

    def test_backup_keeps_settings_links_logins_and_payment_links(self):
        import gzip, tempfile, os
        from django.core.management import call_command
        from core.models import PaymentRequest, ShortLink
        _, _, rooms = make_rooms()
        s = stay(make_guest(), rooms["101"], 0, 2)
        User = get_user_model()
        User.objects.create_user("desk", password="desk-password-1")
        AppSetting.put("hotel_info_wifi_password", "secret-wifi")
        ShortLink.objects.create(code="Ab3dE9x", path="/pay/x/")
        PaymentRequest.objects.create(stay=s, amount=Decimal("500"))
        data = gzip.compress(backup_json())
        # A fresh database: only the admin login (made on deploy, with a different ID) exists.
        PaymentRequest.objects.all().delete()
        ShortLink.objects.all().delete()
        AppSetting.objects.all().delete()
        User.objects.all().delete()
        User.objects.create_superuser("admin", password="admin-password-1")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "backup.json.gz")
            open(path, "wb").write(data)
            call_command("restore_backup", path, yes=True, stdout=io.StringIO())
        self.assertEqual(AppSetting.get("hotel_info_wifi_password"), "secret-wifi")
        self.assertTrue(ShortLink.objects.filter(code="Ab3dE9x").exists())
        self.assertEqual(PaymentRequest.objects.get().amount, Decimal("500"))
        self.assertTrue(User.objects.get(username="desk").check_password("desk-password-1"))
        self.assertEqual(User.objects.filter(username="admin").count(), 1)
        # New records after a restore don't clash with restored IDs.
        ShortLink.objects.create(code="New1234", path="/pay/y/")
        from core.models import Payment
        Payment.objects.create(stay=Stay.objects.get(), amount=Decimal("1"))


def got(s, t):
    from core import revenue
    return [(x.day, x.amount) for x in revenue.received_on(s, t)]


class RevenueTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("rev-admin", password="test-pass-123456"))

    def _unlock(self):
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})

    def test_revenue_split_by_night_and_money(self):
        from core import revenue
        # In-house: 2 nights stayed (incl. tonight) of 4, ₹6,000 total, ₹2,000 paid.
        a = stay(make_guest(), self.rooms["101"], -1, 4, status=Stay.Status.CHECKED_IN, amount_paid=Decimal(2000))
        # Checked out and fully paid; upcoming with an advance; transferred (left out).
        stay(make_guest("B", "+91 90000 00001"), self.rooms["102"], -3, 2, status=Stay.Status.CHECKED_OUT,
             amount_paid=Decimal(3000))
        stay(make_guest("C", "+91 90000 00002"), self.rooms["201"], 2, 1, amount_paid=Decimal(500))
        services.transfer_out(stay(make_guest("D", "+91 90000 00003"), self.rooms["103"], 0, 2), "Hotel X")
        r = revenue.build("all")
        self.assertEqual(r.in_period.nights, 4 + 2 + 1)
        self.assertEqual(r.in_period.earned, Decimal(1500) * 2 + Decimal(3000))
        self.assertEqual(r.in_period.ahead, Decimal(1500) * 2 + Decimal(1500))
        # Received night by night: the in-house guest's first night (₹1,500) is received, ₹500 of the ₹2,000 is held.
        self.assertEqual((r.collected, r.held_in_house), (Decimal(4500), Decimal(500)))
        self.assertEqual(r.balance_pending, Decimal(4000))
        # Not checked in yet: the whole booking is yet to receive, advance included.
        self.assertEqual((r.to_receive, r.advance_upcoming), (Decimal(1500), Decimal(500)))
        self.assertEqual(r.in_period.received, Decimal(4500))
        self.assertEqual(r.collected + r.held_in_house + r.balance_pending + r.to_receive, Decimal(3000 + 6000 + 1500))
        self.assertEqual(r.by_type["Luxury AC"].total, Decimal(9000))
        self.assertEqual([s.pk for s in r.unpaid], [a.pk])

    def test_password_set_unlock_and_lockout(self):
        self.assertContains(self.client.get("/revenue/"), "Set a revenue password")
        self._unlock()
        self.assertContains(self.client.get("/revenue/"), "Previous months")
        self.assertNotIn("secret1", AppSetting.get("revenue_password_hash"))
        self.client.post("/revenue/lock/")
        self.assertContains(self.client.get("/revenue/"), "Enter the revenue password")
        for _ in range(5):
            self.client.post("/revenue/", {"password": "wrong"})
        self.client.post("/revenue/", {"password": "secret1"})
        self.assertContains(self.client.get("/revenue/"), "Enter the revenue password")  # locked out

    def test_received_counted_night_by_night(self):
        from core import revenue
        # 10 nights, ₹10,000, paid in full at check-in: ₹1,000 counts each morning after a night stayed.
        s = Stay(check_in=date(2026, 11, 1), check_out=date(2026, 11, 11), status=Stay.Status.CHECKED_IN,
                 total_amount=Decimal(10000), amount_paid=Decimal(10000))
        self.assertEqual(got(s, date(2026, 11, 1)), [])  # check-in day: nothing yet
        self.assertEqual(got(s, date(2026, 11, 3)), [(date(2026, 11, 2), Decimal(2000))])
        self.assertEqual(got(s, date(2026, 11, 11)), [(date(2026, 11, 10), Decimal(10000))])
        # Only money paid counts: ₹2,000 advance, 5 nights stayed → ₹2,000 received, the rest is balance pending.
        s.amount_paid = Decimal(2000)
        self.assertEqual(got(s, date(2026, 11, 6)), [(date(2026, 11, 2), Decimal(2000))])
        # Early check-out after 6 nights with the bill changed to ₹6,000 and ₹6,500 paid (extras): the rest counts
        # on the check-out day.
        s.check_out, s.total_amount, s.amount_paid, s.status = date(2026, 11, 7), Decimal(6000), Decimal(6500), \
            Stay.Status.CHECKED_OUT
        self.assertEqual(got(s, date(2026, 11, 20)), [(date(2026, 11, 6), Decimal(6000)), (date(2026, 11, 7), Decimal(500))])

        stay(make_guest(), self.rooms["101"], -60, 3, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(4500))
        stay(make_guest("B", "+91 90000 00001"), self.rooms["102"], -1, 2, status=Stay.Status.CHECKED_IN,
             amount_paid=Decimal(1000))
        r = revenue.build("all")
        self.assertEqual((r.in_period.received, r.held_in_house), (Decimal(5500), 0))

    def test_stay_across_month_end_counts_nights_in_each_month(self):
        s = Stay(check_in=date(2026, 10, 29), check_out=date(2026, 11, 2), status=Stay.Status.CHECKED_OUT,
                 total_amount=Decimal(4000), amount_paid=Decimal(4000))
        self.assertEqual(got(s, date(2026, 11, 5)), [(date(2026, 10, 31), Decimal(3000)), (date(2026, 11, 1), Decimal(1000))])

    def test_monthly_rent_counts_day_by_day(self):
        from core import revenue
        # Joined 1 Oct, ₹9,300 for 1–31 Oct = ₹300 a day; November's month (30 days) = ₹310 a day.
        s = Stay(kind=Stay.Kind.MONTHLY, check_in=date(2026, 10, 1), status=Stay.Status.CHECKED_IN,
                 monthly_rent=Decimal(9300), amount_paid=Decimal(18600))
        self.assertEqual(got(s, date(2026, 10, 1)), [])
        self.assertEqual(got(s, date(2026, 10, 11)), [(date(2026, 10, 10), Decimal(3000))])
        self.assertEqual(got(s, date(2026, 11, 2)), [(date(2026, 10, 31), Decimal(9300)), (date(2026, 11, 1), Decimal(310))])
        # Leaves 5 Nov having paid ₹12,000: 4 November days counted, the rest on the leaving day.
        s.amount_paid, s.status, s.check_out = Decimal(12000), Stay.Status.CHECKED_OUT, date(2026, 11, 5)
        self.assertEqual(got(s, date(2026, 11, 20)), [
            (date(2026, 10, 31), Decimal(9300)), (date(2026, 11, 4), Decimal(1240)), (date(2026, 11, 5), Decimal(1460))])
        s.check_in = date(2026, 1, 31)  # short months
        self.assertEqual(revenue._same_day_months_later(s.check_in, 1), date(2026, 2, 28))

    def test_received_shows_dates_covered(self):
        from core import revenue
        s = Stay(kind=Stay.Kind.MONTHLY, check_in=date(2026, 10, 14), status=Stay.Status.CHECKED_OUT,
                 check_out=date(2026, 12, 1), monthly_rent=Decimal(8000), amount_paid=Decimal(12000))
        items = revenue.received_on(s, date(2026, 12, 5))
        self.assertEqual([(x.what, x.start, x.end) for x in items], [
            ("Month 1 rent · 18 days", date(2026, 10, 14), date(2026, 10, 31)),
            ("Month 1 rent · 13 days", date(2026, 11, 1), date(2026, 11, 13)),
            ("Month 2 rent · 15 days", date(2026, 11, 14), date(2026, 11, 28))])  # paid up to here
        self.assertEqual(sum(x.amount for x in items), Decimal(12000))

    def test_cancelled_booking_treated_as_refunded(self):
        from core import revenue
        s = stay(make_guest(), self.rooms["101"], 5, 2, amount_paid=Decimal(1500))
        self.assertEqual(revenue.build("all").advance_upcoming, Decimal(1500))
        services.cancel(s)
        r = revenue.build("all")
        self.assertEqual((r.collected, r.advance_upcoming, r.in_period.received, r.booked_value), (0, 0, 0, 0))

    def test_reminders_rent_not_added_and_never_checked_in(self):
        from core import revenue
        s = Stay(guest=make_guest(), room=self.rooms["201"], kind=Stay.Kind.MONTHLY, check_in=T - timedelta(days=40),
                 status=Stay.Status.CHECKED_IN, monthly_rent=Decimal(8000), total_amount=Decimal(8000))
        services.save_stay(s, action="create", summary="test")
        self.assertEqual(revenue.rent_not_added(T), [(s, 2, Decimal(8000))])  # month 2 started, not added
        missed = stay(make_guest("M", "+91 90000 00005"), self.rooms["101"], -5, 2, amount_paid=Decimal(500))
        page = self.client.get("/")
        self.assertContains(page, "Monthly rent to add")
        self.assertContains(page, "Never checked in")
        self.client.post(f"/stays/{s.pk}/add-rent/", {"next": "/"})
        self.assertEqual(revenue.rent_not_added(T), [])
        self.assertEqual(missed.status, Stay.Status.UPCOMING)

    def test_by_room_shows_nights_still_due(self):
        from core import revenue
        # Checked in yesterday for 3 nights (₹1,500 each), ₹2,000 paid: the first night is received; the month's
        # other nights are still due.
        s = stay(make_guest(), self.rooms["101"], -1, 3, status=Stay.Status.CHECKED_IN, amount_paid=Decimal(2000))
        month = T.replace(day=1)
        in_month = sum(1 for k in range(-1, 2) if T + timedelta(days=k) >= month)
        received = Decimal(1500) if T - timedelta(days=1) >= month else 0
        rows = {x["room"].number: x for x in revenue.rooms_for_month(revenue.build(), month)}
        self.assertEqual((rows["101"]["received"], rows["101"]["due_total"]), (received, 1500 * in_month - received))
        self._unlock()
        page = self.client.get(f"/revenue/{month:%Y-%m}/").content.decode()
        self.assertIn(f"Received ₹{received:,.0f} + still due ₹{1500 * in_month - received:,.0f}", page)
        services.check_out(s)
        r = revenue.build()
        self.assertEqual((r.collected, r.held_in_house), (Decimal(2000), 0))
        rows = {x["room"].number: x for x in revenue.rooms_for_month(r, month)}
        self.assertEqual(rows["101"]["due"], [])

    def test_overall_page(self):
        from core import revenue
        stay(make_guest(), self.rooms["101"], -3, 2, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(3000))
        stay(make_guest("B", "+91 90000 00001"), self.rooms["102"], -1, 2, status=Stay.Status.CHECKED_IN,
             amount_paid=Decimal(1000))  # ₹3,000 bill
        stay(make_guest("C", "+91 90000 00002"), self.rooms["201"], 3, 2, amount_paid=Decimal(500))  # ₹3,000 bill
        Expense.objects.create(reason="Soap", category="supplies", amount=Decimal(400))
        r = revenue.build("all")
        self.assertEqual((r.paid_total, r.still_to_receive, r.booked_value), (Decimal(4500), Decimal(4500), Decimal(9000)))
        self.assertEqual((r.net_cash, r.cash_in_hand), (Decimal(3600), Decimal(4100)))
        self.assertEqual(self.client.get("/revenue/overall/").status_code, 302)  # needs the revenue password
        self._unlock()
        page = self.client.get("/revenue/overall/")
        self.assertContains(page, "₹4,500")
        self.assertContains(page, "Housekeeping &amp; guest supplies")
        self.assertContains(self.client.get("/revenue/"), 'href="/revenue/overall/"')

    def test_monthly_guest_in_house_received_day_by_day(self):
        from core import revenue
        s = Stay(guest=make_guest(), room=self.rooms["201"], kind=Stay.Kind.MONTHLY, check_in=T - timedelta(days=9),
                 status=Stay.Status.CHECKED_IN, total_amount=Decimal(10000), amount_paid=Decimal(8000),
                 monthly_rent=Decimal(10000))
        services.save_stay(s, action="create", summary="test")
        days = (revenue._same_day_months_later(s.check_in, 1) - s.check_in).days
        nine = (Decimal(10000) / days * 9).quantize(Decimal("0.01"))
        r = revenue.build("all")
        self.assertEqual((r.collected, r.held_in_house, r.balance_pending, r.to_receive),
                         (nine, 8000 - nine, Decimal(2000), 0))

    def test_page_renders_with_data_for_every_period(self):
        stay(make_guest(), self.rooms["101"], -40, 3, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(1000))
        stay(make_guest("B", "+91 90000 00001"), self.rooms["201"], 1, 2)
        self._unlock()
        month = T.replace(day=1)
        last_month = (month - timedelta(days=1)).replace(day=1)
        page = self.client.get("/revenue/")
        self.assertContains(page, "Room sales by month")
        self.assertContains(page, f'href="/revenue/{month:%Y-%m}/" aria-current="page"')  # month tabs
        # Room sales follow the chosen month.
        from core import revenue
        old = revenue.build(month=last_month).in_period
        self.assertEqual(old, revenue.build("last_month").in_period)
        self.assertContains(self.client.get(f"/revenue/{last_month:%Y-%m}/"), f"Room sales <span class=\"muted small\">— {last_month:%B %Y}")


class ExpenseTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("exp-admin", password="test-pass-123456"))

    def test_expenses_need_revenue_password_then_come_back(self):
        r = self.client.get("/expenses/new/")
        self.assertRedirects(r, "/revenue/?next=/expenses/new/", fetch_redirect_response=False)
        r = self.client.post("/revenue/?next=/expenses/new/", {"password": "secret1", "confirm": "secret1"})
        self.assertRedirects(r, "/expenses/new/", fetch_redirect_response=False)
        r = self.client.post("/revenue/?next=https://evil.example/", {"password": "secret1"})
        self.assertRedirects(r, "/revenue/", fetch_redirect_response=False)

    def test_add_list_edit_delete_and_profit(self):
        from core import revenue
        from core.models import Expense
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})
        r = self.client.post("/expenses/new/", {
            "date": T.isoformat(), "category": "repairs", "reason": "AC repair", "amount": "2500",
            "payment_mode": "upi", "paid_to": "Cool Air Services", "reference": "INV-42", "room": self.rooms["101"].pk})
        self.assertRedirects(r, "/expenses/", fetch_redirect_response=False)
        self.client.post("/expenses/new/", {"date": T.isoformat(), "category": "electricity", "reason": "Bill",
                                            "amount": "4000", "payment_mode": "bank"})
        self.assertContains(self.client.post("/expenses/new/", {"date": T.isoformat(), "category": "other",
                                                                "reason": "x", "amount": "0", "payment_mode": "cash"}),
                            "more than zero")
        page = self.client.get("/expenses/")  # this month by default
        self.assertContains(page, "AC repair")
        self.assertContains(page, "₹6,500")
        self.assertContains(self.client.get(f"/expenses/?month={T:%Y-%m}&category=repairs"), "₹2,500")
        self.assertContains(self.client.get("/expenses/?period=all"), "AC repair")

        stay(make_guest(), self.rooms["102"], -2, 2, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(3000))
        rep = revenue.build("all")
        self.assertEqual(rep.in_period.expenses, Decimal(6500))
        self.assertEqual(rep.in_period.profit, Decimal(3000) - Decimal(6500))
        self.assertEqual(rep.net_cash, Decimal(3000) - Decimal(6500))
        self.assertContains(self.client.get("/revenue/overall/"), "(received − expenses)")

        e = Expense.objects.get(reason="AC repair")
        self.client.post(f"/expenses/{e.pk}/delete/")
        self.assertFalse(Expense.objects.filter(pk=e.pk).exists())
        self.assertTrue(AuditLog.objects.filter(action="expense", summary__contains="Deleted").exists())

    def test_expenses_in_export_and_backup(self):
        from core.models import Expense
        from openpyxl import load_workbook
        Expense.objects.create(date=T, category="laundry", reason="Bedsheets", amount=Decimal(800))
        wb = load_workbook(io.BytesIO(build_workbook()))
        self.assertEqual(wb["Expenses"]["C2"].value, "Bedsheets")
        self.assertIn('"model": "core.expense"', backup_json().decode())


class HostSettlementWhatsAppTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("x-admin", password="test-pass-123456"))

    def test_host_shown_in_calendar_and_list_editable(self):
        self.assertEqual(services.host_names(), ["Shekhar", "Mounika", "Satish"])
        stay(make_guest(), self.rooms["101"], 0, 2, host="Mounika")
        page = self.client.get("/calendar/").content.decode()
        self.assertIn("<small>Mounika</small>", page)
        self.client.post("/settings/", {"action": "hosts", "hosts": "Shekhar, Satish, Shekhar"})
        self.assertEqual(services.host_names(), ["Shekhar", "Satish"])
        # A stay's old host stays selectable after being removed from the list.
        form = self.client.get(f"/stays/{Stay.objects.get().pk}/edit/").content.decode()
        self.assertIn('value="Mounika" selected', form)

    def test_settlement_per_month(self):
        from core import revenue
        month = T.replace(day=1)
        last_month = (month - timedelta(days=1)).replace(day=1)
        s = stay(make_guest(), self.rooms["101"], -40, 2, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(3000))
        s.check_in, s.check_out = last_month, last_month + timedelta(days=2)
        s.save()
        Expense.objects.create(date=last_month, reason="Soap", amount=Decimal(500))
        Expense.objects.create(date=T, reason="Bulb", amount=Decimal(200))
        r = revenue.build()
        lm = r.ledger[last_month]
        self.assertEqual((lm.received, lm.expenses, lm.profit, lm.left), (Decimal(3000), Decimal(500), Decimal(2500), Decimal(2500)))
        self.assertEqual(r.ledger[month].expenses, Decimal(200))
        self.assertEqual(r.left_to_settle, Decimal(2500))  # this month is still running

        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})
        page = self.client.get("/revenue/").content.decode()  # this month, with past months listed below
        self.assertIn(f'name="month" value="{month:%Y-%m-%d}"', page)
        self.assertIn(f'/revenue/{last_month:%Y-%m}/', page)
        self.assertIn("₹2,500 profit from past months is not settled yet", page)
        page = self.client.get(f"/revenue/{last_month:%Y-%m}/").content.decode()
        self.assertIn(f'name="month" value="{last_month:%Y-%m-%d}"', page)
        self.assertIn('value="2500"', page)
        self.assertIn("2 nights", page)  # the stay's nights are listed under their month
        s2 = stay(make_guest("Z", "+91 90000 00009"), self.rooms["102"], 0, 1, status=Stay.Status.CHECKED_IN)
        s2.check_in, s2.check_out = last_month + timedelta(days=27), month + timedelta(days=1)
        s2.total_amount = Decimal(7000)
        s2.save()
        # By room: a stay from late last month into this month — last month's nights are due in last month.
        rows = {x["room"].number: x for x in revenue.rooms_for_month(revenue.build(), last_month)}
        nights = (s2.check_out - s2.check_in).days
        self.assertEqual((rows["101"]["received"], rows["102"]["due_total"]),
                         (Decimal(3000), (Decimal(7000) / nights * (nights - 1)).quantize(Decimal("0.01"))))
        self.assertContains(self.client.get(f"/revenue/{last_month:%Y-%m}/"), f"{nights - 1} nights in")
        self.assertEqual(self.client.get("/revenue/2099-01/").status_code, 302)  # future month
        self.assertEqual(self.client.get("/revenue/2026-13/").status_code, 404)
        # Settle in two parts.
        self.client.post("/revenue/settle/", {"month": last_month.isoformat(), "amount": "1000", "notes": "Part 1"})
        self.client.post("/revenue/settle/", {"month": last_month.isoformat(), "amount": "1500"})
        r = revenue.build()
        self.assertEqual((r.ledger[last_month].settled, r.ledger[last_month].left, r.left_to_settle), (Decimal(2500), 0, 0))
        self.assertEqual(Settlement.objects.filter(month=last_month).count(), 2)
        self.assertContains(self.client.get(f"/revenue/{last_month:%Y-%m}/"), "Part 1")
        # A month that hasn't started can't be settled.
        future = (month + timedelta(days=40)).replace(day=1)
        self.client.post("/revenue/settle/", {"month": future.isoformat(), "amount": "10"})
        self.assertFalse(Settlement.objects.filter(month=future).exists())

        self.client.post(f"/revenue/settle/{Settlement.objects.get(notes='Part 1').pk}/delete/")
        self.assertEqual(revenue.build().ledger[last_month].left, Decimal(1000))
        # Month link on the expenses page.
        self.assertContains(self.client.get(f"/expenses/?month={last_month:%Y-%m}"), "Soap")
        self.assertNotContains(self.client.get(f"/expenses/?month={last_month:%Y-%m}"), "Bulb")

    def test_profit_shared_by_partners_not_hosts(self):
        stay(make_guest(), self.rooms["101"], -3, 2, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(3000))
        self.assertEqual((services.host_names(), services.partner_names()),
                         (["Shekhar", "Mounika", "Satish"], ["Shekhar", "Satish"]))
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})
        page = self.client.get(f"/revenue/{T - timedelta(days=1):%Y-%m}/").content.decode()  # month of check-out
        self.assertIn("shared by Shekhar & Satish = <b>₹1,500 each</b>", page)
        self.assertIn("Shared equally – Shekhar, Satish", page)
        self.client.post("/settings/", {"action": "partners", "partners": "Shekhar, Satish, Mounika"})
        self.assertEqual(services.partner_names(), ["Shekhar", "Satish", "Mounika"])
        self.assertEqual(services.host_names(), ["Shekhar", "Mounika", "Satish"])  # hosts unchanged

    def test_settle_needs_revenue_password(self):
        self.client.post("/revenue/settle/", {"amount": "100"})
        self.assertFalse(Settlement.objects.exists())

    def test_whatsapp_message_filled_in_and_logged(self):
        from urllib.parse import parse_qs, urlparse
        from core import whatsapp
        whatsapp.save_hotel_info({**whatsapp.hotel_info(), "wifi_password": "corridors@123"})
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], 0, 2)
        response = self.client.get(f"/stays/{s.pk}/whatsapp/?t=checkin")
        url = urlparse(response["Location"])
        self.assertEqual((url.netloc, url.path), ("web.whatsapp.com", "/send"))  # computer: WhatsApp Web
        self.assertEqual(parse_qs(url.query)["phone"], ["919876543210"])
        text = parse_qs(url.query)["text"][0]
        self.assertIn("Ravi Kumar", text)
        self.assertIn("Room: 101", text)
        self.assertIn("corridors@123", text)
        self.assertTrue(AuditLog.objects.filter(stay=s, action="whatsapp").exists())

        self.assertTrue(text.startswith("Dear Ravi Kumar,\n\n"))  # formal
        self.assertTrue(text.endswith("\n\n- BSR Corridors"))  # every message is signed off
        custom = self.client.get(f"/stays/{s.pk}/whatsapp/", {"text": "Hello {guest}"})
        self.assertIn("text=Hello%20Ravi%20Kumar%0A%0A-%20BSR%20Corridors", custom["Location"])  # typed text too
        # Phone: straight into the WhatsApp app. No text: just open the chat.
        phone = self.client.get(f"/stays/{s.pk}/whatsapp/", HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14) Mobile")
        self.assertEqual(phone["Location"], "whatsapp://send?phone=919876543210")

    def test_departing_today_button_and_message_all_in_house(self):
        details = MessageTemplate.objects.get(name="Check-out details")
        self.assertEqual(details.shortcut, "departing")  # set by the migrations
        leaving = stay(make_guest(), self.rooms["101"], -2, 2, status=Stay.Status.CHECKED_IN)  # checks out today
        staying = stay(make_guest("Asha", "+91 90000 00002"), self.rooms["102"], -1, 3, status=Stay.Status.CHECKED_IN)
        stay(make_guest("No Phone", ""), self.rooms["103"], -1, 3, status=Stay.Status.CHECKED_IN)
        page = self.client.get("/").content.decode()
        self.assertIn(f'href="/stays/{leaving.pk}/whatsapp/?t={details.pk}" target="whatsapp"', page)
        self.assertNotIn(f'/stays/{staying.pk}/whatsapp/?t={details.pk}', page)  # only on Departing today
        self.assertIn('href="/whatsapp/in-house/"', page)

        page = self.client.get("/whatsapp/in-house/", {"text": "Hi {guest}, water is off 2–4 PM."}).content.decode()
        self.assertIn("Hi Ravi Kumar, water is off 2–4 PM.", page)  # preview for the first guest
        self.assertIn(f'/stays/{staying.pk}/whatsapp/?text=Hi+%7Bguest%7D', page)
        self.assertIn("No phone number (skipped): No Phone", page)
        self.assertEqual(page.count("data-wa-send"), 2)
        page = self.client.get("/whatsapp/in-house/", {"t": details.pk}).content.decode()
        self.assertIn(f"/stays/{leaving.pk}/whatsapp/?t={details.pk}", page)

    def test_today_page_reminder_buttons(self):
        reminder = MessageTemplate.objects.get(shortcut="balance")
        tomorrow = MessageTemplate.objects.get(shortcut="tomorrow")
        review = MessageTemplate.objects.get(shortcut="checked_out")
        owes = stay(make_guest(), self.rooms["101"], -1, 2, status=Stay.Status.CHECKED_IN)  # leaves tomorrow, unpaid
        left = stay(make_guest("Gone", "+91 90000 00007"), self.rooms["102"], -2, 2, status=Stay.Status.CHECKED_IN)
        services.check_out(left)
        page = self.client.get("/").content.decode()
        self.assertIn(f"/stays/{owes.pk}/whatsapp/?t={reminder.pk}", page)
        self.assertIn(f"/stays/{owes.pk}/whatsapp/?t={tomorrow.pk}", page)
        self.assertIn("Checked out today", page)
        self.assertIn(f"/stays/{left.pk}/whatsapp/?t={review.pk}", page)
        # One message per list: giving the shortcut to another message takes it away from the first.
        self.client.post("/whatsapp/messages/new/", {"name": "Pay soon", "body": "Pay {balance}", "shortcut": "balance",
                                                      "sort_order": 9})
        reminder.refresh_from_db()
        self.assertEqual(reminder.shortcut, "")

    def test_check_in_opens_welcome_and_only_one_check_in_message(self):
        s = stay(make_guest(), self.rooms["101"], 0, 2)
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertIn(f'data-wa-open="/stays/{s.pk}/whatsapp/?t=checkin"', page)
        response = self.client.post(f"/stays/{s.pk}/check-in/")
        self.assertEqual(response["Location"], f"/stays/{s.pk}/?welcome=1")

        other = MessageTemplate.objects.exclude(on_check_in=True).first()
        self.client.post(f"/whatsapp/messages/{other.pk}/", {"name": other.name, "body": other.body,
                                                             "on_check_in": "on", "sort_order": 0})
        self.assertEqual(list(MessageTemplate.objects.filter(on_check_in=True)), [other])

    def test_wa_number(self):
        from core.whatsapp import wa_number
        self.assertEqual(wa_number("+91 98765 43210"), "919876543210")
        self.assertEqual(wa_number("09876543210"), "919876543210")
        self.assertEqual(wa_number("+1 415 555 0100"), "14155550100")

    def test_wifi_name_can_use_floor(self):
        from core import whatsapp
        s = stay(make_guest(), self.rooms["201"], 0, 1)
        self.assertEqual(whatsapp.context(s)["wifi_name"], "BSR (your floor name)")
        whatsapp.save_hotel_info({**whatsapp.hotel_info(), "wifi_name": "BSR {floor}"})
        self.assertEqual(whatsapp.context(s)["wifi_name"], "BSR 2")
        self.rooms["201"].floor = "Second"
        self.rooms["201"].save()
        s.refresh_from_db()
        self.assertEqual(whatsapp.context(s)["wifi_name"], "BSR Second")

    def test_check_out_opens_departure_message(self):
        from urllib.parse import parse_qs, urlparse
        s = stay(make_guest(), self.rooms["101"], -2, 2, status=Stay.Status.CHECKED_IN)
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertIn(f'data-wa-open="/stays/{s.pk}/whatsapp/?t=checkout"', page)
        self.assertIn("Check out &amp; send message", page)
        self.assertEqual(self.client.post(f"/stays/{s.pk}/check-out/")["Location"], f"/stays/{s.pk}/?departed=1")
        self.assertContains(self.client.get(f"/stays/{s.pk}/?departed=1"), "Send departure message")
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/?t=checkout")["Location"]).query)["text"][0]
        self.assertTrue(text.startswith("Dear Ravi Kumar,\n\nThank you for staying with us at BSR Corridors."))
        self.assertIn("Room 101", text)
        self.assertEqual(text.count("- BSR Corridors"), 1)

    def test_payment_done_message_with_amount(self):
        from urllib.parse import parse_qs, urlparse
        done = MessageTemplate.objects.get(name="Payment done")
        s = stay(make_guest(phone="98765 43210"), self.rooms["302"] if "302" in self.rooms else self.rooms["101"], 0, 2)
        r = self.client.post(f"/stays/{s.pk}/payment/", {"amount": "1200", "mode": "upi"})
        self.assertEqual(r["Location"], f"/stays/{s.pk}/?paid=1200#payment")
        page = self.client.get(f"/stays/{s.pk}/?paid=1200").content.decode()
        self.assertIn(f"/stays/{s.pk}/whatsapp/?t={done.pk}&amp;amount=1200", page)  # offered right after paying
        self.assertIn('class="wa-amount"', page)  # amount box next to the button
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/", {"t": done.pk, "amount": "1200"})
                                 ["Location"]).query)["text"][0]
        self.assertIn("receipt of ₹1,200 towards your stay in Room 101", text)
        self.assertIn("Balance due: ₹1,800", text)
        self.assertTrue(text.endswith("- BSR Corridors"))
        # Without an amount: the latest payment.
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/", {"t": done.pk})["Location"]).query)["text"][0]
        self.assertIn("receipt of ₹1,200", text)


class InvoiceTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("inv-admin", password="test-pass-123456"))

    def test_invoice_number_pdf_and_guest_link(self):
        from urllib.parse import parse_qs, urlparse
        from core import invoice
        s = Stay(guest=make_guest(phone="98765 43210"), room=self.rooms["101"], check_in=date(2026, 11, 15),
                 check_out=date(2026, 11, 18), total_amount=Decimal(4500), source="walk_in",
                 status=Stay.Status.CHECKED_IN)
        services.save_stay(s, action="create", summary="test")
        self.assertEqual(invoice.number(s), "261115-261118-101")
        pt = RoomType.objects.create(name="Penthouse", max_guests=4)
        self.assertEqual(invoice.room_code(Room.objects.create(number="PH", room_type=pt)), "601")
        services.add_payment(s, Decimal(4500), "upi")
        # Not generated yet: Generate button, no PDF; Check out and the invoice messages show the loader.
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertIn("Generate invoice", page)
        self.assertNotIn("View PDF", page)
        self.assertIn('data-busy="Checking out and generating the invoice…"', page)
        self.assertEqual(self.client.get(f"/stays/{s.pk}/invoice/").status_code, 404)
        # A message without the invoice link doesn't make one.
        t = MessageTemplate.objects.get(name="Payment reminder")
        self.client.get(f"/stays/{s.pk}/whatsapp/?t={t.pk}")
        self.assertFalse(invoice.generated_at(s))
        # Sending Check-out details makes it first, and the message carries the link.
        t = MessageTemplate.objects.get(name="Check-out details")
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/?t={t.pk}")["Location"]).query)["text"][0]
        self.assertTrue(invoice.generated_at(s))
        self.assertRegex(text, r"Your invoice \(261115-261118-101\):\nhttp://testserver/s/[A-Za-z2-9]{7}\n")
        self.assertNotIn("data-busy", self.client.get(f"/stays/{s.pk}/").content.decode())
        # Check-out generates it; the departure message then carries the guest's link.
        self.client.post(f"/stays/{s.pk}/check-out/")
        self.assertTrue(invoice.has_invoice(s))
        r = self.client.get(f"/stays/{s.pk}/invoice/")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(r.content.startswith(b"%PDF"))
        s.refresh_from_db()
        no = invoice.number(s)
        self.assertIn(f'filename="Invoice {no}.pdf"', r["Content-Disposition"])
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/?t=checkout")["Location"]).query)["text"][0]
        link = next(w for w in text.split() if "/s/" in w)  # short link → the signed invoice address
        self.assertIn("Your invoice (", text)
        self.client.logout()
        path = self.client.get(urlparse(link).path)["Location"]
        self.assertTrue(path.startswith("/invoice/"))
        self.assertEqual(self.client.get(path)["Content-Type"], "application/pdf")
        self.assertEqual(self.client.get(path[:-3] + "x1/").status_code, 404)  # altered link
        self.assertEqual(self.client.get(f"/stays/{s.pk}/invoice/").status_code, 302)  # staff link needs login
        self.client.force_login(get_user_model().objects.get(username="inv-admin"))
        # "WhatsApp invoice" button, and the link in the Check-out details message.
        msg = MessageTemplate.objects.get(name="Invoice")
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), f"/stays/{s.pk}/whatsapp/?t={msg.pk}")
        for name in ("Invoice", "Check-out details"):
            t = MessageTemplate.objects.get(name=name)
            text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/?t={t.pk}")["Location"]).query)["text"][0]
            self.assertIn(no, text)
            self.assertIn("http://testserver/s/", text)

    def test_extend_opens_stay_extended_message(self):
        from urllib.parse import parse_qs, urlparse
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 3, status=Stay.Status.CHECKED_IN,
                 amount_paid=Decimal(4000))  # total ₹4,500: ₹500 still due
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertNotIn(">Stay extended<", page)  # not extended yet
        r = self.client.get(f"/stays/{s.pk}/whatsapp/?t={MessageTemplate.objects.get(name='Stay extended').pk}")
        self.assertRedirects(r, f"/stays/{s.pk}/", fetch_redirect_response=False)  # nothing to tell yet
        old_out = s.check_out
        r = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (old_out + timedelta(days=2)).isoformat(),
                                                        "amount": "3000", "paid_now": "1000", "payment_mode": "cash"},
                             headers={"X-Requested-With": "fetch"})
        d = r.json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["next"], f"/stays/{s.pk}/")
        text = parse_qs(urlparse(self.client.get(d["wa"])["Location"]).query)["text"][0]
        self.assertIn(f"{old_out:%d-%m-%Y} to", text)
        self.assertIn(f"{old_out + timedelta(days=2):%d-%m-%Y}", text)
        self.assertIn("(2 night(s))", text)
        self.assertIn("Amount for the extended stay: ₹3,000", text)
        self.assertIn("Previous balance: ₹500", text)
        self.assertIn("Total amount to be paid: ₹2,500", text)  # 500 + 3000 − 1000 paid now
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), ">Stay extended<")
        # Extended again with nothing due before: the "Previous balance" line is left out.
        s.refresh_from_db()
        services.add_payment(s, Decimal(2500), "upi")
        self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (old_out + timedelta(days=3)).isoformat(),
                                                    "amount": "1500"})
        s.refresh_from_db()
        text = parse_qs(urlparse(self.client.get(d["wa"])["Location"]).query)["text"][0]
        self.assertNotIn("Previous balance", text)
        self.assertIn("Total amount to be paid: ₹1,500", text)
        # A clash (or a wrong date) through the page script: nothing changes, the page is shown normally instead.
        bad = self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": old_out.isoformat(), "amount": "0"},
                               headers={"X-Requested-With": "fetch"})
        self.assertFalse(bad.json()["ok"])

    def test_platform_booking_not_invoiced_automatically(self):
        from urllib.parse import parse_qs, urlparse
        from core import invoice
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 2, status=Stay.Status.CHECKED_IN,
                 source="airbnb", amount_paid=Decimal(3000))
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertIn("Generate invoice", page)  # can be made by hand
        self.assertIn("not made automatically", page)
        self.assertNotIn("WhatsApp invoice", page)
        self.client.post(f"/stays/{s.pk}/check-out/")
        self.assertFalse(invoice.has_invoice(s))  # check-out doesn't make one
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/?t=checkout")["Location"]).query)["text"][0]
        self.assertNotIn("invoice", text.lower())
        self.assertFalse(invoice.generated_at(s))
        self.assertEqual(self.client.get(f"/invoice/{invoice.token(s)}/").status_code, 404)

    def test_platform_booking_invoiced_by_hand_goes_with_check_out_message(self):
        from urllib.parse import parse_qs, urlparse
        from core import invoice
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 3, status=Stay.Status.CHECKED_IN,
                 source="airbnb", source_ref="ABC123", amount_paid=Decimal(3000))
        self.client.post(f"/stays/{s.pk}/make-invoice/")
        self.assertTrue(invoice.has_invoice(s))
        inv = invoice.build(s)
        self.assertEqual(inv.parts[0].amount, s.total)  # the whole stay
        self.assertTrue(inv.parts[0].whole)  # "Booked via: Airbnb" on the PDF, all payments listed
        self.assertTrue(invoice.pdf(s).startswith(b"%PDF"))
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), "WhatsApp invoice")
        # Early check-out: the invoice follows the new dates, and the check-out message carries the link.
        self.client.post(f"/stays/{s.pk}/check-out/")
        s.refresh_from_db()
        self.assertEqual(invoice.number(s), f"{s.check_in:%y%m%d}-{T:%y%m%d}-101")
        text = parse_qs(urlparse(self.client.get(f"/stays/{s.pk}/whatsapp/?t=checkout")["Location"]).query)["text"][0]
        self.assertIn("invoice", text.lower())
        self.assertEqual(self.client.get(f"/invoice/{invoice.token(s)}/").status_code, 200)
        self.assertEqual(invoice.tidy(), 0)  # the nightly clean-up keeps it
        self.assertTrue(invoice.has_invoice(s))
        # Cancelled afterwards: nothing left to invoice, so it goes.
        self.client.post(f"/stays/{s.pk}/undo-check-out/")
        self.client.post(f"/stays/{s.pk}/undo-check-in/")
        self.client.post(f"/stays/{s.pk}/cancel/")
        s.refresh_from_db()
        if s.status == Stay.Status.CANCELLED:
            self.assertFalse(invoice.has_invoice(s))

    def test_platform_booking_extended_through_platform_has_no_invoice(self):
        from core import invoice
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 3, status=Stay.Status.CHECKED_IN,
                 source="airbnb")
        self.assertContains(self.client.get(f"/stays/{s.pk}/extend/"), "Through Airbnb")
        self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (s.check_out + timedelta(days=2)).isoformat(),
                                                    "amount": "3000", "via": "platform"})
        s.refresh_from_db()
        self.assertEqual(s.check_out, T + timedelta(days=3))
        self.assertFalse(invoice.eligible(s))

    def test_platform_booking_extended_with_us_invoices_only_our_nights(self):
        from core import invoice
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 3, status=Stay.Status.CHECKED_IN,
                 source="airbnb", source_ref="ABC123", amount_paid=Decimal(4500))  # paid on Airbnb
        old_out = s.check_out
        self.client.post(f"/stays/{s.pk}/extend/", {"new_check_out": (old_out + timedelta(days=2)).isoformat(),
                                                    "amount": "3000", "paid_now": "1000", "payment_mode": "cash",
                                                    "via": "direct"})
        s.refresh_from_db()
        self.assertEqual((s.direct_from, s.direct_amount, s.total_amount), (old_out, Decimal(3000), Decimal(7500)))
        services.add_payment(s, Decimal(2000), "upi")
        inv = invoice.build(s)
        self.assertEqual((inv.total, inv.paid, inv.balance), (Decimal(3000), Decimal(3000), Decimal(0)))
        self.assertEqual(len(inv.lines), 1)
        self.assertEqual(inv.lines[0].qty, "2 nights")
        self.assertEqual(sorted(p.amount for p in inv.payments), [Decimal(1000), Decimal(2000)])
        self.assertEqual(inv.number, f"{old_out:%y%m%d}-{s.check_out:%y%m%d}-101")
        self.assertTrue(invoice.pdf(s).startswith(b"%PDF"))
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), "Generate invoice")
        self.client.post(f"/stays/{s.pk}/make-invoice/")
        self.assertTrue(invoice.has_invoice(s))

    def test_platform_booking_direct_nights_entered_by_hand(self):
        from core import invoice
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -4, 6, source="booking_com")
        services.add_payment(s, Decimal(2000), "cash", day=T)
        s.direct_from, s.direct_amount = T, Decimal(3000)
        services.save_stay(s, action="update", summary="test")
        inv = invoice.build(s)
        self.assertEqual((inv.total, inv.paid, inv.lines[0].qty), (Decimal(3000), Decimal(2000), "2 nights"))

    def test_payment_opens_payment_done_message(self):
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -1, 2, status=Stay.Status.CHECKED_IN)
        self.assertIn("data-pay-wa", self.client.get(f"/stays/{s.pk}/").content.decode())
        r = self.client.post(f"/stays/{s.pk}/payment/", {"amount": "1200", "mode": "upi"},
                             HTTP_X_REQUESTED_WITH="fetch")
        t = MessageTemplate.objects.get(name="Payment done")
        self.assertEqual(r.json(), {"ok": True, "wa": f"/stays/{s.pk}/whatsapp/?t={t.pk}&amount=1200"})
        s.refresh_from_db()
        self.assertEqual(s.amount_paid, Decimal(1200))
        self.assertEqual(self.client.post(f"/stays/{s.pk}/payment/", {"amount": "0"},
                                          HTTP_X_REQUESTED_WITH="fetch").json()["ok"], False)

    def test_invoice_made_only_by_button_or_check_out(self):
        from unittest import mock
        from core import gdrive, invoice
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 2, status=Stay.Status.CHECKED_IN)
        calls = []

        def fake_save(data, name, key, number):
            calls.append((name, key))
            AppSetting.put(f"{key}_id", "file1")
            AppSetting.put(f"{key}_link", "https://drive.google.com/file/d/file1/view")
            return "https://drive.google.com/file/d/file1/view"

        with mock.patch.object(gdrive, "is_configured", return_value=True), \
                mock.patch.object(gdrive, "save_invoice", side_effect=fake_save), \
                mock.patch.object(gdrive, "delete_invoice") as deleted:
            self.client.post(f"/stays/{s.pk}/payment/", {"amount": "1000", "mode": "upi"})
            self.assertEqual(calls, [])
            self.client.post(f"/stays/{s.pk}/check-out/")
            self.assertEqual(calls, [(f"{invoice.number(s)} Ravi Kumar.pdf", f"invoice_{s.pk}")])
            self.client.post(f"/stays/{s.pk}/payment/", {"amount": "2000", "mode": "cash"})  # not regenerated
            self.assertEqual(len(calls), 1)
            self.client.post(f"/stays/{s.pk}/make-invoice/")  # Update invoice: same file
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][1], f"invoice_{s.pk}")
            self.assertContains(self.client.get(f"/stays/{s.pk}/"), "https://drive.google.com/file/d/file1/view")
            # Nightly: an invoice whose stay no longer qualifies (now marked Airbnb) is removed.
            self.assertEqual(invoice.tidy(), 0)
            Stay.objects.filter(pk=s.pk).update(source="airbnb")
            self.assertEqual(invoice.tidy(), 1)
            deleted.assert_called_once_with(f"invoice_{s.pk}")
            self.assertFalse(invoice.generated_at(s))


    def test_generate_invoice_keeps_one_file_per_number(self):
        import re
        from unittest import mock
        from core import gdrive, invoice

        class Call:
            def __init__(self, fn):
                self.fn = fn

            def execute(self):
                return self.fn()

        class FakeFiles:
            def __init__(self):
                self.files, self.n = {}, 0

            def _new(self, body):
                self.n += 1
                fid = f"f{self.n}"
                self.files[fid] = {"id": fid, "name": body["name"], "parents": body.get("parents", []), "trashed": False}
                return {"id": fid, "webViewLink": f"https://drive/{fid}"}

            def create(self, body, media_body=None, fields=None):
                return Call(lambda: self._new(body))

            def update(self, fileId, body=None, media_body=None, fields=None):
                def run():
                    self.files[fileId]["name"] = body["name"]
                    return {"id": fileId, "webViewLink": f"https://drive/{fileId}"}
                return Call(run)

            def get(self, fileId, fields=None):
                def run():
                    if fileId not in self.files:
                        raise KeyError(fileId)
                    return self.files[fileId]
                return Call(run)

            def delete(self, fileId):
                return Call(lambda: self.files.pop(fileId))

            def list(self, q, **kw):
                parent = re.search(r"'([^']+)' in parents", q).group(1)
                part = re.search(r"name contains '([^']+)'", q)
                return Call(lambda: {"files": [f for f in self.files.values() if parent in f["parents"]
                                               and (not part or part.group(1) in f["name"])]})

        drive = FakeFiles()
        svc = mock.Mock()
        svc.files.return_value = drive
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -2, 2, status=Stay.Status.CHECKED_IN)
        with mock.patch.object(gdrive, "is_configured", return_value=True), \
                mock.patch.object(gdrive, "_service", return_value=svc):
            page = self.client.get(f"/stays/{s.pk}/").content.decode()
            self.assertIn("Generate invoice", page)
            self.client.post(f"/stays/{s.pk}/make-invoice/")
            pdfs = lambda: sorted(f["name"] for f in drive.files.values() if f["name"].endswith(".pdf"))  # noqa: E731
            self.assertEqual(pdfs(), [f"{invoice.number(s)} Ravi Kumar.pdf"])
            self.assertIn("Update invoice", self.client.get(f"/stays/{s.pk}/").content.decode())
            # Generate again and after a payment: still one file, same number.
            self.client.post(f"/stays/{s.pk}/make-invoice/")
            self.client.post(f"/stays/{s.pk}/payment/", {"amount": "500", "mode": "cash"})
            self.assertEqual(len(pdfs()), 1)
            # Dates change → the number changes → the same file is renamed, not a second one.
            s.refresh_from_db()
            services.shorten(s, s.check_out - timedelta(days=1))
            self.client.post(f"/stays/{s.pk}/make-invoice/")
            self.assertEqual(pdfs(), [f"{invoice.number(s)} Ravi Kumar.pdf"])
            # A stray copy with the same number (e.g. from before a restore) is removed on the next save.
            folder = AppSetting.get("gdrive_invoices")
            drive._new({"name": f"{invoice.number(s)} old copy.pdf", "parents": [folder]})
            AppSetting.objects.filter(key=f"invoice_{s.pk}_id").delete()
            self.client.post(f"/stays/{s.pk}/make-invoice/")
            self.assertEqual(len(pdfs()), 1)
            # Cancelled: no invoice any more.
            self.client.post(f"/stays/{s.pk}/cancel/")
            self.assertEqual(pdfs(), [])


class SwapRoomTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("swap-admin", password="test-pass-123456"))

    def test_swap_rooms_of_two_bookings(self):
        a = stay(make_guest("A", "+91 90000 00001"), self.rooms["101"], 1, 3, amount_paid=Decimal(1000))
        b = stay(make_guest("B", "+91 90000 00002"), self.rooms["102"], 2, 2, status=Stay.Status.UPCOMING)
        page = self.client.get(f"/stays/{a.pk}/").content.decode()
        self.assertIn("Swap room", page)
        self.assertIn(f'<option value="{b.pk}">Room 102 — B', page)
        self.client.post(f"/stays/{a.pk}/swap/", {"other": b.pk})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.room.number, b.room.number), ("102", "101"))
        self.assertEqual((a.status, a.amount_paid, a.check_in), (Stay.Status.UPCOMING, Decimal(1000), T + timedelta(days=1)))
        self.assertTrue(AuditLog.objects.filter(stay=a, summary__contains="Rooms swapped with B").exists())

    def test_swap_refused_when_a_third_stay_is_in_the_way(self):
        a = stay(make_guest("A", "+91 90000 00001"), self.rooms["101"], 0, 4)
        b = stay(make_guest("B", "+91 90000 00002"), self.rooms["102"], 0, 1)
        stay(make_guest("C", "+91 90000 00003"), self.rooms["102"], 2, 2)  # 102 is taken for A's later nights
        r = self.client.post(f"/stays/{a.pk}/swap/", {"other": b.pk}, follow=True)
        self.assertContains(r, "already booked")
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.room.number, b.room.number), ("101", "102"))


class TypoCheckTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("chk-admin", password="test-pass-123456"))
        # Usual price: ₹1,500 a night (the stay() helper), five past stays.
        for i in range(5):
            stay(make_guest(f"P{i}", f"+91 90000 1000{i}"), self.rooms["103"], -40 - i * 3, 2,
                 status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(3000))

    def test_wrong_amounts_flagged_until_fixed_or_confirmed(self):
        from core import checks
        extra_zero = stay(make_guest("Zero", "+91 90000 00001"), self.rooms["101"], 1, 2)
        extra_zero.total_amount = Decimal(30000)  # ₹15,000 a night
        extra_zero.save()
        too_many = stay(make_guest("Nights", "+91 90000 00002"), self.rooms["102"], 1, 10)
        too_many.total_amount = Decimal(3000)  # ₹300 a night
        too_many.save()
        overpaid = stay(make_guest("Over", "+91 90000 00003"), self.rooms["201"], 1, 2, amount_paid=Decimal(30000))
        fine = stay(make_guest("Fine", "+91 90000 00004"), self.rooms["103"], 1, 2)
        reasons = {f.key.split("-")[0]: f.reason for f in checks.stay_flags(T)}
        self.assertIn("Extra zero", reasons[f"s{extra_zero.pk}"])
        self.assertIn("Wrong number of nights", reasons[f"s{too_many.pk}"])
        self.assertIn("more than the bill", reasons[f"s{overpaid.pk}"])
        self.assertNotIn(f"s{fine.pk}", reasons)

        page = self.client.get("/").content.decode()
        self.assertIn("Please double-check", page)
        self.assertIn(f"/stays/{extra_zero.pk}/edit/", page)
        # "Looks right" hides it — until the figures change.
        key = next(f.key for f in checks.stay_flags(T) if f.key.startswith(f"s{extra_zero.pk}-"))
        self.client.post("/checks/ok/", {"key": key, "next": "/"})
        self.assertNotIn(key, [f.key for f in checks.stay_flags(T)])
        extra_zero.total_amount = Decimal(40000)
        extra_zero.save()
        self.assertTrue(any(f.key.startswith(f"s{extra_zero.pk}-") for f in checks.stay_flags(T)))

    def test_warning_right_after_saving(self):
        r = self.client.post("/stays/new/", {
            "g-phone": "+91 90000 00009", "g-name": "Typo", "g-nationality": "Indian", "g-id_type": "aadhaar",
            "s-room": self.rooms["101"].pk, "s-check_in": T.isoformat(), "s-check_out": (T + timedelta(days=2)).isoformat(),
            "s-num_guests": 1, "s-total_amount": "30000", "s-amount_paid": "0", "s-source": "walk_in", "s-kind": "daily",
            "o-TOTAL_FORMS": "0", "o-INITIAL_FORMS": "0", "o-MIN_NUM_FORMS": "0", "o-MAX_NUM_FORMS": "9"}, follow=True)
        self.assertContains(r, "Please double-check: ₹15,000 a night")

    def test_host_required_for_online_bookings(self):
        data = {
            "g-phone": "+91 90000 00019", "g-name": "Online", "g-nationality": "Indian", "g-id_type": "aadhaar",
            "s-room": self.rooms["101"].pk, "s-check_in": T.isoformat(), "s-check_out": (T + timedelta(days=2)).isoformat(),
            "s-num_guests": 1, "s-total_amount": "3000", "s-amount_paid": "0", "s-source": "airbnb", "s-kind": "daily",
            "o-TOTAL_FORMS": "0", "o-INITIAL_FORMS": "0", "o-MIN_NUM_FORMS": "0", "o-MAX_NUM_FORMS": "9"}
        r = self.client.post("/stays/new/", data)
        self.assertContains(r, "required for online bookings")
        self.assertFalse(Stay.objects.filter(guest__name="Online").exists())
        self.client.post("/stays/new/", {**data, "s-host": services.host_names()[0]})
        self.assertTrue(Stay.objects.filter(guest__name="Online").exists())
        # Walk-in: still optional.
        self.client.post("/stays/new/", {**data, "g-phone": "+91 90000 00020", "g-name": "Walker",
                                         "s-source": "walk_in", "s-room": self.rooms["102"].pk})
        self.assertTrue(Stay.objects.filter(guest__name="Walker").exists())

    def test_expenses_not_checked(self):
        for i in range(5):
            Expense.objects.create(date=T - timedelta(days=60 + i), category="laundry", reason="Laundry", amount=Decimal(800))
        Expense.objects.create(date=T, category="laundry", reason="Laundry", amount=Decimal(80000))
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})
        self.assertNotContains(self.client.get("/expenses/"), "Please double-check")


class PaymentHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("pay-admin", password="test-pass-123456"))

    def test_every_way_of_paying_leaves_a_dated_entry(self):
        from core import revenue
        from core.models import Payment
        s = stay(make_guest(), self.rooms["101"], 0, 2, amount_paid=Decimal(1000), payment_mode="upi", host="Satish")
        self.assertEqual([(p.amount, p.mode, p.collected_by) for p in s.payments.all()], [(Decimal(1000), "upi", "Satish")])
        # Record payment (with who collected it and the day it was paid).
        self.client.post(f"/stays/{s.pk}/payment/", {"amount": "1500", "mode": "cash", "collected_by": "Shekhar",
                                                      "date": (T - timedelta(days=1)).isoformat()})
        # Extension with money paid; stay form edit lowering the paid amount (a correction).
        services.extend_in_place(Stay.objects.get(pk=s.pk), T + timedelta(days=3), Decimal(1500), Decimal(500), "upi")
        s = Stay.objects.get(pk=s.pk)
        s.amount_paid = Decimal(2800)
        services.save_stay(s, action="update", summary="test")
        rows = [(p.amount, p.date, p.collected_by, p.note) for p in Payment.objects.filter(stay=s)]
        self.assertEqual(rows[0], (Decimal(1500), T - timedelta(days=1), "Shekhar", ""))
        self.assertEqual([r[0] for r in rows], [Decimal(1500), Decimal(1000), Decimal(500), Decimal(-200)])
        self.assertEqual(sum(r[0] for r in rows), Decimal(2800))
        self.assertEqual(rows[-1][3], "Changed on the stay form")

        c = revenue.collected()
        self.assertEqual(c["total"], Decimal(2800))
        self.assertEqual(dict(c["by_person"])["Shekhar"], Decimal(1500))
        self.assertEqual(dict(c["by_mode"])["Cash"], Decimal(1500))

        # Delete a payment entered by mistake: the paid amount follows.
        p = Payment.objects.get(stay=s, amount=1500)
        self.client.post(f"/stays/{s.pk}/payments/{p.pk}/delete/")
        s.refresh_from_db()
        self.assertEqual(s.amount_paid, Decimal(1300))
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), "Payments")
        # Cancelled stays' payments don't count as collected (treated as refunded).
        services.cancel(s)
        self.assertEqual(revenue.collected()["total"], 0)


class RecurringExpenseTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client.force_login(staff("rec-admin", password="test-pass-123456"))
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})

    def test_added_once_a_month_on_its_day(self):
        from core.models import RecurringExpense
        self.client.post("/expenses/monthly/new/", {"reason": "Building rent", "category": "rent", "amount": "40000",
                                                     "day": "1", "payment_mode": "bank", "is_active": "on"})
        r = RecurringExpense.objects.get()
        self.assertEqual(Expense.objects.filter(recurring=r).count(), 1)  # day 1 has come: added for this month
        self.client.get("/expenses/")
        services.add_recurring_expenses()
        self.assertEqual(Expense.objects.filter(recurring=r).count(), 1)  # never twice in a month
        e = Expense.objects.get(recurring=r)
        self.assertEqual((e.date, e.amount, e.category), (T.replace(day=1), Decimal(40000), "rent"))
        # Next month, on its day.
        nxt = (T.replace(day=28) + timedelta(days=5)).replace(day=1)
        self.assertEqual(len(services.add_recurring_expenses(nxt)), 1)
        # A later day doesn't come before its day; stopped ones aren't added.
        late = RecurringExpense.objects.create(reason="Internet", category="internet", amount=Decimal(999), day=28)
        self.assertEqual(services.add_recurring_expenses(nxt.replace(day=27)), [])
        late.is_active = False
        late.save()
        self.assertEqual(services.add_recurring_expenses(nxt.replace(day=28)), [])
        self.assertContains(self.client.get("/expenses/"), "Monthly fixed expenses")
        self.assertContains(self.client.post("/expenses/monthly/new/", {"reason": "X", "category": "other",
                                                                        "amount": "10", "day": "31", "payment_mode": "cash"}),
                            "1 to 28")


class PhotoTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("photo-admin", password="test-pass-123456"))

    def _jpeg(self, size=(3000, 2000)):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, "white").save(buf, "PNG")
        return SimpleUploadedFile("id.png", buf.getvalue(), content_type="image/png")

    def _photo(self, colour="white"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (40, 30), colour).save(buf, "PNG")
        return SimpleUploadedFile("id.png", buf.getvalue(), content_type="image/png")

    def test_id_photos_saved_once_and_ticked_per_stay(self):
        from unittest import mock
        from PIL import Image
        from django.core.files.uploadedfile import SimpleUploadedFile
        g = make_guest(name="Ravi Kumar")
        s = stay(g, self.rooms["101"], 0, 2)
        StayGuest.objects.create(stay=s, position=2, name="Sita")
        ids = iter(f"drive-{i}" for i in range(1, 20))
        up_url = f"/stays/{s.pk}/id-photos/upload/"
        with mock.patch("core.gdrive.upload_photo", side_effect=lambda *a: next(ids)) as up, \
             mock.patch("core.gdrive.fetch", return_value=b"jpeg-bytes"), mock.patch("core.gdrive.delete_file") as rm:
            self.client.post(up_url, {"person": "Ravi Kumar", "photo": [self._jpeg(), self._photo("red")]})
            self.client.post(up_url, {"person": "Sita", "photo": [self._photo("blue")]})
            day = f"{s.check_in:%d-%m-%Y}"
            self.assertEqual([(p.person, p.name, p.file_id) for p in g.id_photos.all()], [
                ("Ravi Kumar", f"Ravi Kumar_{s.pk}_{day}.jpg", "drive-1"),
                ("Ravi Kumar", f"Ravi Kumar_{s.pk}_{day}_2.jpg", "drive-2"),
                ("Sita", f"Sita_{s.pk}_{day}.jpg", "drive-3"),
            ])
            self.assertEqual(s.id_photos.count(), 3)  # new photos are ticked for the stay they were added on
            sent = Image.open(io.BytesIO(up.call_args_list[0][0][0]))
            self.assertEqual((sent.format, max(sent.size)), ("JPEG", 1600))  # shrunk, re-encoded
            r = self.client.get(f"/id-photos/{g.id_photos.first().pk}/")
            self.assertEqual((r.content, r["Content-Type"]), (b"jpeg-bytes", "image/jpeg"))
            self.assertTrue(AuditLog.objects.filter(action="reveal_id", summary__contains="ID photo").exists())

            # Same photo sent again → not stored twice.
            r = self.client.post(up_url, {"person": "Ravi Kumar", "photo": [self._photo("red")]}, follow=True)
            self.assertContains(r, "not stored again")
            self.assertEqual((g.id_photos.count(), up.call_count), (3, 3))

            # The guest books again: earlier photos are offered to tick, none ticked yet.
            again = stay(g, self.rooms["101"], 10, 2)
            page = self.client.get(f"/stays/{again.pk}/")
            self.assertContains(page, "Saved earlier — tick to use")
            self.assertContains(page, "No ID photo for this stay yet")
            red, blue = g.id_photos.get(file_id="drive-2"), g.id_photos.get(file_id="drive-3")
            self.client.post(f"/stays/{again.pk}/id-photos/use/", {"photo": [red.pk, blue.pk]})
            self.assertEqual(set(again.id_photos.all()), {red, blue})
            self.assertEqual(StayIdPhoto.objects.get(stay=again, photo=red).added_by, "photo-admin")
            # Re-sending a saved photo on the new booking ticks it there, still without storing it again.
            self.client.post(f"/stays/{again.pk}/id-photos/upload/", {"person": "Ravi Kumar", "photo": [self._jpeg()]})
            self.assertEqual((again.id_photos.count(), g.id_photos.count(), up.call_count), (3, 3, 3))
            # A new photo (ID changed) on the new booking: stored, named after that booking, ticked there only.
            self.client.post(f"/stays/{again.pk}/id-photos/upload/", {"person": "Ravi Kumar", "photo": [self._photo("green")]})
            new = g.id_photos.last()
            self.assertEqual(new.name, f"Ravi Kumar_{again.pk}_{again.check_in:%d-%m-%Y}.jpg")
            self.assertEqual(list(new.stays.all()), [again])
            # Untick on an open stay: allowed, photo stays saved.
            self.client.post(f"/stays/{again.pk}/id-photos/{blue.pk}/unuse/")
            self.assertNotIn(blue, again.id_photos.all())
            self.assertTrue(g.id_photos.filter(pk=blue.pk).exists())
            # Someone else's photo can't be ticked.
            other = stay(make_guest(name="Other", phone="+91 90000 00000"), self.rooms["101"], 20, 2)
            self.client.post(f"/stays/{other.pk}/id-photos/use/", {"photo": [red.pk]})
            self.assertEqual(other.id_photos.count(), 0)

            # Checked out: the stay's photos are the record — can't be unticked or deleted.
            Stay.objects.filter(pk=s.pk).update(status=Stay.Status.CHECKED_OUT)
            self.client.post(f"/stays/{s.pk}/id-photos/{red.pk}/unuse/")
            self.assertIn(red, s.id_photos.all())
            r = self.client.post(f"/id-photos/{red.pk}/delete/", follow=True)
            self.assertContains(r, "can’t be deleted")
            rm.assert_not_called()
            # A photo not used on any checked-out stay can be deleted (Drive too).
            self.client.post(f"/id-photos/{new.pk}/delete/")
            rm.assert_called_once_with(new.file_id)
            self.assertFalse(g.id_photos.filter(pk=new.pk).exists())
            # Someone not on the stay → refused.
            self.client.post(up_url, {"person": "Stranger", "photo": [self._photo("black")]})
            self.assertFalse(g.id_photos.filter(person="Stranger").exists())
            self.assertContains(self.client.get(f"/guests/{g.pk}/"), f"#{again.pk}")
        # Not a photo → refused, nothing saved.
        with mock.patch("core.gdrive.upload_photo") as up:
            r = self.client.post(f"/stays/{again.pk}/id-photos/upload/",
                                 {"person": "Sita", "photo": SimpleUploadedFile("x.pdf", b"%PDF")}, follow=True)
            up.assert_not_called()
        self.assertContains(r, "isn’t a photo")

    def test_today_reminds_missing_id_photos_after_check_in(self):
        g = make_guest(name="Ravi Kumar")
        s = stay(g, self.rooms["101"], 0, 2, num_guests=2)
        StayGuest.objects.create(stay=s, position=2, name="Sita")
        self.assertNotContains(self.client.get("/"), "ID photos missing")  # not checked in yet
        Stay.objects.filter(pk=s.pk).update(status=Stay.Status.CHECKED_IN)
        self.assertContains(self.client.get("/"), "Ravi Kumar (101) · 0 of 2")
        front = IdPhoto.objects.create(guest=g, stay=s, person="Ravi Kumar", file_id="a", name="a.jpg")
        back = IdPhoto.objects.create(guest=g, stay=s, person="Ravi Kumar", file_id="b", name="b.jpg")
        StayIdPhoto.objects.bulk_create([StayIdPhoto(stay=s, photo=front), StayIdPhoto(stay=s, photo=back)])
        self.assertContains(self.client.get("/"), "Ravi Kumar (101) · 1 of 2")  # front + back is still one person
        StayIdPhoto.objects.create(stay=s, photo=IdPhoto.objects.create(guest=g, stay=s, person="Sita", file_id="c", name="c.jpg"))
        self.assertNotContains(self.client.get("/"), "ID photos missing")

    def test_unnamed_second_guest_gets_a_row_and_photos_can_change_person(self):
        g = make_guest(name="Kishore")
        s = stay(g, self.rooms["101"], 0, 3, num_guests=2)  # 2 guests, guest 2 not named
        Stay.objects.filter(pk=s.pk).update(status=Stay.Status.CHECKED_IN)
        photos = [IdPhoto.objects.create(guest=g, stay=s, person="Kishore", file_id=f, name=f"{f}.jpg") for f in "ab"]
        StayIdPhoto.objects.bulk_create([StayIdPhoto(stay=s, photo=p) for p in photos])
        self.assertContains(self.client.get("/"), "Kishore (101) · 1 of 2")
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertIn('<span class="photo-person">Guest 2</span>', page)
        self.assertNotIn("{#", page)
        self.assertNotIn("comment", page.split('id="id-photos"')[1][:300])
        self.client.post(f"/stays/{s.pk}/id-photos/{photos[1].pk}/person/", {"person": "Guest 2"})
        photos[1].refresh_from_db()
        self.assertEqual(photos[1].person, "Guest 2")
        self.assertNotContains(self.client.get("/"), "ID photos missing")
        # Not to someone off the stay, and not once it is a checked-out stay's record.
        self.client.post(f"/stays/{s.pk}/id-photos/{photos[1].pk}/person/", {"person": "Stranger"})
        Stay.objects.filter(pk=s.pk).update(status=Stay.Status.CHECKED_OUT)
        self.client.post(f"/stays/{s.pk}/id-photos/{photos[1].pk}/person/", {"person": "Kishore"})
        photos[1].refresh_from_db()
        self.assertEqual(photos[1].person, "Guest 2")

    def test_receipt_on_expense(self):
        from unittest import mock
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})
        with mock.patch("core.gdrive.upload_photo", return_value="drive-r1"):
            self.client.post("/expenses/new/", {"date": T.isoformat(), "category": "repairs", "reason": "AC repair",
                                                "amount": "2500", "payment_mode": "upi", "receipt": self._jpeg()})
        e = Expense.objects.get()
        self.assertEqual(e.receipt_id, "drive-r1")
        self.assertContains(self.client.get("/expenses/"), f"/expenses/{e.pk}/receipt/")
        # Drive not connected: the expense is still saved, with a message.
        r = self.client.post("/expenses/new/", {"date": T.isoformat(), "category": "repairs", "reason": "Fan",
                                                "amount": "500", "payment_mode": "cash", "receipt": self._jpeg()}, follow=True)
        self.assertContains(r, "isn’t connected yet")
        self.assertEqual(Expense.objects.get(reason="Fan").receipt_id, "")


class MonthlyReportTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("rep-admin", password="test-pass-123456"))
        self.client.post("/revenue/", {"password": "secret1", "confirm": "secret1"})

    def test_share_summary_and_save_report(self):
        from unittest import mock
        from urllib.parse import unquote
        from core import gdrive
        last = (T.replace(day=1) - timedelta(days=1)).replace(day=1)
        s = stay(make_guest(), self.rooms["101"], 0, 2, status=Stay.Status.CHECKED_OUT, amount_paid=Decimal(3000))
        s.check_in, s.check_out = last + timedelta(days=2), last + timedelta(days=4)
        s.save()
        r = self.client.get(f"/revenue/{last:%Y-%m}/share/")
        text = unquote(r["Location"].split("text=", 1)[1])
        self.assertTrue(r["Location"].startswith("https://web.whatsapp.com/send?text="))
        self.assertIn(f"{last:%B %Y}", text)
        self.assertIn("Received: ₹3,000", text)

        with mock.patch("core.gdrive._service"), mock.patch("core.gdrive._folders", return_value=("r", "b", "e")), \
             mock.patch("core.gdrive._folder", return_value="f"), mock.patch("core.gdrive._exists", return_value=False), \
             mock.patch("core.gdrive._upload", return_value={"id": "sheet1", "webViewLink": "https://docs.google.com/x"}) as up:
            self.assertEqual(gdrive.save_last_months_report_if_missing(T), "https://docs.google.com/x")
            self.assertEqual(gdrive.save_last_months_report_if_missing(T), "")  # only once
            self.assertEqual(up.call_count, 1)
        self.assertContains(self.client.get(f"/revenue/{last:%Y-%m}/"), "report in Google Drive")
        r = self.client.get(f"/revenue/{last:%Y-%m}/share/")
        self.assertIn("https%3A//docs.google.com/x", r["Location"])


class MaintenanceTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("mnt-admin", password="test-pass-123456"))

    def test_service_due_problem_open_and_cost_to_expenses(self):
        from core.models import MaintenanceLog
        room = self.rooms["101"]
        self.client.post(f"/rooms/room/{room.pk}/log/", {"kind": "service", "date": (T - timedelta(days=90)).isoformat(),
                                                         "what": "AC service", "cost": "800", "add_expense": "on",
                                                         "next_due": (T + timedelta(days=3)).isoformat()})
        self.client.post(f"/rooms/room/{room.pk}/log/", {"kind": "problem", "date": T.isoformat(), "what": "Geyser not heating"})
        self.assertEqual(Expense.objects.get().amount, Decimal(800))  # cost added to Expenses
        page = self.client.get("/").content.decode()
        self.assertIn("Room upkeep", page)
        self.assertIn("AC service", page)
        self.assertIn("Geyser not heating", page)
        problem = MaintenanceLog.objects.get(kind="problem")
        self.client.post(f"/rooms/log/{problem.pk}/done/", {"next": "/"})
        # A new service replaces the old "next due" reminder.
        self.client.post(f"/rooms/room/{room.pk}/log/", {"kind": "service", "date": T.isoformat(), "what": "AC service",
                                                         "next_due": (T + timedelta(days=90)).isoformat()})
        page = self.client.get("/").content.decode()
        self.assertNotIn("Room upkeep", page)
        self.assertContains(self.client.get(f"/rooms/room/{room.pk}/log/"), "Geyser not heating")


class LoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = get_user_model().objects.create_superuser("shekhar", password="admin-pass-12345", first_name="Shekhar")
        self.client.force_login(self.admin)

    def test_admin_adds_login_and_log_shows_who(self):
        from django.core.management import call_command
        r = self.client.post("/properties/users/", {"action": "add", "name": "Satish", "username": "Satish",
                                                   "password": "satish-pass-1", "confirm": "satish-pass-1"}, follow=True)
        self.assertContains(r, "Login created for Satish")
        satish = get_user_model().objects.get(username="satish")
        self.assertEqual(self.client.post("/properties/users/", {"action": "add", "name": "X", "username": "x",
                                                               "password": "short", "confirm": "short"}).status_code, 302)
        self.assertFalse(get_user_model().objects.filter(username="x").exists())

        other = self.client_class()
        self.assertTrue(other.login(username="satish", password="satish-pass-1"))
        # A new login opens nothing until the admin gives access.
        self.assertEqual(other.get("/stays/").status_code, 403)
        from cluster.models import Item, Membership
        bsr = Item.objects.get(is_default=True)
        self.client.post("/properties/users/", {"action": "access", "user": satish.pk, f"role-{bsr.pk}": "owner"})
        self.assertEqual(Membership.objects.get(user=satish).role, "owner")
        other.post("/settings/", {"action": "hosts", "hosts": "Shekhar, Mounika, Satish"})
        self.assertEqual(AuditLog.objects.filter(action="settings").latest("at").by, "Satish")
        self.assertContains(other.get("/settings/"), "<b>Satish:</b>")
        # Only the main admin manages logins.
        self.assertRedirects(other.get("/properties/users/"), "/properties/", fetch_redirect_response=False)
        # Own password change keeps you signed in.
        other.post("/settings/password/", {"current": "satish-pass-1", "password": "satish-pass-2", "confirm": "satish-pass-2"})
        self.assertEqual(other.get("/").status_code, 200)
        satish.refresh_from_db()
        self.assertTrue(satish.check_password("satish-pass-2"))
        # Switch off; deploys no longer switch other logins off.
        self.client.post("/properties/users/", {"action": "toggle", "user": satish.pk})
        satish.refresh_from_db()
        self.assertFalse(satish.is_active)
        self.client.post("/properties/users/", {"action": "toggle", "user": satish.pk})
        with override_settings():
            import os
            from unittest import mock
            with mock.patch.dict(os.environ, {"ADMIN_USERNAME": "shekhar", "ADMIN_PASSWORD": "admin-pass-12345"}):
                call_command("ensure_admin", stdout=io.StringIO())
        satish.refresh_from_db()
        self.assertTrue(satish.is_active)


class PaymentLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("pay-admin", password="test-pass-123456"))

    def _send(self, s, amount):
        from urllib.parse import parse_qs, urlparse
        r = self.client.post(f"/stays/{s.pk}/pay-link/", {"amount": amount})
        return parse_qs(urlparse(r["Location"]).query)["text"][0]

    @override_settings(UPI_ID="bsr@upi", UPI_NAME="BSR Corridors")
    def test_payment_link_pay_claim_verify(self):
        from urllib.parse import urlparse
        from core.models import PaymentRequest
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -1, 2, status=Stay.Status.CHECKED_IN)
        page = self.client.get(f"/stays/{s.pk}/").content.decode()
        self.assertIn("Send UPI payment link", page)
        self.assertIn('value="3000"', page)  # prefilled with the balance
        text = self._send(s, "2500")  # changed amount
        self.assertIn("₹2,500", text)
        self.assertTrue(text.endswith("- BSR Corridors"))
        link = next(w for w in text.split() if "/s/" in w)
        req = PaymentRequest.objects.get()
        self.assertEqual((req.amount, req.status), (Decimal(2500), "open"))
        # Guest's page: no login; UPI button with payee and amount; QR code.
        self.client.logout()
        path = self.client.get(urlparse(link).path)["Location"]  # short link → the signed pay page
        self.assertTrue(path.startswith("/pay/"))
        self.assertEqual(self.client.get("/s/zzzzzzz").status_code, 404)
        page = self.client.get(path).content.decode()
        self.assertIn("upi://pay?pa=bsr%40upi&amp;pn=BSR%20Corridors&amp;am=2500.00&amp;cu=INR", page)
        self.assertIn('src="data:image/png;base64,', page)
        self.assertIn("Save QR code", page)
        self.assertEqual(self.client.get(path[:-3] + "x1/").status_code, 404)
        # "I have paid" closes the link and offers the WhatsApp confirmation.
        self.client.post(path, {"paid": "1", "reference": "4123 4567 8901"})
        req.refresh_from_db()
        self.assertEqual((req.status, req.reference), ("claimed", "412345678901"))
        page = self.client.get(path).content.decode()
        self.assertNotIn("upi://pay", page)
        self.assertIn("https://wa.me/917013684602?text=", page)
        # Staff: shown to verify; recording it marks the link paid.
        self.client.force_login(get_user_model().objects.get(username="pay-admin"))
        self.assertContains(self.client.get("/"), "UPI payments to verify")
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), "Guest says they paid ₹2,500")
        self.client.post(f"/stays/{s.pk}/payment/", {"amount": "2500", "mode": "upi", "pay_request": req.pk,
                                                     "note": "UPI payment link"})
        req.refresh_from_db()
        s.refresh_from_db()
        self.assertEqual((req.status, s.amount_paid), ("paid", Decimal(2500)))
        self.assertNotContains(self.client.get("/"), "UPI payments to verify")

    @override_settings(UPI_ID="bsr@upi")
    def test_not_received_reopens_and_new_link_replaces_old(self):
        from core.models import PaymentRequest
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -1, 2, status=Stay.Status.CHECKED_IN)
        self._send(s, "1000")
        first = PaymentRequest.objects.get()
        first.status = "claimed"
        first.save()
        self.client.post(f"/stays/{s.pk}/pay-not-received/", {"pay_request": first.pk})
        first.refresh_from_db()
        self.assertEqual(first.status, "open")
        self._send(s, "1200")
        first.refresh_from_db()
        self.assertEqual(first.status, "replaced")
        self.client.logout()
        from core import payments
        self.assertContains(self.client.get(f"/pay/{payments.token(first)}/"), "Link expired")

    @override_settings(UPI_ID="")
    def test_payment_link_off_without_upi_id(self):
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -1, 2, status=Stay.Status.CHECKED_IN)
        self.assertContains(self.client.get(f"/stays/{s.pk}/"), "add <b>UPI_ID</b>")
        self.client.post(f"/stays/{s.pk}/pay-link/", {"amount": "100"})
        self.assertFalse(s.payment_requests.exists())

    def test_testing_page_creates_and_deletes_test_data(self):
        self.assertContains(self.client.get("/testing/"), "Create test stay")
        r = self.client.post("/testing/", {"phone": "+91 90000 99999", "amount": "1"})
        s = Stay.objects.get(room__number="TEST")
        self.assertRedirects(r, f"/stays/{s.pk}/", fetch_redirect_response=False)
        self.assertEqual((s.guest.name, s.status, s.total_amount), ("Test Guest", "checked_in", Decimal(1)))
        self.assertContains(self.client.get("/"), "Test data is present")
        services.add_payment(s, Decimal(1), "upi")
        self.client.post("/testing/", {"action": "delete"})
        self.assertFalse(Stay.objects.filter(room__number="TEST").exists())
        self.assertFalse(Room.objects.filter(number="TEST").exists())
        self.assertFalse(Guest.objects.filter(name="Test Guest").exists())
        # A real guest's number isn't taken over.
        make_guest("Real", "+91 90000 11111")
        self.client.post("/testing/", {"phone": "+91 90000 11111"})
        self.assertFalse(Stay.objects.filter(room__number="TEST").exists())


class ShortLinkTests(TestCase):
    @override_settings(SHORT_LINK_BASE="https://bsrcorridors.com")
    def test_short_link_uses_website_and_is_stable(self):
        from core.shortlinks import shorten
        a = shorten("https://bsr-corridors-admin.onrender.com/pay/12:abc/")
        self.assertRegex(a, r"^https://bsrcorridors\.com/s/[A-Za-z2-9]{7}$")
        self.assertEqual(shorten("http://other/pay/12:abc/"), a)  # same page → same short link
        self.assertNotEqual(shorten("https://x/pay/13:def/"), a)
        self.assertEqual(self.client.get("/s/" + a.rsplit("/", 1)[1])["Location"], "/pay/12:abc/")


class WhatsAppOpenTests(TestCase):
    def setUp(self):
        cache.clear()
        _, _, self.rooms = make_rooms()
        self.client.force_login(staff("wa-admin", password="test-pass-123456"))

    def test_phone_opens_app_computer_opens_web_and_script_gets_text(self):
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], -1, 2, status=Stay.Status.CHECKED_IN)
        t = MessageTemplate.objects.get(name="Location")
        r = self.client.get(f"/stays/{s.pk}/whatsapp/?t={t.pk}", HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 14) Mobile")
        self.assertTrue(r["Location"].startswith("whatsapp://send?phone=919876543210&text="))
        r = self.client.get(f"/stays/{s.pk}/whatsapp/?t={t.pk}", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0)")
        self.assertTrue(r["Location"].startswith("https://web.whatsapp.com/send?phone=919876543210&text="))
        d = self.client.get(f"/stays/{s.pk}/whatsapp/?t={t.pk}", HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0)",
                            HTTP_X_REQUESTED_WITH="fetch").json()
        self.assertTrue(d["url"].startswith("https://web.whatsapp.com/send?"))
        self.assertTrue(d["text"].startswith("Dear Ravi Kumar,"))
        self.assertTrue(d["text"].endswith("- BSR Corridors"))

    def test_caretaker_button_after_check_in(self):
        s = stay(make_guest(phone="98765 43210"), self.rooms["101"], 0, 2, status=Stay.Status.UPCOMING)
        self.client.post(f"/stays/{s.pk}/check-in/")
        self.assertNotContains(self.client.get(f"/stays/{s.pk}/?welcome=1"), "Send caretaker")
        t = MessageTemplate.objects.create(name="Caretaker contact", body="Dear {guest}, caretaker: Amirul.")
        page = self.client.get(f"/stays/{s.pk}/?welcome=1").content.decode()
        self.assertIn(f"/stays/{s.pk}/whatsapp/?t={t.pk}", page)
        self.assertIn("Send caretaker contact", page)

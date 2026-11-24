"""PG and co-living: floors → rooms → beds, residents and tenancies (requirements v2 §8).

These tables exist only in a PG / co-living item's own schema (never in BSR Corridors' tables).

Who is in which bed, and when, is kept as *bed spells*: one row per tenancy per bed, from the day they took the bed to
the day they left it. Shifting beds closes one spell and opens the next, so a bed's history and a resident's history
both stay complete. On Postgres two spells in the same bed can't overlap (one resident per bed at a time).
"""

import string
from datetime import date

from django.db import models
from django.db.models import Q

from core.fields import EncryptedTextField, mask


class Floor(models.Model):
    name = models.CharField(max_length=40, unique=True, help_text="e.g. Ground, 1st, 2nd")
    sort_order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers show first")

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class RoomType(models.Model):
    name = models.CharField(max_length=60, unique=True, help_text="e.g. 2-sharing AC")
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Room(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MAINTENANCE = "maintenance", "Under maintenance"

    number = models.CharField("Room number", max_length=20, unique=True)
    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name="rooms")
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms")
    is_ac = models.BooleanField("AC", default=False)
    attached_bath = models.BooleanField("Attached bathroom", default=True)
    balcony = models.BooleanField(default=False)
    # No rent here: each resident's rent is agreed with them at move-in (it differs bed to bed and person to person).
    deposit = models.DecimalField("Usual advance", max_digits=10, decimal_places=2, default=0,
                                  help_text="Refundable security deposit, suggested at move-in (can be changed there)")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["floor__sort_order", "number"]

    def __str__(self):
        return f"Room {self.number}"

    def next_label(self) -> str:
        """A, B, C … — the first letter no bed of this room uses (removed beds keep theirs, so they can come back)."""
        used = set(self.beds.values_list("label", flat=True))
        for n in range(1, 4):
            for letters in _labels(n):
                if letters not in used:
                    return letters
        raise ValueError("Too many beds")


def _labels(n):
    if n == 1:
        yield from string.ascii_uppercase
    else:
        for head in _labels(n - 1):
            for c in string.ascii_uppercase:
                yield head + c


class BedQuerySet(models.QuerySet):
    def in_use(self):
        return self.filter(removed_on__isnull=True)


class Bed(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MAINTENANCE = "maintenance", "Under maintenance"

    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="beds")
    label = models.CharField(max_length=6)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    notes = models.CharField(max_length=200, blank=True)
    added_on = models.DateField(default=date.today)
    # Removed beds are never deleted: they keep their history and can be restored.
    removed_on = models.DateField(null=True, blank=True)
    removed_by = models.CharField(max_length=150, blank=True)
    removed_reason = models.CharField(max_length=200, blank=True)

    objects = BedQuerySet.as_manager()

    class Meta:
        ordering = ["room__floor__sort_order", "room__number", "label"]
        constraints = [models.UniqueConstraint(fields=["room", "label"], name="pg_bed_label_unique")]

    def __str__(self):
        return f"{self.room.number}-{self.label}"

    @property
    def is_removed(self) -> bool:
        return self.removed_on is not None

    def spell_on(self, day: date):
        return self.spells.covering(day).select_related("tenancy__resident").first()

    def busy_from(self, day: date) -> bool:
        """Someone is in this bed on or after `day` (current or booked) — it can't be removed or given away."""
        return self.spells.live().filter(Q(end__isnull=True) | Q(end__gt=day)).exists()


class Resident(models.Model):
    class IdType(models.TextChoices):
        AADHAAR = "aadhaar", "Aadhaar"
        DRIVING_LICENCE = "dl", "Driving licence"
        PASSPORT = "passport", "Passport"
        VOTER_ID = "voter", "Voter ID"
        PAN = "pan", "PAN card"
        OTHER = "other", "Other"

    class Gender(models.TextChoices):
        FEMALE = "f", "Female"
        MALE = "m", "Male"
        OTHER = "o", "Other"

    class Occupation(models.TextChoices):
        STUDENT = "student", "Student"
        WORKING = "working", "Working"
        OTHER = "other", "Other"

    name = models.CharField(max_length=120)
    phone = models.CharField("Phone (WhatsApp)", max_length=20, help_text="With country code, e.g. +91 98765 43210")
    phone_key = models.CharField(max_length=15, unique=True, editable=False)  # last 10 digits: one resident per number
    address = models.TextField("Permanent address", blank=True)
    id_type = models.CharField("ID proof", max_length=20, choices=IdType.choices, default=IdType.AADHAAR)
    id_number = EncryptedTextField("ID number", blank=True)
    id_photo = models.CharField(max_length=100, blank=True, editable=False)  # Drive file id
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True)
    occupation = models.CharField("Student / working", max_length=10, choices=Occupation.choices, blank=True)
    organisation = models.CharField("College or company", max_length=120, blank=True)
    emergency_name = models.CharField("Emergency contact name", max_length=120, blank=True)
    emergency_relation = models.CharField("Relation", max_length=40, blank=True)
    emergency_phone = models.CharField("Emergency phone", max_length=20, blank=True)
    police_verified_on = models.DateField("Police verification done on", null=True, blank=True)
    vehicle = models.CharField("Vehicle number", max_length=20, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    @staticmethod
    def key_for(phone: str) -> str:
        return "".join(c for c in phone if c.isdigit())[-10:]

    def save(self, *args, **kwargs):
        self.phone_key = self.key_for(self.phone)
        super().save(*args, **kwargs)

    @property
    def id_masked(self) -> str:
        return mask(self.id_number)

    @property
    def phone_digits(self) -> str:
        return "".join(c for c in self.phone if c.isdigit())

    @property
    def ready_to_move_in(self) -> bool:
        """Permanent address and ID are required at move-in (§8.3)."""
        return bool(self.address.strip() and self.id_number)


class TenancyQuerySet(models.QuerySet):
    def current(self):
        return self.filter(status__in=[Tenancy.Status.STAYING, Tenancy.Status.NOTICE])

    def open(self):
        return self.filter(status__in=[Tenancy.Status.RESERVED, Tenancy.Status.STAYING, Tenancy.Status.NOTICE])


class Tenancy(models.Model):
    class Status(models.TextChoices):
        RESERVED = "reserved", "Reserved"
        STAYING = "staying", "Staying"
        NOTICE = "notice", "On notice"
        MOVED_OUT = "moved_out", "Moved out"
        CANCELLED = "cancelled", "Cancelled"

    class Cycle(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        WEEKLY = "weekly", "Weekly"
        DAILY = "daily", "Daily"

    class DueRule(models.TextChoices):
        MOVE_IN = "move_in", "Same date as move-in, every cycle"
        FIXED = "fixed", "A fixed date every month (first period pro-rated)"

    resident = models.ForeignKey(Resident, on_delete=models.PROTECT, related_name="tenancies")
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="tenancies", help_text="Current (or last) bed")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.STAYING)
    cycle = models.CharField("Billing cycle", max_length=8, choices=Cycle.choices, default=Cycle.MONTHLY)
    due_rule = models.CharField("Rent due", max_length=8, choices=DueRule.choices, default=DueRule.MOVE_IN)
    due_day = models.PositiveSmallIntegerField("Due date of the month", null=True, blank=True,
                                               help_text="1–28, for a fixed date")
    rent = models.DecimalField("Rent per cycle", max_digits=10, decimal_places=2,
                               help_text="As agreed with this resident; includes electricity and food")
    deposit = models.DecimalField("Advance / security deposit", max_digits=10, decimal_places=2, default=0)
    deposit_received = models.DecimalField("Advance received so far", max_digits=10, decimal_places=2, default=0,
                                           help_text="For a reservation: the token advance")
    move_in = models.DateField("Move-in date")
    notice_days = models.PositiveSmallIntegerField("Notice period (days)", default=30)
    notice_given_on = models.DateField(null=True, blank=True)
    last_day = models.DateField("Last day", null=True, blank=True, help_text="Set when notice is given")
    move_out = models.DateField("Moved out on", null=True, blank=True)
    agreement = models.CharField(max_length=100, blank=True, editable=False)  # Drive file id
    notes = models.TextField(blank=True)
    created_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Move-out settlement (§8.6): deposit − unpaid rent − deductions = refund
    unpaid_rent = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deductions = models.JSONField(default=list, blank=True)  # [{"reason": "Cleaning", "amount": "500"}]
    refund = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    refund_due_by = models.DateField(null=True, blank=True)
    refund_paid_on = models.DateField(null=True, blank=True)
    refund_mode = models.CharField(max_length=20, blank=True)
    settled_by = models.CharField(max_length=150, blank=True)

    objects = TenancyQuerySet.as_manager()

    class Meta:
        ordering = ["-move_in", "-pk"]
        constraints = [models.CheckConstraint(condition=Q(due_day__isnull=True) | Q(due_day__gte=1, due_day__lte=28),
                                              name="pg_tenancy_due_day_1_28")]

    def __str__(self):
        return f"{self.resident.name} in {self.bed}"

    @property
    def is_open(self) -> bool:
        return self.status in (self.Status.RESERVED, self.Status.STAYING, self.Status.NOTICE)

    @property
    def deductions_total(self):
        from decimal import Decimal
        return sum((Decimal(str(d.get("amount", 0))) for d in self.deductions), Decimal("0"))

    @property
    def refund_pending(self) -> bool:
        return self.status == self.Status.MOVED_OUT and self.refund > 0 and self.refund_paid_on is None


class BedSpellQuerySet(models.QuerySet):
    def live(self):
        """Spells of tenancies that weren't cancelled."""
        return self.exclude(tenancy__status=Tenancy.Status.CANCELLED)

    def covering(self, day: date):
        return self.live().filter(start__lte=day).filter(Q(end__isnull=True) | Q(end__gt=day))


class BedSpell(models.Model):
    """One tenancy in one bed, from `start` up to (not including) `end`; end empty = still there."""
    tenancy = models.ForeignKey(Tenancy, on_delete=models.CASCADE, related_name="spells")
    bed = models.ForeignKey(Bed, on_delete=models.PROTECT, related_name="spells")
    start = models.DateField()
    end = models.DateField(null=True, blank=True)

    objects = BedSpellQuerySet.as_manager()

    class Meta:
        ordering = ["start", "pk"]

    def __str__(self):
        return f"{self.bed}: {self.start} → {self.end or '…'}"


# ── Rent and money (phase 4) ──────────────────────────────────────────────────


class RentDue(models.Model):
    """One rent period, added on its due date (its first day). `full` is the period's rent at the time; `amount` is
    what is charged — less when the resident leaves part-way through (charged by the day). Later rent changes never
    alter periods already added."""
    tenancy = models.ForeignKey(Tenancy, on_delete=models.CASCADE, related_name="dues")
    start = models.DateField()
    end = models.DateField()  # the period's natural end (exclusive)
    full = models.DecimalField(max_digits=10, decimal_places=2)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start"]
        constraints = [models.UniqueConstraint(fields=["tenancy", "start"], name="pg_rentdue_one_per_period")]

    def __str__(self):
        return f"{self.tenancy}: {self.start} → {self.end} ₹{self.amount}"

    @property
    def part(self) -> bool:
        return self.amount != self.full


class RentPayment(models.Model):
    class Kind(models.TextChoices):
        RENT = "rent", "Rent"
        DEPOSIT = "deposit", "Advance / deposit"

    class Mode(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK = "bank", "Bank transfer"
        OTHER = "other", "Other"
        ADVANCE = "advance", "From the advance (move-out)"  # not money coming in: settled against the advance

    tenancy = models.ForeignKey(Tenancy, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.RENT)
    paid_on = models.DateField()
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.UPI)
    reference = models.CharField("UPI / bank reference", max_length=40, blank=True)
    note = models.CharField(max_length=200, blank=True)
    recorded_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-pk"]

    def __str__(self):
        return f"{self.paid_on:%d-%m-%Y} ₹{self.amount:,.0f} {self.get_kind_display()}"


class RentRequest(models.Model):
    """A UPI payment link sent to a resident (no gateway: money goes straight to the property's UPI ID).
    open → the resident pays and ticks "I've paid" (claimed) → staff check the UPI app and record it (paid), or mark
    it not received (open again). A newer link for the tenancy replaces older open ones."""

    class Status(models.TextChoices):
        OPEN = "open", "Waiting for payment"
        CLAIMED = "claimed", "Resident says paid — to verify"
        PAID = "paid", "Paid"
        REPLACED = "replaced", "Replaced by a newer link"

    tenancy = models.ForeignKey(Tenancy, on_delete=models.CASCADE, related_name="requests")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    reference = models.CharField("UPI reference (UTR)", max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class PgMessage(models.Model):
    """A ready-made WhatsApp message for residents; {placeholders} are filled in from the tenancy and the property."""

    class Use(models.TextChoices):
        WELCOME = "welcome", "Opens after Move in"
        RENT_DUE = "rent_due", "Rent due (button on Today and the tenancy)"
        OVERDUE = "overdue", "Rent overdue (button on Today and the tenancy)"
        PAY_LINK = "pay_link", "Carries the UPI payment link"
        RECEIPT = "receipt", "Payment received (with the receipt link)"
        NOTICE = "notice", "Notice noted"
        MOVE_OUT = "move_out", "Opens after Move out"

    name = models.CharField(max_length=60, help_text="Button label")
    use = models.CharField("Used for", max_length=10, choices=Use.choices, blank=True,
                           help_text="Each use has one message; empty: only a button on the tenancy page")
    body = models.TextField()
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "pk"]
        constraints = [models.UniqueConstraint(fields=["use"], condition=~Q(use=""), name="pg_message_one_per_use")]

    def __str__(self):
        return self.name

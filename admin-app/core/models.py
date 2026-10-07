from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .fields import EncryptedTextField, mask


class RoomType(models.Model):
    name = models.CharField(max_length=60, unique=True, help_text="e.g. Luxury AC Room")
    is_ac = models.BooleanField("Air-conditioned", default=True)
    bed_type = models.CharField(max_length=60, default="King-size bed")
    max_guests = models.PositiveSmallIntegerField(default=2)
    default_rate = models.DecimalField("Default nightly rate (₹)", max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Room(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        MAINTENANCE = "maintenance", "Under maintenance"

    number = models.CharField("Room number", max_length=20, unique=True)
    floor = models.CharField(max_length=20, blank=True)
    room_type = models.ForeignKey(RoomType, on_delete=models.PROTECT, related_name="rooms")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    sort_order = models.PositiveIntegerField(default=0, help_text="Lower numbers show first in the calendar.")
    is_active = models.BooleanField(default=True, help_text="Inactive rooms are hidden but keep their history.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["sort_order", "floor", "number"]

    def __str__(self):
        return f"Room {self.number}"


class Guest(models.Model):
    class IdType(models.TextChoices):
        AADHAAR = "aadhaar", "Aadhaar"
        DRIVING_LICENCE = "dl", "Driving licence"
        PASSPORT = "passport", "Passport"
        VOTER_ID = "voter", "Voter ID"
        PAN = "pan", "PAN card"
        OTHER = "other", "Other"

    name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, db_index=True, help_text="With country code, e.g. +91 98765 43210")
    address = models.TextField(help_text="As shown on the ID proof")
    nationality = models.CharField(max_length=60, default="Indian")
    id_type = models.CharField("ID proof", max_length=20, choices=IdType.choices, default=IdType.AADHAAR)
    id_number = EncryptedTextField("ID number")
    # Foreign nationals (Form C)
    passport_number = EncryptedTextField(blank=True)
    visa_number = models.CharField(max_length=40, blank=True)
    visa_type = models.CharField(max_length=40, blank=True)
    arrival_in_india = models.DateField("Arrival in India", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.phone})"

    @property
    def is_foreign(self) -> bool:
        return self.nationality.strip().lower() not in {"indian", "india", ""}

    @property
    def id_masked(self) -> str:
        return mask(self.id_number)

    @property
    def phone_digits(self) -> str:
        return "".join(c for c in self.phone if c.isdigit())


class StayQuerySet(models.QuerySet):
    def live(self):
        """Everything except cancelled stays."""
        return self.exclude(status=Stay.Status.CANCELLED)

    def overlapping(self, start, end):
        """Stays occupying at least one night in [start, end)."""
        return self.filter(check_in__lt=end, check_out__gt=start)


class Stay(models.Model):
    class Status(models.TextChoices):
        UPCOMING = "upcoming", "Upcoming"
        CHECKED_IN = "checked_in", "Checked in"
        CHECKED_OUT = "checked_out", "Checked out"
        CANCELLED = "cancelled", "Cancelled"

    class PaymentMode(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK = "bank", "Bank transfer"
        OTHER = "other", "Other"

    guest = models.ForeignKey(Guest, on_delete=models.PROTECT, related_name="stays")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="stays")
    check_in = models.DateField()
    check_out = models.DateField()
    num_guests = models.PositiveSmallIntegerField("Number of guests", default=1)
    nightly_rate = models.DecimalField("Nightly rate (₹)", max_digits=10, decimal_places=2, default=0)
    amount_paid = models.DecimalField("Amount paid (₹)", max_digits=10, decimal_places=2, default=0)
    payment_mode = models.CharField(max_length=20, choices=PaymentMode.choices, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPCOMING, db_index=True)
    form_c_filed = models.BooleanField("Form C filed", default=False)
    notes = models.TextField(blank=True)
    # Split stays (guest moved rooms during an extension) point at the first stay of the chain.
    linked_to = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="continuations")
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_out_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = StayQuerySet.as_manager()

    class Meta:
        ordering = ["-check_in"]
        indexes = [models.Index(fields=["room", "check_in", "check_out"])]
        constraints = [
            models.CheckConstraint(condition=Q(check_out__gt=models.F("check_in")), name="stay_checkout_after_checkin"),
        ]
        # Postgres also has an exclusion constraint (migration 0002) that makes overlapping stays in the
        # same room impossible, even if two saves happen at the same moment.

    def __str__(self):
        return f"{self.guest.name} · {self.room} · {self.check_in:%d-%m} → {self.check_out:%d-%m}"

    @property
    def nights(self) -> int:
        return (self.check_out - self.check_in).days

    @property
    def total(self) -> Decimal:
        return (self.nightly_rate or Decimal(0)) * self.nights

    @property
    def balance(self) -> Decimal:
        return self.total - (self.amount_paid or Decimal(0))

    @property
    def is_active(self) -> bool:
        return self.status in {self.Status.UPCOMING, self.Status.CHECKED_IN}

    @property
    def root(self) -> "Stay":
        return self.linked_to or self

    def chain(self) -> list["Stay"]:
        """This stay plus any linked continuations (room moves), in date order."""
        root = self.root
        return sorted([root, *root.continuations.all()], key=lambda s: s.check_in)

    @property
    def form_c_due(self) -> bool:
        return self.guest.is_foreign and not self.form_c_filed and self.status == self.Status.CHECKED_IN


class AuditLog(models.Model):
    at = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=40)
    summary = models.TextField()
    stay = models.ForeignKey(Stay, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.at:%d-%m-%Y %H:%M} {self.action}: {self.summary}"

    @classmethod
    def record(cls, action: str, summary: str, stay: Stay | None = None) -> "AuditLog":
        return cls.objects.create(action=action, summary=summary, stay=stay)


class BackupRun(models.Model):
    class Kind(models.TextChoices):
        BACKUP = "backup", "Nightly backup"
        EXPORT = "export", "Export to Drive"

    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.BACKUP)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    message = models.TextField(blank=True)
    link = models.URLField(blank=True)

    class Meta:
        ordering = ["-started_at"]


class AppSetting(models.Model):
    """Small key/value store (e.g. Google Drive folder IDs created by the app)."""

    key = models.CharField(max_length=80, unique=True)
    value = models.TextField(blank=True)

    @classmethod
    def get(cls, key: str, default: str = "") -> str:
        return cls.objects.filter(key=key).values_list("value", flat=True).first() or default

    @classmethod
    def put(cls, key: str, value: str) -> None:
        cls.objects.update_or_create(key=key, defaults={"value": value})

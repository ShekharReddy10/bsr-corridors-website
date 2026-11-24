"""The cluster (BSR Group of Hotels): its items (hotels, PGs, co-livings), who may open which, and each item's look.

These tables live in the main database next to BSR Corridors' tables (they only ADD tables; nothing of BSR Corridors
is moved). Each item's own data (rooms, guests, stays, …) lives in its own schema — or, for BSR Corridors, exactly
where it has always been. See router.py.
"""

from django.conf import settings
from django.db import models

from core.fields import EncryptedTextField

# Tested colour sets: (label, accent, accent-dark, sidebar). Readable white text on the sidebar and on accent buttons.
THEMES = {
    "classic": ("Classic gold", "#b88a44", "#9a7034", "#1a2328"),
    "teal": ("Teal", "#0f8a83", "#0b6b66", "#0f2a2b"),
    "indigo": ("Indigo", "#4f5bd5", "#3c46b0", "#1b1d3a"),
    "rose": ("Rose", "#c2416b", "#a03256", "#2d1520"),
    "forest": ("Forest", "#2f8a4a", "#236b39", "#13261a"),
    "plum": ("Plum", "#8a4bb0", "#6f3a90", "#22152d"),
    "ocean": ("Ocean", "#2c6db5", "#225792", "#122236"),
    "terracotta": ("Terracotta", "#c0602f", "#9d4d24", "#2b1a12"),
}


class Item(models.Model):
    class Kind(models.TextChoices):
        HOTEL = "hotel", "Hotel"
        PG = "pg", "PG"
        COLIVING = "coliving", "Co-living"

    class For(models.TextChoices):
        ANY = "", "—"
        MEN = "men", "Men"
        WOMEN = "women", "Women"
        COED = "coed", "Co-ed"

    code = models.SlugField("Short code", max_length=12, unique=True,
                            help_text="2–12 letters or numbers, e.g. bsrc. Used in addresses and file names.")
    name = models.CharField(max_length=120)
    kind = models.CharField("Type", max_length=10, choices=Kind.choices, default=Kind.HOTEL)
    # BSR Corridors: its data stays where it always was (the main database, at the site root). Exactly one.
    is_default = models.BooleanField(default=False, editable=False)
    schema = models.CharField(max_length=63, blank=True, editable=False,
                              help_text="Postgres schema holding this item's tables (empty for BSR Corridors).")
    db_url = EncryptedTextField("Own database (optional)", blank=True,
                                help_text="Empty: a separate section of the main database.")
    address = models.TextField(blank=True)
    map_link = models.URLField(blank=True)
    email = models.EmailField(blank=True)
    phones = models.CharField(max_length=120, blank=True)
    for_whom = models.CharField("For", max_length=6, choices=For.choices, blank=True)
    theme = models.CharField(max_length=20, choices=[(k, v[0]) for k, v in THEMES.items()], default="classic")
    managed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    whatsapp_phone = models.CharField("WhatsApp number", max_length=20, blank=True,
                                      help_text="Guests confirm payments to this number")
    check_in_time = models.CharField(max_length=40, blank=True)
    check_out_time = models.CharField(max_length=40, blank=True)
    notes = models.TextField(blank=True)
    # Photos, stored in the item's own Google Drive (file IDs).
    cover_id = models.CharField(max_length=100, blank=True, editable=False)
    logo_id = models.CharField(max_length=100, blank=True, editable=False)
    # Business details (all optional — S5).
    legal_name = models.CharField("Legal / business name", max_length=150, blank=True)
    gstin = models.CharField("GSTIN", max_length=15, blank=True)
    pan = models.CharField("PAN", max_length=10, blank=True)
    udyam = models.CharField("Udyam number", max_length=25, blank=True)
    invoice_footer = models.CharField("Invoice footer", max_length=200, blank=True)
    # Payments to this item (never to another item's account).
    upi_id = models.CharField("UPI ID", max_length=80, blank=True)
    upi_name = models.CharField("UPI name", max_length=80, blank=True, help_text="Payee name shown in the UPI app")
    short_link_base = models.URLField("Short link website", blank=True,
                                      help_text="Website that forwards /s/… links here; empty: this app's address")
    # Its own Google Drive (Connect Google Drive button).
    drive_token = EncryptedTextField(blank=True, editable=False)
    drive_email = models.EmailField("Google account", blank=True, editable=False)
    drive_folder = models.CharField("Drive folder", max_length=120, blank=True,
                                    help_text="Created in that Google Drive; empty: the item's name")
    # PG / co-living move-out (decision D10).
    refund_days = models.PositiveSmallIntegerField("Deposit refunded within (days)", default=7)
    deductions = models.TextField("Standard deductions", blank=True,
                                  help_text="One per line, e.g. “Cleaning: 500”")
    sort_order = models.PositiveIntegerField(default=0)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "cluster_items"
        ordering = ["sort_order", "name"]
        constraints = [models.UniqueConstraint(fields=["is_default"], condition=models.Q(is_default=True),
                                               name="cluster_one_default_item")]

    def __str__(self):
        return self.name

    @property
    def prefix(self) -> str:
        """Where its pages live: BSR Corridors at the root (unchanged), the others under /p/<code>/."""
        return "/" if self.is_default else f"/p/{self.code}/"

    @property
    def colors(self) -> tuple[str, str, str]:
        return THEMES.get(self.theme, THEMES["classic"])[1:]

    @property
    def alias(self) -> str:
        return "default" if self.is_default else f"item_{self.code}"


class Membership(models.Model):
    """Who may open which cluster item, with which role. The main admin (superuser) may open everything."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner / partner"
        MANAGER = "manager", "Manager"
        DESK = "desk", "Front desk / Host"
        ACCOUNTANT = "accountant", "Accountant"
        VIEWER = "viewer", "View only"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.DESK)
    money_access = models.BooleanField("Money access", default=False,
                                       help_text="Revenue, profit and settlements (still behind the money password)")
    see_full_ids = models.BooleanField("Can see full ID numbers", default=False)
    can_delete = models.BooleanField("Can delete", default=False)

    class Meta:
        db_table = "cluster_memberships"
        constraints = [models.UniqueConstraint(fields=["user", "item"], name="cluster_one_membership")]

    def __str__(self):
        return f"{self.user} @ {self.item}: {self.get_role_display()}"

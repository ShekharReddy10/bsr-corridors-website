"""Add / edit a cluster item (admin only)."""

import re

from django import forms
from django.contrib.auth import get_user_model

from .models import THEMES, Item

MONEY_MIN = 6

BASIC = ["name", "code", "kind", "address", "map_link", "email", "phones", "whatsapp_phone", "for_whom",
         "check_in_time", "check_out_time", "managed_by", "theme", "sort_order", "notes"]
BUSINESS = ["legal_name", "gstin", "pan", "udyam", "invoice_footer"]
PAYMENTS = ["upi_id", "upi_name", "short_link_base"]
MOVE_OUT = ["refund_days", "deductions"]
DRIVE = ["drive_folder"]


class ItemForm(forms.ModelForm):
    money_password = forms.CharField(label="Money password", required=False, widget=forms.PasswordInput(render_value=False),
                                     help_text=f"For this property's revenue and profit pages (at least {MONEY_MIN} characters)")
    money_confirm = forms.CharField(label="Repeat money password", required=False,
                                    widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = Item
        fields = BASIC + BUSINESS + PAYMENTS + MOVE_OUT + DRIVE
        widgets = {"address": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 2}),
                   "deductions": forms.Textarea(attrs={"rows": 3, "placeholder": "Cleaning: 500\nKey lost: 300"}),
                   "theme": forms.RadioSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.creating = self.instance.pk is None
        self.fields["email"].required = True  # decision: email required
        self.fields["managed_by"].required = True
        self.fields["managed_by"].queryset = get_user_model().objects.filter(is_active=True).order_by("username")
        self.fields["managed_by"].label_from_instance = lambda u: u.get_full_name() or u.username
        self.fields["theme"].choices = [(k, v[0]) for k, v in THEMES.items()]
        if self.creating:
            self.fields["money_password"].required = self.fields["money_confirm"].required = True
        else:
            # The type decides the tables and the code the addresses: fixed once created.
            for name in ("code", "kind"):
                self.fields[name].disabled = True
            self.fields["money_password"].label = "New money password"
            self.fields["money_password"].help_text = "Only to reset it (e.g. forgotten) — leave empty to keep it"
            if self.instance.is_default:
                self.fields["email"].required = False

    def clean_code(self):
        code = self.cleaned_data["code"].lower()
        if not re.fullmatch(r"[a-z][a-z0-9]{1,11}", code):
            raise forms.ValidationError("2–12 letters and numbers, starting with a letter (no spaces or dashes).")
        if code in {"cluster", "properties", "users", "static", "login", "logout", "new", "drive", "pg", "p", "s", "overview", "search", "activity", "archive"}:
            raise forms.ValidationError("That code is reserved — pick another.")
        return code

    def clean_gstin(self):
        g = self.cleaned_data["gstin"].strip().upper()
        if g and not re.fullmatch(r"\d{2}[A-Z0-9]{13}", g):
            raise forms.ValidationError("A GSTIN has 15 characters, e.g. 36ABCDE1234F1Z5.")
        return g

    def clean_pan(self):
        p = self.cleaned_data["pan"].strip().upper()
        if p and not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", p):
            raise forms.ValidationError("A PAN looks like ABCDE1234F.")
        return p

    def clean_upi_id(self):
        u = self.cleaned_data["upi_id"].strip()
        if u and not re.fullmatch(r"[\w.\-]{2,}@[a-zA-Z][\w]{1,}", u):
            raise forms.ValidationError("A UPI ID looks like name@bank, e.g. bsrpg@ybl.")
        return u

    def clean(self):
        data = super().clean()
        pw, confirm = data.get("money_password", ""), data.get("money_confirm", "")
        if pw or self.creating:
            if len(pw) < MONEY_MIN:
                self.add_error("money_password", f"Use at least {MONEY_MIN} characters.")
            elif pw != confirm:
                self.add_error("money_confirm", "The two passwords don’t match.")
        return data

    def sections(self):
        """Field groups for the page."""
        groups = [("Basic details", BASIC), ("Money password", ["money_password", "money_confirm"]),
                  ("Payments & links", PAYMENTS), ("Business details (all optional)", BUSINESS),
                  ("Google Drive", DRIVE), ("PG / co-living move-out", MOVE_OUT)]
        return [(title, [self[n] for n in names if n in self.fields and n != "theme"]) for title, names in groups]


class _PartnerFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [f.cleaned_data for f in self.forms if f.cleaned_data and not f.cleaned_data.get("DELETE")
                and f.cleaned_data.get("name")]
        names = [r["name"].strip().lower() for r in rows]
        if len(set(names)) != len(names):
            raise forms.ValidationError("Each partner only once.")
        shares = [r.get("share") for r in rows]
        given = [s for s in shares if s is not None]
        if given and len(given) != len(shares):
            raise forms.ValidationError("Give every partner a share, or leave all shares empty (equal shares).")
        if given and sum(given) != 100:
            raise forms.ValidationError(f"The shares add up to {sum(given):g}% — they must add up to 100%.")
        if any(s is not None and s <= 0 for s in shares):
            raise forms.ValidationError("Each share must be more than 0%.")


def partner_formset(*args, extra: int = 2, **kwargs):
    from django.forms import inlineformset_factory
    from .models import Partner
    FormSet = inlineformset_factory(
        Item, Partner, formset=_PartnerFormSet, fields=["name", "phone", "share", "pay_to"], extra=extra, can_delete=True,
        widgets={"name": forms.TextInput(attrs={"placeholder": "Name"}),
                 "phone": forms.TextInput(attrs={"placeholder": "Phone (optional)", "inputmode": "tel"}),
                 "share": forms.NumberInput(attrs={"placeholder": "%", "step": "0.01", "min": "0", "max": "100"}),
                 "pay_to": forms.TextInput(attrs={"placeholder": "UPI ID / bank (optional)"})})
    return FormSet(*args, prefix="partners", **kwargs)

from django import forms

from core.services import today

from .models import Bed, Floor, Resident, Room, RoomType, Tenancy

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class FloorForm(forms.ModelForm):
    class Meta:
        model = Floor
        fields = ["name", "sort_order"]


class RoomTypeForm(forms.ModelForm):
    class Meta:
        model = RoomType
        fields = ["name", "notes"]


class RoomForm(forms.ModelForm):
    beds = forms.IntegerField(label="Number of beds", min_value=1, max_value=20, initial=2,
                              help_text="Beds A, B, C … are made for you; add or remove beds later on the room page")

    class Meta:
        model = Room
        fields = ["number", "floor", "room_type", "beds", "is_ac", "attached_bath", "balcony", "rent_monthly",
                  "rent_weekly", "rent_daily", "deposit", "status", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            del self.fields["beds"]  # changed with Add bed / Remove bed


class BedForm(forms.ModelForm):
    class Meta:
        model = Bed
        fields = ["label", "rent_monthly", "rent_weekly", "rent_daily", "status", "notes"]

    def clean_label(self):
        label = self.cleaned_data["label"].strip().upper()
        if Bed.objects.filter(room=self.instance.room, label=label).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another bed in this room has that label.")
        return label


class ResidentForm(forms.ModelForm):
    class Meta:
        model = Resident
        fields = ["name", "phone", "address", "id_type", "id_number", "date_of_birth", "gender", "occupation",
                  "organisation", "emergency_name", "emergency_relation", "emergency_phone", "police_verified_on",
                  "vehicle", "notes"]
        widgets = {"address": forms.Textarea(attrs={"rows": 2}), "notes": forms.Textarea(attrs={"rows": 2}),
                   "date_of_birth": DATE, "police_verified_on": DATE,
                   "id_number": forms.TextInput(attrs={"autocomplete": "off"}),
                   "phone": forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The saved ID number is never put in the form (it would be readable in the page): it can be replaced, and
        # those allowed to see full IDs use "Show" (which is logged).
        if self.instance.pk and self.instance.id_number:
            self.initial["id_number"] = ""
            self.fields["id_number"].help_text = f"Saved: {self.instance.id_masked}. Leave empty to keep it."
        self._keep_id = self.instance.id_number if self.instance.pk else ""

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        key = Resident.key_for(phone)
        if len(key) < 10:
            raise forms.ValidationError("Enter the full mobile number (10 digits, with country code if not Indian).")
        other = Resident.objects.filter(phone_key=key).exclude(pk=self.instance.pk).first()
        if other:
            raise forms.ValidationError(f"{other.name} already has this number — open their page instead.")
        return phone

    def clean_id_number(self):
        value = self.cleaned_data["id_number"].strip()
        return value or self._keep_id


class TenancyForm(forms.ModelForm):
    """Move-in (or reservation, when the date is in the future)."""

    class Meta:
        model = Tenancy
        fields = ["move_in", "cycle", "due_rule", "due_day", "rent", "deposit", "deposit_received", "notice_days",
                  "notes"]
        widgets = {"move_in": DATE, "notes": forms.Textarea(attrs={"rows": 2})}
        help_texts = {"move_in": "A future date reserves the bed until then"}

    def __init__(self, *args, bed=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.bed = bed
        if not self.is_bound and not self.instance.pk:
            self.initial.setdefault("move_in", today())
            if bed is not None:
                self.initial.setdefault("rent", bed.rent_for("monthly"))
                self.initial.setdefault("deposit", bed.room.deposit)
        self.fields["rent"].widget.attrs.update({"data-rent-monthly": bed.rent_for("monthly") if bed else "",
                                                 "data-rent-weekly": bed.rent_for("weekly") if bed else "",
                                                 "data-rent-daily": bed.rent_for("daily") if bed else ""})

    def clean(self):
        data = super().clean()
        if data.get("due_rule") == Tenancy.DueRule.FIXED:
            if data.get("cycle") != Tenancy.Cycle.MONTHLY:
                self.add_error("due_rule", "A fixed date works only with monthly rent.")
            elif not data.get("due_day"):
                self.add_error("due_day", "Which date of the month? (1–28)")
            elif not 1 <= data["due_day"] <= 28:
                self.add_error("due_day", "Pick a date from 1 to 28 (every month has it).")
        else:
            data["due_day"] = None
        if data.get("rent") is not None and data["rent"] <= 0:
            self.add_error("rent", "Enter the rent.")
        if data.get("deposit_received") and data.get("deposit") is not None and data["deposit_received"] > data["deposit"]:
            self.add_error("deposit_received", "More than the advance agreed.")
        return data


class NoticeForm(forms.Form):
    given_on = forms.DateField(label="Notice given on", widget=DATE, initial=today)
    last_day = forms.DateField(label="Last day", widget=DATE, required=False,
                               help_text="Empty: the notice period from the date given")


class ShiftForm(forms.Form):
    bed = forms.ModelChoiceField(label="New bed", queryset=Bed.objects.none())
    day = forms.DateField(label="From", widget=DATE, initial=today)

    def __init__(self, *args, beds=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bed"].queryset = Bed.objects.filter(pk__in=[b.pk for b in beds]).select_related("room")
        self.fields["bed"].label_from_instance = lambda b: f"Room {b.room.number} · bed {b.label}"


class RefundForm(forms.Form):
    paid_on = forms.DateField(label="Paid on", widget=DATE, initial=today)
    mode = forms.ChoiceField(choices=[("UPI", "UPI"), ("Cash", "Cash"), ("Bank", "Bank transfer")])

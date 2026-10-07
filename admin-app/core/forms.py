from datetime import timedelta

from django import forms

from .models import Guest, Room, RoomType, Stay, StayGuest

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class GuestForm(forms.ModelForm):
    """Guest details. On edit, a blank ID number keeps the stored one."""

    guest_id = forms.IntegerField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Guest
        fields = [
            "phone", "name", "address", "nationality", "id_type", "id_number",
            "passport_number", "visa_number", "visa_type", "arrival_in_india",
        ]
        widgets = {
            "phone": forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off", "placeholder": "+91 98765 43210"}),
            "address": forms.Textarea(attrs={"rows": 2}),
            "nationality": forms.TextInput(attrs={"list": "nationalities"}),
            "id_number": forms.TextInput(attrs={"autocomplete": "off"}),
            "passport_number": forms.TextInput(attrs={"autocomplete": "off"}),
            "arrival_in_india": DATE,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["guest_id"].initial = self.instance.pk
            for name in ("id_number", "passport_number"):
                stored = getattr(self.instance, name)
                self.initial[name] = ""
                if stored:
                    self.fields[name].required = False
                    self.fields[name].widget.attrs["placeholder"] = f"Saved: ••••{stored[-4:]} — leave blank to keep"

    def clean_phone(self):
        phone = self.cleaned_data["phone"].strip()
        if sum(c.isdigit() for c in phone) < 10:
            raise forms.ValidationError("Enter a valid phone number (at least 10 digits).")
        other = Guest.objects.filter(phone_key=Guest.key_for(phone)).exclude(pk=self.instance.pk or 0).first()
        if other:
            raise forms.ValidationError(
                f"This number is already saved for {other.name}. Tap “Use saved details” to add the booking under them."
            )
        return phone

    def clean_id_number(self):
        value = self.cleaned_data.get("id_number", "").strip()
        if not value and self.instance.pk:
            return self.instance.id_number
        return clean_aadhaar(value, self.cleaned_data.get("id_type"))

    def clean_passport_number(self):
        value = self.cleaned_data.get("passport_number", "").strip()
        if not value and self.instance.pk:
            return self.instance.passport_number
        return value

    def clean(self):
        data = super().clean()
        nationality = (data.get("nationality") or "").strip().lower()
        if nationality and nationality not in {"indian", "india"}:
            for name in ("passport_number", "visa_number", "arrival_in_india"):
                if not data.get(name):
                    self.add_error(name, "Required for foreign nationals (Form C).")
        return data


class StayForm(forms.ModelForm):
    class Meta:
        model = Stay
        fields = ["source", "source_ref", "room", "check_in", "check_out", "num_guests",
                  "total_amount", "amount_paid", "payment_mode", "notes"]
        widgets = {
            "check_in": DATE,
            "check_out": DATE,
            "notes": forms.Textarea(attrs={"rows": 2}),
            "total_amount": forms.NumberInput(attrs={"inputmode": "decimal", "step": "1", "min": "0"}),
            "amount_paid": forms.NumberInput(attrs={"inputmode": "decimal", "step": "1"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rooms = Room.objects.filter(is_active=True).select_related("room_type")
        if self.instance.pk:
            rooms = rooms | Room.objects.filter(pk=self.instance.room_id)
        self.fields["room"].queryset = rooms.distinct()
        self.fields["room"].label_from_instance = lambda r: f"{r.number} — {r.room_type.name}" + (
            " (maintenance)" if r.status == Room.Status.MAINTENANCE else ""
        )
        # Expose each room's capacity to the page script.
        self.fields["room"].widget.attrs["data-rooms"] = ",".join(
            f"{r.pk}:{r.room_type.max_guests}" for r in self.fields["room"].queryset
        )
        self.fields["total_amount"].help_text = "Agreed price for the whole stay"

    def clean(self):
        data = super().clean()
        check_in, check_out, room = data.get("check_in"), data.get("check_out"), data.get("room")
        if check_in and check_out and check_out <= check_in:
            self.add_error("check_out", "Check-out must be at least one day after check-in.")
        if room and data.get("num_guests") and data["num_guests"] > room.room_type.max_guests:
            self.add_error("num_guests", f"{room.room_type.name} allows up to {room.room_type.max_guests} guests.")
        if room and room.status == Room.Status.MAINTENANCE and (not self.instance.pk or self.instance.room_id != room.pk):
            self.add_error("room", "This room is under maintenance.")
        return data


def clean_aadhaar(value: str, id_type: str) -> str:
    if value and id_type == Guest.IdType.AADHAAR:
        digits = value.replace(" ", "")
        if not (digits.isdigit() and len(digits) == 12):
            raise forms.ValidationError("Aadhaar number must be 12 digits.")
        return digits
    return value


class StayGuestForm(forms.ModelForm):
    """One additional guest (Guest 2, 3, …). Everything optional; blank ID on edit keeps the saved one."""

    class Meta:
        model = StayGuest
        fields = ["name", "phone", "id_type", "id_number"]
        widgets = {
            "id_number": forms.TextInput(attrs={"autocomplete": "off"}),
            "phone": forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off", "placeholder": "+91 98765 43210"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["id_number"].required = False
        self.fields["id_type"].required = False
        if self.instance.pk and self.instance.id_number:
            self.initial["id_number"] = ""
            self.fields["id_number"].widget.attrs["placeholder"] = f"Saved: ••••{self.instance.id_number[-4:]} — leave blank to keep"

    def clean_id_type(self):
        return self.cleaned_data.get("id_type") or Guest.IdType.AADHAAR

    def clean_phone(self):
        phone = (self.cleaned_data.get("phone") or "").strip()
        if phone and sum(c.isdigit() for c in phone) < 10:
            raise forms.ValidationError("Enter a valid phone number (at least 10 digits).")
        return phone

    def clean_id_number(self):
        value = self.cleaned_data.get("id_number", "").strip()
        if not value and self.instance.pk:
            return self.instance.id_number
        return clean_aadhaar(value, self.cleaned_data.get("id_type"))

    @property
    def is_blank(self) -> bool:
        data = getattr(self, "cleaned_data", {}) or {}
        return not any((data.get(k) or "").strip() for k in ("name", "phone", "id_number"))


MAX_OTHER_GUESTS = 9


def other_guests_formset(stay: Stay, data=None):
    """Rows for guests 2…N. Enough empty rows for the largest room type."""
    from django.db.models import Max
    from django.forms import inlineformset_factory

    largest = RoomType.objects.aggregate(m=Max("max_guests"))["m"] or 2
    rows = max(1, min(MAX_OTHER_GUESTS, largest - 1))
    existing = stay.other_guests.count() if stay.pk else 0
    FormSet = inlineformset_factory(Stay, StayGuest, form=StayGuestForm, extra=max(0, rows - existing),
                                    can_delete=False, max_num=MAX_OTHER_GUESTS)
    return FormSet(data, instance=stay, prefix="o")


class ExtendForm(forms.Form):
    new_check_out = forms.DateField(label="New check-out date", widget=DATE)
    amount = forms.DecimalField(label="Amount for the extra days (₹)", min_value=0, max_digits=10, decimal_places=2,
                                initial=0, widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "1"}),
                                help_text="Added to the stay’s total")
    paid_now = forms.DecimalField(label="Paid now (₹)", min_value=0, max_digits=10, decimal_places=2, required=False,
                                  widget=forms.NumberInput(attrs={"inputmode": "decimal", "step": "1"}))
    payment_mode = forms.ChoiceField(choices=[("", "—")] + list(Stay.PaymentMode.choices), required=False)

    def __init__(self, *args, stay: Stay, **kwargs):
        super().__init__(*args, **kwargs)
        self.stay = stay
        self.fields["new_check_out"].initial = stay.check_out + timedelta(days=1)
        self.fields["new_check_out"].widget.attrs["min"] = (stay.check_out + timedelta(days=1)).isoformat()

    def clean_new_check_out(self):
        value = self.cleaned_data["new_check_out"]
        if value <= self.stay.check_out:
            raise forms.ValidationError("Pick a date after the current check-out.")
        return value


class ShortenForm(forms.Form):
    new_check_out = forms.DateField(label="Leaving on", widget=DATE)


class PaymentForm(forms.Form):
    amount = forms.DecimalField(min_value=1, max_digits=10, decimal_places=2, widget=forms.NumberInput(attrs={"inputmode": "decimal"}))
    mode = forms.ChoiceField(choices=[("", "—")] + list(Stay.PaymentMode.choices), required=False)


class RoomTypeForm(forms.ModelForm):
    class Meta:
        model = RoomType
        fields = ["name", "is_ac", "bed_type", "max_guests", "is_active"]


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["number", "floor", "room_type", "status", "sort_order", "is_active", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["room_type"].queryset = RoomType.objects.filter(is_active=True) | RoomType.objects.filter(
            pk=self.instance.room_type_id or 0
        )


class DateRangeForm(forms.Form):
    start = forms.DateField(widget=DATE)
    end = forms.DateField(widget=DATE)
    include_full_ids = forms.BooleanField(required=False, label="Include full ID numbers")

    def clean(self):
        data = super().clean()
        if data.get("start") and data.get("end") and data["end"] < data["start"]:
            self.add_error("end", "End date must be on or after the start date.")
        return data

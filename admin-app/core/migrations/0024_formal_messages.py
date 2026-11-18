"""WhatsApp messages in a formal hotel style, and a "Payment done" message (thanks for {amount} received).

The hotel's sign-off "- BSR Corridors" is added to every message when it is sent, so the texts don't include it.
Ready-made messages are matched by name; the previous wording of each is kept in the log (Settings & log).
Messages added under other names are left as they are.
"""

from django.db import migrations

FORMAL = {
    "Welcome (check-in)": (
        "Dear {guest},\n\n"
        "Welcome to {hotel}. We are delighted to have you with us.\n\n"
        "Room: {room}\n"
        "Check-out: {check_out} by {check_out_time}\n\n"
        "Wi-Fi: {wifi_name}\n"
        "Password: {wifi_password}\n\n"
        "House rules:\n{house_rules}\n\n"
        "Should you require any assistance during your stay, please contact us at {contact}.\n\n"
        "We wish you a comfortable and pleasant stay."),
    "Check-out details": (
        "Dear {guest},\n\n"
        "This is a courteous reminder that your check-out from Room {room} is scheduled for {check_out} "
        "by {check_out_time}.\n\n"
        "Total amount: {total}\n"
        "Amount paid: {paid}\n"
        "Balance due: {balance}\n\n"
        "Kindly hand over the room key at the reception at the time of check-out.\n\n"
        "Thank you for choosing {hotel}."),
    "Booking confirmation": (
        "Dear {guest},\n\n"
        "We are pleased to confirm your reservation at {hotel}.\n\n"
        "Room: {room}\n"
        "Check-in: {check_in} ({check_in_time})\n"
        "Check-out: {check_out}\n"
        "Nights: {nights}\n"
        "Total amount: {total}\n"
        "Amount paid: {paid}\n\n"
        "Address: {location}\n\n"
        "For any assistance, please contact us at {contact}. We look forward to welcoming you."),
    "Payment reminder": (
        "Dear {guest},\n\n"
        "This is a gentle reminder that a balance of {balance} is pending for your stay in Room {room}.\n\n"
        "Kindly settle the amount at your earliest convenience by UPI or in cash at the reception.\n\n"
        "Thank you for your cooperation."),
    "Location": (
        "Dear {guest},\n\n"
        "Please find below the address and directions to {hotel}:\n{location}\n\n"
        "Should you need any help finding us, please call {contact}."),
    "Thank you": (
        "Dear {guest},\n\n"
        "Thank you for choosing {hotel}. We hope you had a pleasant stay with us.\n\n"
        "It would be our pleasure to welcome you again. For future reservations, please message us on this "
        "number."),
    "Departure (check-out)": (
        "Dear {guest},\n\n"
        "Thank you for staying with us at {hotel}. You have been checked out of Room {room}.\n\n"
        "Kindly ensure that you have collected all your belongings, and that the room key, TV and AC remotes and "
        "cupboard keys have been left in the room.\n\n"
        "We wish you a safe journey and look forward to hosting you again. For future reservations, please contact "
        "us at {contact}."),
    "Check-out reminder (tomorrow)": (
        "Dear {guest},\n\n"
        "This is a courteous reminder that your check-out from Room {room} is scheduled for tomorrow, {check_out}, "
        "by {check_out_time}.\n\n"
        "Balance due: {balance}\n\n"
        "Should you wish to extend your stay or require a late check-out, please reply to this message and we will "
        "be glad to assist."),
    "Review request": (
        "Dear {guest},\n\n"
        "Thank you for staying with us at {hotel}. We hope your stay was comfortable.\n\n"
        "We would be grateful if you could spare a moment to share your experience on Google:\n{review_link}\n\n"
        "We look forward to welcoming you again."),
}

PAYMENT_DONE = (
    "Dear {guest},\n\n"
    "We acknowledge with thanks the receipt of {amount} towards your stay in Room {room}.\n\n"
    "Total amount: {total}\n"
    "Amount paid: {paid}\n"
    "Balance due: {balance}\n\n"
    "Thank you for your payment.")


def forwards(apps, schema_editor):
    MessageTemplate = apps.get_model("core", "MessageTemplate")
    AuditLog = apps.get_model("core", "AuditLog")
    for t in MessageTemplate.objects.all():
        body = FORMAL.get(t.name.strip())
        if body and t.body != body:
            AuditLog.objects.create(action="whatsapp", by="update",
                                    summary=f"WhatsApp message “{t.name}” reworded formally. Previous text:\n{t.body}")
            t.body = body
            t.save(update_fields=["body"])
    if not MessageTemplate.objects.filter(name__iexact="Payment done").exists():
        last = MessageTemplate.objects.order_by("-sort_order").values_list("sort_order", flat=True).first() or 0
        MessageTemplate.objects.create(name="Payment done", body=PAYMENT_DONE, sort_order=last + 1)


class Migration(migrations.Migration):
    dependencies = [("core", "0023_stay_id_photo_links")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

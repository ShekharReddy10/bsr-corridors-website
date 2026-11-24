"""The default WhatsApp messages for residents (added to every new PG / co-living; the migration uses them too)."""

MESSAGES = [
    ("Welcome", "welcome",
     "Dear {first_name},\n\nWelcome to {property}! Your bed is {bed} in room {room}, from {move_in}.\n\n"
     "Rent: {rent} ({cycle}), due {due_rule}. It includes electricity and food.\n"
     "Advance received: {deposit}.\n\n"
     "For anything you need, please contact us at {contact}.\n\nWe hope you have a comfortable stay."),
    ("Rent due", "rent_due",
     "Dear {first_name},\n\nThis is a reminder that your rent of {amount_due} is due on {due_date}.\n\n"
     "You can pay by UPI here: {payment_link}\n\nThank you."),
    ("Rent overdue", "overdue",
     "Dear {first_name},\n\nYour rent of {amount_due} has been due since {due_since}. Please pay it at the earliest.\n\n"
     "You can pay by UPI here: {payment_link}\n\n"
     "If you have already paid, please share the payment details with us. Thank you."),
    ("Payment link", "pay_link",
     "Dear {first_name},\n\nPlease use this link to pay {amount} by UPI: {payment_link}\n\nThank you."),
    ("Payment received", "receipt",
     "Dear {first_name},\n\nWe have received {amount_paid} on {paid_on}. Thank you.\n\n"
     "Your receipt: {receipt_link}\n\nBalance still due: {amount_due}"),
    ("Notice noted", "notice",
     "Dear {first_name},\n\nWe have noted your notice. Your last day with us is {last_day}.\n\n"
     "Your advance of {deposit} will be settled when you move out, after any rent due and deductions."),
    ("Move-out", "move_out",
     "Dear {first_name},\n\nThank you for staying with us at {property}.\n\n{settlement}\n\nWe wish you all the best."),
    ("Complaint fixed", "fixed",
     "Dear {first_name},\n\nYour complaint ({issue}) raised on {raised_on} has been fixed.\n\n"
     "If anything is still not right, please let us know. Thank you."),
]


def ensure_messages(using=None) -> int:
    """Add any default message whose use has none yet (never overwrites edited ones). Returns how many were added."""
    from .models import PgMessage
    qs = PgMessage.objects.using(using) if using else PgMessage.objects
    n = 0
    for i, (name, use, body) in enumerate(MESSAGES):
        if not qs.filter(use=use).exists():
            qs.create(name=name, use=use, body=body, sort_order=i)
            n += 1
    return n

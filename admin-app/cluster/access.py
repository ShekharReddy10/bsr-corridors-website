"""Who may do what inside a cluster item: a role per item, plus three switches (money, full IDs, delete).

Checked once per request by the middleware, from the page's URL name — so every page is covered, including ones added
later (unlisted pages: anyone with access may look; changing anything needs "work").
"""

from .models import Membership

R = Membership.Role

# Capabilities: view · work (bookings, check-in/out, guests, messages) · pay (record payments) · manage (rooms,
# WhatsApp texts, backups, testing) · settings (hosts, partners, purge) · money · expenses · export · ids · delete
ROLE_CAPS = {
    R.OWNER: {"view", "work", "pay", "manage", "settings", "money", "expenses", "export", "ids", "delete"},
    R.MANAGER: {"view", "work", "pay", "manage", "expenses", "export"},
    R.DESK: {"view", "work", "pay"},
    R.ACCOUNTANT: {"view", "pay", "money", "expenses", "export"},
    R.VIEWER: {"view"},
}
ALL = set().union(*ROLE_CAPS.values())

_money = ["revenue", "revenue_month", "revenue_overall", "revenue_month_share", "revenue_month_report", "revenue_lock",
          "settle", "revenue_password"]
_expenses = ["expense_list", "expense_new", "expense_edit", "expense_receipt", "recurring_new", "recurring_edit"]
_manage = ["rooms", "roomtype_new", "roomtype_edit", "room_new", "room_edit", "whatsapp", "wa_template_new",
           "wa_template_edit", "backups"]
_deletes = {"payment_delete": "pay", "id_photo_delete": "work", "expense_delete": "expenses",
            "expense_receipt_delete": "expenses", "recurring_delete": "expenses", "settlement_delete": "money",
            "wa_template_delete": "manage", "room_delete": "manage", "maintenance_delete": "work"}

# url name → (needed to open, needed to send a form)
RULES: dict[str, tuple[set, set]] = {
    **{n: ({"money"}, {"money"}) for n in _money},
    **{n: ({"expenses"}, {"expenses"}) for n in _expenses},
    **{n: ({"view"}, {"manage"}) for n in _manage},
    **{n: ({"view"}, {cap, "delete"}) for n, cap in _deletes.items()},
    "export": ({"export"}, {"export"}),
    "testing": ({"manage"}, {"manage"}),
    "settings": ({"view"}, {"settings"}),
    "stay_reveal_id": ({"ids"}, {"ids"}),
}
_pg_manage = ["pg_setup", "pg_setup_new", "pg_setup_edit", "pg_bed_edit", "pg_bed_action", "pg_room"]
RULES.update({n: ({"view"}, {"manage"}) for n in _pg_manage})
RULES["pg_reveal_id"] = ({"ids"}, {"ids"})
PG_TENANCY_ACTIONS = {"refund": {"pay"}}
STAY_ACTIONS = {"payment": {"pay"}, "pay-not-received": {"pay"}, "delete": {"work", "delete"}}


def caps(user, membership: Membership | None) -> set:
    if user.is_superuser:
        return ALL
    if membership is None:
        return set()
    c = set(ROLE_CAPS.get(membership.role, set()))
    if membership.money_access:
        c.add("money")
    if membership.see_full_ids:
        c.add("ids")
    if membership.can_delete:
        c.add("delete")
    return c


def needed(request, url_name: str, kwargs: dict) -> set:
    post = request.method not in ("GET", "HEAD", "OPTIONS")
    if url_name == "stay_action" and post:
        return STAY_ACTIONS.get(kwargs.get("action", ""), {"work"})
    if url_name == "pg_tenancy_action" and post:
        return PG_TENANCY_ACTIONS.get(kwargs.get("action", ""), {"work"})
    if url_name == "pg_file" and kwargs.get("what") == "id":
        return {"ids"}
    if url_name == "export" and post and request.POST.get("include_full_ids"):
        return {"export", "ids"}
    get_caps, post_caps = RULES.get(url_name, ({"view"}, {"work"}))
    return post_caps if post else get_caps

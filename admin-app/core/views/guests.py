from django.core.paginator import Paginator
from django.db.models import Count, Max, Q, Sum
from django.shortcuts import get_object_or_404, render

from ..models import Guest, Stay


def guest_list(request):
    """Find a guest by phone (or name) to add a new booking under the same number."""
    q = request.GET.get("q", "").strip()
    guests = Guest.objects.annotate(stay_count=Count("stays"), last_stay=Max("stays__check_in"))
    if q:
        digits = "".join(c for c in q if c.isdigit())
        cond = Q(name__icontains=q)
        if len(digits) >= 3:
            cond |= Q(phone_key__contains=digits[-10:])
        guests = guests.filter(cond)
    page = Paginator(guests.order_by("-last_stay", "name"), 25).get_page(request.GET.get("page"))
    template = "partials/guest_rows.html" if request.headers.get("HX-Request") else "core/guest_list.html"
    return render(request, template, {"page": page, "q": q})


def guest_detail(request, pk):
    guest = get_object_or_404(Guest, pk=pk)
    stays = guest.stays.select_related("room").order_by("-check_in")
    live = stays.live()
    totals = live.aggregate(total=Sum("total_amount"), paid=Sum("amount_paid"))
    total, paid = totals["total"] or 0, totals["paid"] or 0
    return render(request, "core/guest_detail.html", {
        "guest": guest, "stays": stays, "total": total, "paid": paid, "balance": total - paid,
        "nights": sum(s.nights for s in live),
    })

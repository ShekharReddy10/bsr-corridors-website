from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import revenue
from ..forms import ExpenseForm
from ..models import AuditLog, Expense
from ..services import today
from .revenue import revenue_unlocked


@revenue_unlocked
def expense_list(request):
    t = today()
    periods = revenue.periods(t)
    period = next((p for p in periods if p.key == request.GET.get("period", "month")), periods[1])
    expenses = Expense.objects.select_related("room")
    if period.start:
        expenses = expenses.filter(date__gte=period.start, date__lt=period.end)
    category = request.GET.get("category", "")
    if category in Expense.Category.values:
        expenses = expenses.filter(category=category)
    q = request.GET.get("q", "").strip()
    if q:
        expenses = expenses.filter(Q(reason__icontains=q) | Q(paid_to__icontains=q) | Q(reference__icontains=q))

    total = expenses.aggregate(s=Sum("amount"))["s"] or Decimal(0)
    labels = dict(Expense.Category.choices)
    by_category = list(expenses.values("category").annotate(total=Sum("amount"), n=Count("pk")).order_by("-total"))
    top = by_category[0]["total"] if by_category else 1
    for row in by_category:
        row["label"], row["pct"] = labels[row["category"]], float(row["total"] / top * 100)
    modes = dict(Expense._meta.get_field("payment_mode").choices)
    by_mode = [{"label": modes.get(r["payment_mode"], "—"), "total": r["total"]}
               for r in expenses.values("payment_mode").annotate(total=Sum("amount")).order_by("-total")]

    page = Paginator(expenses.order_by("-date", "-pk"), 50).get_page(request.GET.get("page"))
    return render(request, "core/expense_list.html", {
        "page": page, "total": total, "by_category": by_category, "by_mode": by_mode,
        "periods": periods, "period": period, "category": category, "q": q,
        "categories": Expense.Category.choices,
    })


@revenue_unlocked
def expense_form(request, pk=None):
    obj = get_object_or_404(Expense, pk=pk) if pk else None
    form = ExpenseForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        AuditLog.record("expense", f"{'Updated' if obj else 'Added'} expense: {saved} — {saved.reason}")
        messages.success(request, f"Expense of ₹{saved.amount:,.0f} saved.")
        if "add_another" in request.POST:
            return redirect("expense_new")
        return redirect("expense_list")
    return render(request, "core/expense_form.html", {"form": form, "expense": obj})


@revenue_unlocked
@require_POST
def expense_delete(request, pk):
    obj = get_object_or_404(Expense, pk=pk)
    AuditLog.record("expense", f"Deleted expense: {obj} — {obj.reason}")
    obj.delete()
    messages.success(request, "Expense deleted.")
    return redirect("expense_list")

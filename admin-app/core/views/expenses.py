from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .. import checks, gdrive, revenue, services
from ..forms import ExpenseForm, RecurringExpenseForm
from ..models import AuditLog, Expense, RecurringExpense
from ..services import today
from . import photos
from .revenue import revenue_unlocked


@revenue_unlocked
def expense_list(request):
    for e in services.add_recurring_expenses():
        messages.info(request, f"Added this month’s fixed expense: {e.reason} ₹{e.amount:,.0f}.")
    # One month at a time (?month=2026-10, default this month), or ?period=all / week / year.
    this_month = today().replace(day=1)
    periods = {p.key: p for p in revenue.periods(today())}
    period = periods.get(request.GET.get("period", ""))
    if period is None:
        try:
            period = revenue.month_period(datetime.strptime(request.GET.get("month", ""), "%Y-%m").date())
        except ValueError:
            period = revenue.month_period(this_month)
    first = Expense.objects.order_by("date").values_list("date", flat=True).first()
    first = min(first.replace(day=1) if first else this_month, period.start or this_month)
    months, m = [], this_month
    while m >= first:
        months.append((f"{m:%b %Y}", f"?month={m:%Y-%m}", m == period.start and period.end == revenue._add_months(m, 1)))
        m = revenue._add_months(m, -1)
    months.append(("All time", "?period=all", period.key == "all"))
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
        "month_links": months, "period": period, "category": category, "q": q,
        "flags": checks.expense_flags(today()),
        "recurring": RecurringExpense.objects.all(),
        "categories": Expense.Category.choices,
    })


@revenue_unlocked
def expense_form(request, pk=None):
    obj = get_object_or_404(Expense, pk=pk) if pk else None
    form = ExpenseForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        if upload := request.FILES.get("receipt"):
            if file_id := photos.store(request, upload, "receipts", f"{saved.date:%Y-%m-%d} {saved.reason[:60]} — {saved.pk}.jpg"):
                if saved.receipt_id:
                    gdrive.delete_file(saved.receipt_id)
                saved.receipt_id = file_id
                saved.save(update_fields=["receipt_id"])
        AuditLog.record("expense", f"{'Updated' if obj else 'Added'} expense: {saved} — {saved.reason}")
        messages.success(request, f"Expense of ₹{saved.amount:,.0f} saved.")
        for reason in checks.check_expense(saved):
            messages.warning(request, f"Please double-check: {reason}")
        if "add_another" in request.POST:
            return redirect("expense_new")
        return redirect("expense_list")
    return render(request, "core/expense_form.html", {"form": form, "expense": obj})


@revenue_unlocked
@require_POST
def expense_delete(request, pk):
    obj = get_object_or_404(Expense, pk=pk)
    AuditLog.record("expense", f"Deleted expense: {obj} — {obj.reason}")
    if obj.receipt_id:
        gdrive.delete_file(obj.receipt_id)
    obj.delete()
    messages.success(request, "Expense deleted.")
    return redirect("expense_list")


@revenue_unlocked
def recurring_form(request, pk=None):
    obj = get_object_or_404(RecurringExpense, pk=pk) if pk else None
    form = RecurringExpenseForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        AuditLog.record("expense", f"{'Updated' if obj else 'Added'} monthly fixed expense: {saved}")
        messages.success(request, f"Monthly expense “{saved.reason}” saved — it is added on day {saved.day} of each month.")
        for e in services.add_recurring_expenses():
            messages.info(request, f"Added this month’s: {e.reason} ₹{e.amount:,.0f}.")
        return redirect("/expenses/#recurring")
    return render(request, "core/simple_form.html", {
        "form": form, "title": f"{'Edit' if obj else 'Add'} monthly fixed expense", "back": "expense_list",
        "delete_url": f"/expenses/monthly/{obj.pk}/delete/" if obj else "",
    })


@revenue_unlocked
@require_POST
def recurring_delete(request, pk):
    obj = get_object_or_404(RecurringExpense, pk=pk)
    AuditLog.record("expense", f"Deleted monthly fixed expense: {obj} (expenses already added are kept)")
    obj.delete()
    messages.success(request, "Monthly expense removed. Expenses already added are kept.")
    return redirect("/expenses/#recurring")

"""Invoice PDFs: for staff from the stay page, and for the guest through the signed link in the check-out message."""

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404

from .. import invoice
from ..models import AuditLog, Stay


def _pdf_response(stay: Stay, download: bool) -> HttpResponse:
    response = HttpResponse(invoice.pdf(stay), content_type="application/pdf")
    name = f"Invoice {invoice.number(stay)}.pdf"
    response["Content-Disposition"] = f'{"attachment" if download else "inline"}; filename="{name}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def stay_invoice(request, pk):
    stay = get_object_or_404(Stay.objects.select_related("guest", "room", "room__room_type"), pk=pk)
    if not invoice.has_invoice(stay):  # only once generated, and only stays that have one
        raise Http404
    return _pdf_response(stay, request.GET.get("download") == "1")


def short_link(request, code):
    """Public: /s/<code> → the page it stands for (the page itself checks its own link)."""
    from django.shortcuts import redirect
    from ..models import ShortLink
    link = ShortLink.objects.filter(code=code).first()
    if link is None:
        raise Http404
    return redirect(link.path)


def guest_invoice(request, token):
    """Public: the guest's own invoice (no login). A wrong or altered link is a plain 404."""
    stay = invoice.stay_for_token(token)
    if stay is None or not invoice.has_invoice(stay):
        raise Http404
    AuditLog.record("invoice", f"Guest opened invoice {invoice.number(stay)}", stay)
    return _pdf_response(stay, request.GET.get("download") == "1")

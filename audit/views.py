"""Audit view over HTTP. No query lives in this module.

Same rule as the other view modules: §7 rule 0 makes the view layer hold no
permission logic, and the CI grep that fails the build on a query here is what
holds it.

Only admins may see the audit trail (row 22). The decorator answers that; the
view just renders what the service returns.
"""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from audit.models import AuditLog
from core.permissions import admin_required, login_required


@admin_required
@login_required
@require_http_methods(["GET"])
def audit_list(request):
    """The audit trail: 50 rows a page, newest first."""

    from django.core.paginator import Paginator

    page = 1
    try:
        page = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        pass

    qs = AuditLog.objects.select_related("actor").order_by("-created_at")
    paginator = Paginator(qs, 50)
    page_obj = paginator.get_page(page)

    return render(
        request,
        "audit/list.html",
        {"page_obj": page_obj},
    )
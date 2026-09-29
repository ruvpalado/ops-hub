from django.http import HttpResponse


def healthz(request):
    """Liveness probe. No database touch: a hung DB must not look like a down app.

    The scheduler and the load balancer both probe this; they want to know whether
    this process can serve a request, and the process can even when Postgres is
    mid-failover. Data-path health is the sync commands' job to report.
    """
    return HttpResponse("ok", content_type="text/plain", status=200)
from django.http import HttpResponse, HttpResponseRedirect

from accounts.views import SESSION_USER_KEY


def healthz(request):
    """Liveness probe. No database touch: a hung DB must not look like a down app.

    The scheduler and the load balancer both probe this; they want to know whether
    this process can serve a request, and the process can even when Postgres is
    mid-failover. Data-path health is the sync commands' job to report.
    """
    return HttpResponse("ok", content_type="text/plain", status=200)


def home(request):
    """Landing gate: signed-in users stay, everyone else is sent to Google.

    The signed-in body is a placeholder until the dashboard step lands; what
    matters here is that a fresh visitor has one obvious next action instead of
    a 404.
    """
    if not request.session.get(SESSION_USER_KEY):
        return HttpResponseRedirect("/auth/google")
    return HttpResponse("signed in", content_type="text/plain", status=200)
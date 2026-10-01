# Ops Hub process definitions for an external scheduler (Heroku, Fly, etc.)

# The web process serves HTTP. The scheduler runs on a separate clock process
# so a hung sync never blocks a page render (§4.1).
web: python manage.py migrate && gunicorn opshub.wsgi --workers 4 --timeout 120 --bind 0.0.0.0:$PORT

# The scheduler process wakes every 5 minutes and runs the two sync commands
# for all due sources. Each command handles its own locking and idempotency.
scheduler: while true; do python manage.py sync_gmail --all; python manage.py sync_calendar --all; sleep 300; done

# One-off tasks that should never run in the web process:
# - seed_demo (development only)
# - set_token_key (deployment bootstrap)
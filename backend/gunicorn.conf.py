import os

from backend.config import CONFIG, validate_bind_security

validate_bind_security()
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("PAPER_GUNICORN_THREADS", "8"))
if threads <= 1:
    raise ValueError("PAPER_GUNICORN_THREADS must be greater than 1")
timeout = 0
bind = CONFIG["server"].get("bind") or f"{CONFIG['server']['host']}:{CONFIG['server']['port']}"

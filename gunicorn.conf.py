# gunicorn.conf.py  —  Production server configuration
# ─────────────────────────────────────────────────────
# Run:  gunicorn -c gunicorn.conf.py wsgi:application

import multiprocessing

# ── Binding ───────────────────────────────────────────────────────────────
bind = "0.0.0.0:5000"

# ── Workers ───────────────────────────────────────────────────────────────
# Thread-based workers are required for SSE (streaming responses).
# Gevent/eventlet would also work but require extra deps.
worker_class = "gthread"

# 2-4 workers is optimal for this workload (CPU-light, I/O-heavy via Gemini).
workers = min(4, multiprocessing.cpu_count() * 2)

# Threads per worker — allows multiple SSE clients per worker.
threads = 4

# ── Timeouts ──────────────────────────────────────────────────────────────
# Bulk jobs can take many minutes; keep-alive must outlast the longest job.
timeout = 600          # 10 minutes — covers even large bulk batches
keepalive = 5
graceful_timeout = 30

# ── Logging ───────────────────────────────────────────────────────────────
accesslog = "logs/gunicorn_access.log"
errorlog  = "logs/gunicorn_error.log"
loglevel  = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Performance ───────────────────────────────────────────────────────────
# Prevent worker memory leaks by recycling after N requests.
max_requests = 500
max_requests_jitter = 50

# Preload the app once and fork — saves memory and speeds up startup.
preload_app = True

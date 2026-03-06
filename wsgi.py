"""
ASGI / WSGI entry-point for production servers.

Usage (Windows — uvicorn):
    uvicorn wsgi:application --host 0.0.0.0 --port 5000 --workers 4

Usage (Linux — gunicorn):
    gunicorn -c gunicorn.conf.py wsgi:application
"""

from app import app

# Uvicorn needs an ASGI app; Flask is WSGI.
# asgiref.wsgi.WsgiToAsgi wraps it so both servers work from the same file.
try:
    from asgiref.wsgi import WsgiToAsgi
    application = WsgiToAsgi(app)
except ImportError:
    # Fallback: expose raw WSGI app (for gunicorn / waitress)
    application = app

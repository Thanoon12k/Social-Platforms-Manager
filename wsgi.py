"""WSGI entry (PythonAnywhere, gunicorn: `gunicorn wsgi:application`)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app as application  # noqa: E402,F401

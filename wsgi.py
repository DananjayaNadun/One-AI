"""WSGI entry point for gunicorn and PythonAnywhere.

PythonAnywhere: in the Web tab, point the WSGI configuration file at this
module and expose `application`.
"""
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import app as application  # noqa: E402

if __name__ == "__main__":
    application.run()

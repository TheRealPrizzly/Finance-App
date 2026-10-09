"""WSGI entry point for production servers (PythonAnywhere, gunicorn, ...)."""
from app import create_app

application = create_app()

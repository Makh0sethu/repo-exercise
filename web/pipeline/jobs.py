"""Start a run in the background so the web request returns immediately.
v1 uses a plain thread; a real job queue (and a worker with its own Docker
access) replaces this when the Docker-based tools arrive."""
import threading

from django.db import close_old_connections

from .runner import execute


def start_async(run):
    def work():
        try:
            execute(run)
        finally:
            close_old_connections()
    t = threading.Thread(target=work, name=f"run-{run.pk}", daemon=True)
    t.start()
    return t

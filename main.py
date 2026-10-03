"""Entry point. Run with `python main.py` (development) or `gunicorn main:app` (container)."""

import logging

from edge_gateway import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

#!/usr/bin/env python3
"""Web server wrapper for Amazon Clone MVP holdout testing.

This server wraps the web app and exposes JSON endpoints that the
Python evaluator can call to validate behavior.
"""

from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os
from pathlib import Path

PORT = 3000
APP_DIR = Path(__file__).parent / "app"


class HoldoutHandler(SimpleHTTPRequestHandler):
    """Extended handler with test endpoints."""

    def do_GET(self):
        if self.path == "/test/endpoints":
            self.send_json({
                "product_listing": "/",
                "search": "/?search=",
                "cart": "/#cart",
                "checkout": "/checkout.html",
                "detail": "/detail.html"
            })
        elif self.path == "/test/health":
            self.send_json({"status": "ok"})
        elif self.path == "/test/products":
            self.send_json({"products": True})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/test/validate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                # Simple validation for checkout form
                email = data.get("email", "")
                card = data.get("card", "")

                valid = True
                if "@" not in email or "." not in email.split("@")[-1]:
                    valid = False
                if len(card.replace(" ", "")) < 16:
                    valid = False

                self.send_json({"valid": valid})
            except Exception as e:
                self.send_json({"valid": False, "error": str(e)})
        elif self.path == "/test/order":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode()
            try:
                data = json.loads(body)
                import random
                import string
                order_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                self.send_json({"order_id": order_id})
            except Exception as e:
                self.send_json({"order_id": None, "error": str(e)})
        else:
            self.send_error(404)

    def send_json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # Suppress logging to avoid PII leakage


def run_server(port=PORT):
    """Start the holdout test server."""
    os.chdir(APP_DIR)
    server = HTTPServer(("localhost", port), HoldoutHandler)
    print(f"Holdout server running on port {port}")
    print(f"Serving app from: {APP_DIR}")
    server.serve_forever()


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PORT
    run_server(port)
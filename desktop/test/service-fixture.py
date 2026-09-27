"""Loopback-only platform fixture. No models, project data or cloud connections."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--data-dir")
parser.add_argument("--config")
args = parser.parse_args()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (
            json.dumps({"service": "aieyra-control", "version": "fixture"}).encode()
            if self.path == "/api/health"
            else b'<!doctype html><meta charset="UTF-8"><title>Control fixture</title><h1>Platform fixture</h1><textarea id="draft"></textarea>'
        )
        self.send_response(200)
        self.send_header(
            "Content-Type", "application/json" if self.path == "/api/health" else "text/html"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()

"""提前拒绝请求时仍返回完整响应；不接触正式配置或数据库。"""

import http.client
import importlib.util
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("control_http_rejection", ROOT / "service/main.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class HTTPRejectionTests(unittest.TestCase):
    def setUp(self):
        self.effects = []
        self.app = SimpleNamespace(csrf="isolated-csrf", submit=self.submit)
        self.server = control.Server(0, self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def submit(self, value):
        self.effects.append(value)
        return {"fixture": True}

    def raw_post(self, body, extra=(), length=None):
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            headers = [
                "POST /api/chat HTTP/1.1",
                f"Host: 127.0.0.1:{self.server.server_port}",
                f"Origin: {self.server.origin}",
                f"X-Control-CSRF: {self.app.csrf}",
                "Content-Type: application/json",
                f"Content-Length: {len(body) if length is None else length}",
                *extra,
            ]
            connection.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
            connection.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(connection)
            response.begin()
            return response.status, json.loads(response.read())

    def test_truncated_upload_never_dispatches_complete_json_prefix(self):
        body = b'{"body":"must not run"}'
        self.assertEqual(self.raw_post(body, length=len(body) + 5)[0], 400)
        self.assertEqual(self.effects, [])

    def test_duplicate_sensitive_headers_never_dispatch(self):
        for header in (
            "Host: other.test",
            "Origin: https://other.test",
            "X-Control-CSRF: other",
            "Content-Length: 999",
            "Content-Type: text/plain",
            "Authorization: Bearer duplicate",
            "Sec-Fetch-Site: cross-site",
        ):
            extras = [header]
            if header.startswith(("Authorization:", "Sec-Fetch-Site:")):
                extras.insert(0, header.split(":")[0] + ": ")
            with self.subTest(header=header):
                self.assertEqual(self.raw_post(b"{}", extras)[0], 400)
        self.assertEqual(self.effects, [])

    def test_ambiguous_or_nonfinite_json_never_dispatches(self):
        for body in (
            b'{"body":"one","body":"two"}',
            b'{"nested":{"id":1,"id":2}}',
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":-Infinity}',
        ):
            with self.subTest(body=body):
                self.assertEqual(self.raw_post(body)[0], 400)
        self.assertEqual(self.effects, [])

    def test_rejections_deliver_complete_json_without_dispatch(self):
        opener = build_opener(ProxyHandler({}))
        headers = {
            "Origin": self.server.origin,
            "X-Control-CSRF": self.app.csrf,
            "Content-Type": "application/json",
        }
        cases = [
            ({"Origin": "https://invalid.example"}, 403, "origin_rejected"),
            ({"X-Control-CSRF": ""}, 403, "csrf_rejected"),
            ({"Content-Type": "text/plain"}, 400, "json_required"),
        ]
        # 重复独立请求覆盖 Windows 发送头与正文之间的调度竞争，不重投业务请求。
        for repeat in range(20):
            for override, status, code in cases:
                with self.subTest(repeat=repeat, code=code):
                    request = Request(
                        self.server.origin + "/api/chat",
                        json.dumps({"body": "x" * 16000}).encode(),
                        headers={**headers, **override},
                    )
                    with self.assertRaises(HTTPError) as caught:
                        opener.open(request, timeout=3)
                    with caught.exception as response:
                        self.assertEqual(response.code, status)
                        self.assertEqual(json.load(response)["code"], code)
        self.assertEqual(self.effects, [])

    def test_rejection_precedes_body_and_discards_pipelined_request(self):
        body = b'{"body":"must not run"}'
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            connection.sendall(
                (
                    f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                    "Origin: https://invalid.example\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            self.assertEqual(response.getheader("Connection"), "close")
            self.assertEqual(json.loads(response.read())["code"], "origin_rejected")
            # 客户端在得知拒绝前已开始上传；不能因关闭读端而丢掉之前的403。
            pipelined = (
                f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                f"Origin: {self.server.origin}\r\nX-Control-CSRF: {self.app.csrf}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode()
            connection.sendall(body + pipelined + body)
            connection.shutdown(socket.SHUT_WR)
            self.assertEqual(connection.recv(1024), b"")
        self.assertEqual(self.effects, [])

    def test_missing_upload_does_not_keep_handler_alive(self):
        finished = threading.Event()

        class ObservedHandler(control.Handler):
            def finish(self):
                try:
                    super().finish()
                finally:
                    finished.set()

        self.server.RequestHandlerClass = ObservedHandler
        with socket.create_connection(self.server.server_address, timeout=2) as connection:
            start = time.monotonic()
            connection.sendall(
                (
                    f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                    "Origin: https://invalid.example\r\nContent-Type: application/json\r\n"
                    "Content-Length: 32768\r\n\r\n"
                ).encode()
            )
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            response.read()
            self.assertTrue(finished.wait(1), "拒绝后的未完成上传不能占用15秒读取超时")
            self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.effects, [])


if __name__ == "__main__":
    unittest.main()

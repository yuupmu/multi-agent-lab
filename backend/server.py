import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .engine import Conflict, Engine
from .openai_client import APIError


def handler_for(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def respond(self, code, payload, content_type="application/json; charset=utf-8"):
            data = json.dumps(payload, ensure_ascii=False).encode() if content_type.startswith("application/json") else payload
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(data)

        def host_allowed(self):
            host = self.headers.get("Host", "")
            return host in ("127.0.0.1:%s" % self.server.server_port, "localhost:%s" % self.server.server_port)

        def do_GET(self):
            if not self.host_allowed():
                return self.respond(403, {"error": "로컬 주소만 허용합니다."})
            path = urlsplit(self.path).path
            try:
                if path == "/api/health":
                    return self.respond(200, {"mode": "openai" if engine.connection()['connected'] else "mock", "model_connected": engine.connection()['connected']})
                if path == '/api/connection':
                    return self.respond(200, engine.connection())
                if path == "/api/runs":
                    return self.respond(200, engine.list_runs())
                if path == "/api/runs/latest":
                    return self.respond(200, engine.latest())
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                    return self.respond(200, engine.get(parts[2]))
                if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "artifact":
                    state = engine.get(parts[2])
                    if not state["artifact"]:
                        raise KeyError("아직 코드가 없습니다.")
                    return self.respond(200, state["artifact"])
                # Explicit allowlist: never serve source, database, benchmark, or answer keys.
                filename = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}.get(path)
                if filename:
                    content = (engine.root / "web" / filename).read_bytes()
                    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                    return self.respond(200, content, mime + "; charset=utf-8")
                self.respond(404, {"error": "찾을 수 없습니다."})
            except KeyError as exc:
                self.respond(404, {"error": str(exc.args[0])})
            except Exception:
                self.respond(500, {"error": "서버 오류"})

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.host_allowed() or (origin and origin != "http://" + self.headers.get("Host", "")):
                return self.respond(403, {"error": "같은 로컬 웹 화면에서 요청하세요."})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "JSON 요청이 필요합니다."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 200000:
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                parts = urlsplit(self.path).path.strip("/").split("/")
                if parts == ['api', 'connection']:
                    return self.respond(200, engine.connect(body.get('key')))
                if parts == ['api', 'connection', 'disconnect']:
                    return self.respond(200, engine.disconnect())
                if parts == ["api", "runs"]:
                    return self.respond(201, engine.start(body))
                if len(parts) == 4 and parts[:2] == ["api", "runs"]:
                    if parts[3] == 'resume':
                        return self.respond(200, engine.resume(parts[2]))
                    if parts[3] == "stop":
                        return self.respond(200, engine.stop(parts[2]))
                    if parts[3] == "test":
                        return self.respond(202, engine.test(parts[2]))
                if len(parts) == 6 and parts[:2] == ["api", "runs"] and parts[3] == "requests" and parts[5] == "answer":
                    return self.respond(200, engine.answer(parts[2], parts[4], body.get("answer"), body.get("note", "")))
                self.respond(404, {"error": "찾을 수 없습니다."})
            except Conflict as exc:
                self.respond(409, {"error": str(exc)})
            except APIError as exc:
                self.respond(400, {'error': str(exc)})
            except KeyError as exc:
                self.respond(404, {"error": str(exc.args[0])})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                self.respond(500, {"error": "서버 오류"})
    return Handler


def create_server(root, port=8000, data_dir=None):
    engine = Engine(root, data_dir=data_dir)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(engine))
    except Exception:
        engine.close()
        raise
    return server, engine

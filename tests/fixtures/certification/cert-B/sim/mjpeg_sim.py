"""Cert-B MJPEG webcam simulator (mjpg-streamer / crowsnest boundary).

GET /webcam/?action=stream -> multipart/x-mixed-replace MJPEG produced live by ffmpeg
(lavfi test pattern, 640x360, 10 fps). GET /webcam/?action=snapshot -> one JPEG.
Usage: python3 mjpeg_sim.py <port>
"""
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SRC = ["ffmpeg", "-hide_banner", "-v", "error", "-re", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/webcam/?action=snapshot"):
            jpg = subprocess.run(SRC[:6] + ["-f", "lavfi", "-i", "testsrc=size=640x360", "-frames:v", "1", "-f", "mjpeg", "-"],
                                 capture_output=True, timeout=30).stdout
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(jpg))); self.end_headers()
            self.wfile.write(jpg); return
        if not self.path.startswith("/webcam/?action=stream"):
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=ffmpeg")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        proc = subprocess.Popen(SRC + ["-c:v", "mjpeg", "-q:v", "5", "-f", "mpjpeg", "-"], stdout=subprocess.PIPE)
        try:
            while True:
                chunk = proc.stdout.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()


ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()

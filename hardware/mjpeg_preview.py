"""MJPEG 웹 프리뷰 서버 (재사용) — rpi_main.py / camera_preview.py 공용.

라파에 모니터가 없어도 카메라 화면을 PC 브라우저에서 본다 (X11 포워딩 불필요).
**카메라를 여는 쪽(단일 소유자)이** draw된 프레임을 push_frame()으로 넣으면 스트리밍한다.
→ rpi_main이 카메라를 잡은 채로 이걸 쓰면 별도 프로세스 없이 원격 관찰이 된다
  (라파 카메라는 한 프로세스만 열 수 있으므로 소유자는 하나여야 한다).

    output = start_preview_server(8000)     # 백그라운드 스레드로 서버 기동
    ...
    draw_hud(frame, mid, x, y, yaw)         # (선택) 검출값 오버레이
    push_frame(output, frame)               # 매 프레임 브라우저로

    # PC 브라우저:  http://<라파IP>:8000
"""

import io
import socketserver
import threading
from http import server
from threading import Condition

import cv2

PORT = 8000

_PAGE = """<!DOCTYPE html>
<html><head><title>AGV 카메라</title>
<style>
 body {{ background:#111; color:#eee; font-family:sans-serif; text-align:center; margin:0; padding:16px; }}
 img {{ width:80vw; max-width:960px; border:1px solid #444; }}
 p  {{ color:#aaa; max-width:960px; margin:12px auto; line-height:1.6; }}
 b  {{ color:#fff; }}
</style></head>
<body>
<h2>AGV 카메라</h2>
<img src="stream.mjpg">
<p>{note}</p>
</body></html>"""


class StreamingOutput(io.BufferedIOBase):
    """최신 JPEG 프레임 한 장을 들고 있다가 대기 중인 스트림 클라이언트에게 깨워 보낸다."""

    def __init__(self):
        self.frame = None
        self.condition = Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


class _StreamingHandler(server.BaseHTTPRequestHandler):
    # start_preview_server()가 서브클래스로 output/page를 주입한다.
    _output: StreamingOutput = None
    _page: bytes = b""

    def do_GET(self):
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
        elif self.path == '/index.html':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', len(self._page))
            self.end_headers()
            self.wfile.write(self._page)
        elif self.path == '/stream.mjpg':
            self.send_response(200)
            self.send_header('Cache-Control', 'no-cache, private')
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=FRAME')
            self.end_headers()
            try:
                while True:
                    with self._output.condition:
                        self._output.condition.wait()
                        frame = self._output.frame
                    self.wfile.write(b'--FRAME\r\n')
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', len(frame))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.write(b'\r\n')
            except Exception:
                pass  # 브라우저가 닫힘 — 정상
        else:
            self.send_error(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class _StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_preview_server(port: int = PORT, note: str = "") -> StreamingOutput:
    """MJPEG 서버를 백그라운드 스레드로 띄우고, 프레임을 넣을 StreamingOutput을 돌려준다."""
    output = StreamingOutput()
    page = _PAGE.format(note=note).encode('utf-8')
    handler = type("_BoundHandler", (_StreamingHandler,),
                   {"_output": output, "_page": page})
    srv = _StreamingServer(('0.0.0.0', port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return output


def push_frame(output: StreamingOutput, frame, quality: int = 80):
    """프레임(BGR/RGB ndarray)을 JPEG로 인코딩해 스트림에 밀어넣는다."""
    ok, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        output.write(jpg.tobytes())


def draw_hud(frame, marker_id, x, y, yaw):
    """검출값을 프레임 위에 최소한으로 그린다 (id / yaw / x / y). 서버로 가는 값 그대로."""
    if marker_id is None:
        cv2.putText(frame, "no marker", (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (128, 128, 128), 2)
    else:
        cv2.putText(frame, f"ID {marker_id}   yaw {yaw:7.1f}", (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2)
        cv2.putText(frame, f"x {x:.0f}mm   y {y:.0f}mm", (10, 78),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

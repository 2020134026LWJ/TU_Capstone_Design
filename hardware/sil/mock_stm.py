"""
Mock STM32 — SIL용 가짜 펌웨어 (현실 버전)
TU Capstone Design

주원이 main.c 분석 기반 실제 응답:
  STATE_READY → 명령(1~7) 받음 → ACK(0xFF) 즉시 → STATE_MOVE
              → (move_time 동작) → DONE(0x81) 1번 → STATE_READY
  · 항상 ACK→DONE 순서  · 동작당 DONE 정확히 1번 (STATE_READY로 멈춰 재송신 없음)
  · command 0 = carrier(대기) → 무시

현실적 위험 옵션:
  move_time : 동작 시간 (모터 도는 시간 흉내)
  drop_done : 통신 유실 — DONE을 안 보냄 (UART 노이즈로 바이트 누락 흉내)

※ pty master는 os.read/os.write(fd)로 통신 (serial.Serial 불가)
"""

import os
import select
import time
import threading

EVT_DONE = 0x81
EVT_ACK  = 0xFF


class MockSTM:
    def __init__(self, master_fd, move_time=0.1):
        self.fd = master_fd
        self.move_time = move_time
        self.drop_done = False             # 명령별로 동적 설정 (통신 유실 흉내)

        self._running = False
        self._buf = bytearray()
        self.received = []                 # 받은 command 기록 (검증용)

    def start(self):
        self._running = True
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if not r:
                continue
            try:
                b = os.read(self.fd, 1)
            except OSError:
                break
            if not b:
                continue
            self._buf += b
            if b == b'>':
                self._handle(bytes(self._buf))
                self._buf = bytearray()
            elif len(self._buf) > 42:
                self._buf = bytearray()

    def _handle(self, pkt):
        if len(pkt) < 3 or pkt[0:1] != b'<':
            return
        command = pkt[1] - ord('0')        # buf[1] = command
        self.received.append(command)
        if command == 0:
            return                         # carrier(대기) — 무시

        # 실제 STM: ACK 즉시 → (동작) → DONE 1번
        os.write(self.fd, bytes([EVT_ACK]))
        drop = self.drop_done
        threading.Thread(target=self._motion, args=(drop,), daemon=True).start()

    def _motion(self, drop):
        time.sleep(self.move_time)
        if not drop:
            os.write(self.fd, bytes([EVT_DONE]))   # 동작 완료 (유실 시 생략)

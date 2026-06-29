"""
SIL 하니스 (현실 버전) — bridge_rpi ↔ (pty 가상 UART) ↔ MockSTM
TU Capstone Design

명령을 '하나씩'(서버처럼 DONE 받고 다음) 보내며, 주원이 STM 실제 응답
(ACK→동작시간→DONE 1번) 기준으로 진짜 가능한 위험만 검증:
  ① forward 완료 처리 — forward도 DONE 1번 오는데 bridge가 맞는 cmd_ack를 내나
  ② 통신 유실        — DONE이 안 오면 bridge가 멈추나 (timeout/복구 없음)

실행:  cd TU_Capstone_Design && python3 -m hardware.sil.run_sil
"""

import os
import pty
import json
import time

import hardware.bridge_rpi as br
from hardware.sil.mock_stm import MockSTM


class FakeMQTTClient:
    """broker 없이 SIL 돌리기 위한 가짜 client (publish만 가로챔)"""
    def __init__(self, on_publish=None):
        self.on_connect = None
        self.on_message = None
        self._on_publish = on_publish

    def connect(self, *a, **k): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, *a, **k): pass

    def publish(self, topic, payload):
        if self._on_publish:
            self._on_publish(topic, payload)


def _make_pty():
    master_fd, slave_fd = pty.openpty()
    return master_fd, os.ttyname(slave_fd)


def _setup():
    master_fd, slave_name = _make_pty()
    br.UART_ENABLED = True
    br.UART_PORT = slave_name
    published = []
    br.mqtt.Client = lambda **k: FakeMQTTClient(
        on_publish=lambda t, p: published.append((t, json.loads(p))))
    bridge = br.Bridge(rid=1)
    bridge.open_uart()
    bridge.set_marker_offset(10.0, -5.0, 90.0)
    return bridge, published, master_fd


def _ack_count(published):
    return sum(1 for _, p in published if p.get("type") == "cmd_ack")


def send_and_wait(bridge, published, cmd, timeout=1.5):
    """명령 보내고 cmd_ack 올 때까지 대기 (= 서버의 '하나씩'). timeout이면 멈춤(None)."""
    n0 = _ack_count(published)
    bridge._dispatch_cmd(cmd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _ack_count(published) > n0:
            acks = [p for _, p in published if p.get("type") == "cmd_ack"]
            return acks[-1]["cmd"]
        time.sleep(0.01)
    return None


def run_scenario(name, cmds, move_time=0.1, drop_done_on=None):
    print(f"\n{'='*54}\n[시나리오] {name}\n{'='*54}")
    bridge, published, master_fd = _setup()
    stm = MockSTM(master_fd, move_time=move_time)
    stm.start()

    for cmd in cmds:
        stm.drop_done = (drop_done_on == cmd)      # 이 명령 차례에 DONE 유실
        ack = send_and_wait(bridge, published, cmd)
        if ack is None:
            print(f"  ❌ '{cmd}' → DONE 안 옴 (1.5s timeout) → AGV 멈춤! (복구 로직 없음)")
            break
        mark = "✅" if ack == cmd else f"⚠️  ack가 '{ack}' (보낸 건 '{cmd}')"
        print(f"  {mark}  '{cmd}' → cmd_ack '{ack}'")

    stm.stop()
    bridge.disconnect()
    os.close(master_fd)


if __name__ == "__main__":
    run_scenario("① 정상 (하나씩, 완료 기다리고 다음)",
                 ["turn_left", "forward", "lift_up"])
    run_scenario("② 느린 동작 (move_time=0.5s)",
                 ["forward", "turn_right"], move_time=0.5)
    run_scenario("③ 통신 유실 — forward의 DONE 누락",
                 ["turn_left", "forward", "lift_up"], drop_done_on="forward")
    print("\n" + "="*54)
    print("현실 SIL 종료 — ❌/⚠️ 가 실제 운영에서 진짜 위험입니다.")
    print("="*54)

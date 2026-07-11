"""
SIL 하니스 (현실 버전) — bridge_rpi ↔ (pty 가상 UART) ↔ MockSTM
TU Capstone Design

명령을 '하나씩'(서버처럼 완료 받고 다음) 보내며, 주원이 STM 실제 응답
(ACK→동작시간→DONE 1번) 기준으로 진짜 가능한 위험만 검증.

[문제 #1 수정 반영 — 2026-06-30]
  forward 완료는 카메라 '마커'가 서버에 보고(_marker_mixin이 forward ACK로 사용).
  → bridge는 forward DONE에 cmd_ack를 보내지 않음 (중복/오ack 방지).
  · turn/lift : DONE → cmd_ack 발행 (완료 신호)
  · forward   : DONE → 조용히 소비 (cmd_ack 없음). ACK로 _pending_code만 0 복귀.

검증 항목:
  ① 정상     — turn/lift는 cmd_ack 옴 / forward는 cmd_ack 없이 ACK·DONE 정상 소비
  ② 느린 동작 — move_time 늘려도 동일
  ③ 통신 유실 — turn의 DONE 누락 시 bridge가 멈추나 (cmd_ack 안 옴 → 서버 hang)
               (forward DONE 유실은 이제 무해 — 완료는 마커 채널이라 UART와 무관)

실행:  cd TU_Capstone_Design && python3 -m virtual_test.software_in_the_loop.run_sil
"""

import os
import pty
import json
import time

import hardware.bridge_rpi as br
from virtual_test.software_in_the_loop.mock_stm import MockSTM

TURN_LIFT = ("turn_left", "turn_right", "turn_180", "lift_up", "lift_down")


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
            self._on_publish(topic, json.loads(payload))


def _make_pty():
    master_fd, slave_fd = pty.openpty()
    return master_fd, os.ttyname(slave_fd)


def _setup():
    master_fd, slave_name = _make_pty()
    br.UART_ENABLED = True
    br.UART_PORT = slave_name
    published = []
    br.mqtt.Client = lambda **k: FakeMQTTClient(on_publish=lambda t, p: published.append((t, p)))
    bridge = br.Bridge(rid=1)
    bridge.open_uart()
    bridge.set_marker_offset(10.0, -5.0, 90.0)
    return bridge, published, master_fd


def _ack_count(published):
    return sum(1 for _, p in published if p.get("type") == "cmd_ack")


def send_turn_lift(bridge, published, cmd, timeout=1.5):
    """turn/lift: cmd_ack 올 때까지 대기 (= 서버의 '하나씩'). timeout이면 멈춤(None)."""
    n0 = _ack_count(published)
    bridge._dispatch_cmd(cmd)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _ack_count(published) > n0:
            acks = [p for _, p in published if p.get("type") == "cmd_ack"]
            return acks[-1]["cmd"]
        time.sleep(0.01)
    return None


def send_forward(bridge, published, move_time, timeout=1.5):
    """forward: cmd_ack 없어야 정상. ACK로 _pending_code=0 + DONE 조용히 소비 확인.

    반환: (ok, reason)
    """
    n0 = _ack_count(published)
    bridge._dispatch_cmd("forward")
    # ACK(즉시) + DONE(move_time 뒤) 도착·소비 대기
    deadline = time.time() + timeout
    while time.time() < deadline:
        if bridge._pending_code == 0:        # ACK 처리됨 (armed → carrier 복귀)
            break
        time.sleep(0.01)
    time.sleep(move_time + 0.15)             # DONE 도착·소비 여유
    if bridge._pending_code != 0:
        return False, "ACK 미수신 (_pending_code≠0)"
    if _ack_count(published) != n0:
        return False, "forward인데 cmd_ack 발행됨 (중복 신호)"
    if bridge._last_cmd is not None:
        return False, f"_last_cmd 잔류({bridge._last_cmd})"
    return True, "cmd_ack 없음 + ACK·DONE 정상 소비"


def run_scenario(name, cmds, move_time=0.1, drop_done_on=None):
    print(f"\n{'='*54}\n[시나리오] {name}\n{'='*54}")
    bridge, published, master_fd = _setup()
    stm = MockSTM(master_fd, move_time=move_time)
    stm.start()

    for cmd in cmds:
        stm.drop_done = (drop_done_on == cmd)      # 이 명령 차례에 DONE 유실
        if cmd == "forward":
            ok, reason = send_forward(bridge, published, move_time)
            mark = "✅" if ok else "❌"
            print(f"  {mark}  'forward' → {reason}")
            if not ok:
                break
        else:
            ack = send_turn_lift(bridge, published, cmd)
            if ack is None:
                print(f"  ❌ '{cmd}' → cmd_ack 안 옴 (1.5s timeout) → AGV 멈춤! (복구 로직 없음)")
                break
            mark = "✅" if ack == cmd else f"⚠️  ack가 '{ack}' (보낸 건 '{cmd}')"
            print(f"  {mark}  '{cmd}' → cmd_ack '{ack}'")

    stm.stop()
    bridge.disconnect()
    os.close(master_fd)


if __name__ == "__main__":
    run_scenario("① 정상 (turn/forward/lift — forward는 cmd_ack 없음이 정상)",
                 ["turn_left", "forward", "lift_up"])
    run_scenario("② 느린 동작 (move_time=0.5s)",
                 ["forward", "turn_right"], move_time=0.5)
    run_scenario("③ 통신 유실 — turn의 DONE 누락 (forward 아님: 그건 이제 무해)",
                 ["forward", "turn_left", "lift_up"], drop_done_on="turn_left")
    print("\n" + "="*54)
    print("현실 SIL 종료 — ❌/⚠️ 가 실제 운영에서 진짜 위험입니다.")
    print("③의 turn DONE 유실 멈춤 = 미해결 TODO ③ (통신유실 복구).")
    print("="*54)

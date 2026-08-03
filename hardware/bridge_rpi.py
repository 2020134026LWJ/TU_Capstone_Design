"""
AGV Bridge (실물 RPi 전용) — 주원이 STM32 펌웨어 프로토콜 대응
TU Capstone Design - AGV 물류 피킹 시스템

  서버(MQTT) ↔ Bridge(RPi) ↔ STM32(UART)
  + 같은 라파의 카메라 스레드가 set_marker_offset()으로 최신 ArUco offset 공급
  ※ Isaac Sim(콜백) 버전은 isaac_simulation/bridge_isaac.py (이 파일은 실물 AGV 전용)

MQTT 토픽:
  수신: /agv/cmd         서버 → AGV 명령
  송신: /agv/marker      AGV 위치/방향 보고 (marker_id + heading)  ← 카메라 스레드가 호출
        /agv/cmd_ack     명령 완료 보고

UART 프로토콜 (RPi ↔ STM32, 주원이 rpi_uart.c / main.c 기준):
  송신 (Bridge→STM): ASCII "<command,±xxxx,±yyyy,±wwww>"  (21바이트)
      command = 1자리(1~7, 0=carrier/무명령), x/y/yaw = (mm·deg)×10 정수 → STM이 /10로 복원
      x/y/yaw offset = 카메라가 본 ArUco 오프셋 (set_marker_offset로 갱신한 최신값)
  수신 (STM→Bridge): 단일 바이트  0x81=DONE(동작완료) / 0xFF=ACK(명령수신)

  전송 흐름 (주원이 카메라와 동일 — bridge가 인수):
      set_marker_offset()가 매 프레임 패킷 스트리밍. command은 평소 0,
      MQTT 명령 수신 시 _pending_code에 실림 → EVT_ACK 오면 0으로 복귀.

[carrier 흐름 — 해결(2026-06-30)] 명령 계속 보내도 OK. 주원이가 STM에 DMA 처리 →
  명령은 필요할 때만 동작. bridge는 오프셋을 연속 스트리밍, command=0이 기본.

[미팅 후 조정 필요 — TODO]
  (2) forward 완료신호 : STM은 forward도 DONE을 보냄 ↔ 서버는 marker로 도착 인지 → 조율
  (3) 통신 유실 복구 : DONE 누락 시 timeout 주체(bridge/서버) + STM 상태조회 가능한가

[결정됨 — 카메라(비전)는 주원이 영역, bridge는 받기만]
  - UART 포트 = /dev/ttyAMA0 (주원이 카메라 코드와 동일)
  - 카메라가 (marker_id, x, y, yaw)를 계산 → set_marker_offset()/publish_marker()로 넘김
  - heading/yaw 변환·ArUco 비전 로직은 주원이 카메라가 담당 (bridge는 안 건드림)
"""

import json
import os
import time
import threading
import uuid
from typing import Optional

import paho.mqtt.client as mqtt

# ─── 설정은 hardware/config.py 에서 (배포 시 그 파일만 수정) ───
# 모듈 전역으로 가져옴 → SIL 테스트가 br.UART_ENABLED/br.UART_PORT 를 monkeypatch 가능
from hardware.config import (
    MQTT_HOST, MQTT_PORT,
    TOPIC_CMD, TOPIC_MARKER, TOPIC_CMD_ACK, TOPIC_PRESENCE,
    UART_PORT, UART_BAUD, UART_ENABLED, UART_OFFSET_HZ,
    TOPIC_POSE, POSE_PUBLISH_HZ,
)


# ─── 명령 / 이벤트 코드 (주원이 rpi_uart.h와 동일) ───

# 명령 문자열 → STM 명령 코드 (ASCII 숫자 1자리로 전송)
CMD_CODE = {
    "forward":    0x01,
    "stop":       0x02,
    "lift_up":    0x03,
    "lift_down":  0x04,
    "turn_left":  0x05,
    "turn_right": 0x06,
    "turn_180":   0x07,
}

EVT_DONE = 0x81  # STM → 동작 완료
EVT_ACK  = 0xFF  # STM → 명령 수신 확인

# forward 완료(EVT_DONE) 후 마커 발행까지의 안정 대기 (2026-07-17).
# 모터가 멈춘 직후 BNO055 yaw가 진동/관성으로 흔들려, 바로 turn을 파싱하면 STM이
# 회전량을 잘못 잡아 no-op이 된다(실측: 대기 없으면 32ms에 "완료"=안 돎, 3초 대기하면 정상).
# EVT_DONE 후 이 시간만큼 마커 발행을 늦춰 서버가 다음 명령을 늦게 내게 한다.
# 그동안 카메라가 STM에 offset을 계속 흘려 yaw가 안정된다. env로 튜닝: FWD_SETTLE_SECS
# 기본 0.8 (2026-07-21): 2대 동시 HIL에서 0.8로 완주 확인 — 1.5는 필요 이상으로 느려
# 매 노드마다 0.7초씩 쌓였다. 회전 no-op이 재발하면 이 값부터 올려볼 것.
FORWARD_SETTLE_SECS = float(os.environ.get("FWD_SETTLE_SECS", "0.8"))

# 리프트 완료(EVT_DONE) 후 cmd_ack 발행까지의 정착 대기 (2026-07-21).
# STM의 리프트 DONE은 TIM5 타이머 기반이라 스텝 펄스 종료 시점에 뜨는데, 시저리프트가
# 기계적으로 완전히 내려/올라가 정착하기 전에 뜰 수 있다. 즉시 cmd_ack를 내면 서버가
# 곧장 다음 명령(forward)을 발행 → 리프트가 덜 내려간 채 출발한다(실물 관측 2026-07-21,
# 특히 WS에 선반 내려놓고 출발할 때). forward의 FORWARD_SETTLE_SECS와 같은 논리로, 리프트
# DONE 후 이 시간만큼 cmd_ack를 늦춰 다음 명령을 지연시킨다. env로 튜닝: LIFT_SETTLE_SECS.
# [주의] 리프트가 '끝까지 안 내려가고 멈춘다'면 이건 정착 문제가 아니라 STM TIM5 Period가
#        짧은 것 = 펌웨어 사안(주원)이라 이 값으로는 안 고쳐진다.
# 기본값 0.7 (2026-07-21): 서버 로그 실측 — STM 리프트 ack이 항상 명령 발행 후 ~1.47초에
# 오는데(up/down 공통) 물리 리프트는 ~2.13초라 ack이 ~0.65초 이르게 뜬다. 그 gap을 메운다.
# 여전히 성급하면 올릴 것. (근본은 STM TIM5가 물리보다 이르게 DONE을 쏘는 것 = 주원이 TIM5를
#  물리 시간에 맞추면 이 값 0으로 가능. 여기선 펌웨어 무수정으로 브릿지에서 보정.)
LIFT_SETTLE_SECS = float(os.environ.get("LIFT_SETTLE_SECS", "0.7"))

# 회전 완료(EVT_DONE) 후 cmd_ack 발행까지의 정착 대기 (2026-07-22).
# STM이 회전 DONE을 보고하는 시점에도 차체가 관성으로 목표각을 지나쳤다가(오버슛) 아직
# IMU 폐루프가 목표로 되돌아오는 중일 수 있다(실물 관측: turn_180이 181° 목표인데 DONE
# 직후 yaw≈221°). 즉시 cmd_ack를 내면 서버가 곧장 forward를 발행 → 로봇이 아직 안정되지
# 않은(틀어진) 헤딩으로 출발해 다음 마커를 놓친다. forward/리프트의 *_SETTLE_SECS와 같은
# 논리로, 회전 DONE 후 이 시간만큼 cmd_ack를 늦춰 차체가 목표각에 정착한 뒤 다음 명령이
# 나가게 한다. 서버는 이 ack 전엔 다음 명령을 안 내므로 = 회전이 정착할 때까지 안 움직인다.
# [주의] 오버슛이 '되돌아오지 않고 그 자리에 멈추는' 종류라면(폐루프 없음) 이 값으로는
#        안 고쳐진다 = 펌웨어 감쇠/정지임계 사안(주원). 넣기 전 turn_180 격리 테스트로
#        DONE 후 yaw가 목표로 기어드는지(=정착시간이 듣는지) 먼저 확인할 것.
# env로 튜닝: TURN_SETTLE_SECS. 기본 0.5 (보수적 시작값 — 실측 후 조정).
TURN_SETTLE_SECS = float(os.environ.get("TURN_SETTLE_SECS", "0.5"))

# 반복 이벤트 병합 창 (2026-07-18).
# 주원이 STM 펌웨어(hardware/stm32/rpi_uart.c Send_Event)는 통신유실 대비로 EVT_DONE/EVT_ACK를
# 매번 3회 반복 전송한다(10ms 간격 → ~20ms 구간에 0x81 3개 / 0xFF 3개). 브릿지가 이 반복을
# 그대로 처리하면, 앞 명령의 2·3번째 반복 바이트가 방금 arm된 다음 명령(_last_cmd)의 ack으로
# 잘못 붙어 dispatch 직후 ~12ms에 '유령 ack'이 터진다(리프트 no-op의 근본원인). 실제 이벤트는
# 어떤 것도 이 창 안에 두 번 오지 않으므로(turn/lift/forward 모두 완료까지 >1초), 같은 이벤트
# 값이 이 시간 안에 또 오면 반복으로 보고 버린다 → 첫 바이트만 그 명령 것으로 센다.
EVENT_DEDUP_SECS = float(os.environ.get("EVENT_DEDUP_SECS", "0.1"))


class Bridge:
    """AGV UART 브릿지 (실물 RPi 전용, 단일 로봇)"""

    def __init__(self, rid: int):
        self.rid = rid

        # 수정 63: client_id 전역 유일성. 초 단위 타임스탬프로는 부족하다 —
        # 트윈(Isaac)과 같은 초에 뜨면 id가 겹쳐 브로커가 서로를 끊어내는
        # 무한 재연결 루프에 빠진다(명령 유실). pid+uuid로 확실히 분리한다.
        # 접두사도 역할별로 다르게: 실물/벤치="bridge_", 트윈="twin_".
        self._client = mqtt.Client(
            client_id=f"bridge_{rid}_{os.getpid()}_{uuid.uuid4().hex[:6]}")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

        # 수정 75 — 유언(LWT). **connect() 전에 등록해야 효력이 있다.**
        # 전원이 나가면 우리 코드는 한 줄도 못 돈다 → 브로커가 대신 offline을 알려준다.
        self._client.will_set(
            TOPIC_PRESENCE,
            json.dumps({"rid": rid, "online": False}),
            qos=1, retain=True,
        )

        # UART
        self._serial = None
        self._uart_thread: Optional[threading.Thread] = None
        self._running = False

        # UART write 락 — 카메라 스레드(offset 스트리밍)와 MQTT 스레드(명령)가
        # 동시에 write하면 바이트가 뒤섞여 STM이 깨진 패킷을 읽는다(급발진 원인).
        # 한 패킷(write+flush)을 통째로 원자화한다. jw_headless가 단일 스레드로
        # 얻던 효과를 락으로 재현.
        self._uart_lock = threading.Lock()

        # 마지막으로 전송한 명령 추적 (DONE 수신 시 ack 타입 결정)
        self._last_cmd: Optional[str] = None

        # 반복 이벤트 병합용 — 각 이벤트 값(0x81/0xFF)의 마지막 처리 시각.
        # STM이 3회 반복 전송하는 EVT_DONE/EVT_ACK를 한 번으로 합친다(EVENT_DEDUP_SECS).
        self._evt_last_ts: dict = {}

        # forward 마커 게이팅 (2026-07-17): forward 주행 중엔 카메라 마커 발행을 붙잡았다가
        # STM EVT_DONE(=실물이 실제로 멈춘 시점)에 발행한다. 두 가지를 동시에 얻는다:
        #   ① forward 완료를 카메라(이른 시점)가 아닌 실물 완료로 통일 (turn/lift와 동일 기준)
        #   ② 주행 중 옆칸 마커 오검출이 서버로 안 감 (mismatch 자체가 안 생김)
        self._fwd_active = False               # forward dispatch ~ settle 끝까지 (마커 버퍼링 구간)
        self._fwd_marker: Optional[tuple] = None  # 주행 중/안정 중 본 최신 마커 (settle 후 발행)
        self._fwd_settling = False             # EVT_DONE 받고 settle 타이머 도는 중 (중복 방지)

        # 현재 STM으로 실어 보내는 명령 코드 (0=carrier/무명령).
        # MQTT 명령 수신 시 set → EVT_ACK 오면 0으로 복귀 (주원이 카메라와 동일 흐름)
        self._pending_code = 0

        # 카메라 스레드가 갱신하는 최신 ArUco offset (x_mm, y_mm, yaw_deg)
        # → UART 송신 시 command과 합쳐 패킷에 실림
        self._latest_offset = (0.0, 0.0, 0.0)

        # 수정 70 — 서버가 알려준 forward 목적지 (실물은 안 씀, 벤치 가짜 로봇이 사용)
        self._target_node: Optional[int] = None

        # 수정 67 — UART 오프셋 스트리밍 주기 제한 (카메라 프레임률과 분리)
        self._offset_period = 1.0 / UART_OFFSET_HZ if UART_OFFSET_HZ > 0 else 0.0
        self._last_offset_tx = 0.0

        # 수정 68 — /agv/pose 연속 스트림 (트윈 전용) 발행 주기 제한
        self._pose_period = 1.0 / POSE_PUBLISH_HZ if POSE_PUBLISH_HZ > 0 else 0.0
        self._last_pose_tx = 0.0

    # ─── MQTT ─────────────────────────────────────────────────────────────────

    def connect(self):
        self._client.connect(MQTT_HOST, MQTT_PORT, 60)
        self._client.loop_start()

    def disconnect(self):
        self._running = False
        if self._uart_thread:
            self._uart_thread.join(timeout=0.5)   # 수신 스레드 정리 후 포트 닫기
        # 수정 75: 정상 종료는 유언을 남기지 않으므로(브로커가 LWT를 폐기한다)
        # 직접 offline을 알린다. Ctrl+C로 끄고 서버가 계속 도는 경우.
        self._publish_presence(False)
        self._client.loop_stop()
        self._client.disconnect()
        if self._serial:
            self._serial.close()

    def _publish_presence(self, online: bool):
        """수정 75 — 접속/이탈 알림. retain=True: 서버가 나중에 떠도 즉시 안다."""
        self._client.publish(
            TOPIC_PRESENCE,
            json.dumps({"rid": self.rid, "online": online}),
            qos=1, retain=True,
        )

    def _on_connect(self, client, userdata, flags, rc):
        client.subscribe(TOPIC_CMD)
        # 재접속 때도 다시 발행된다 — LWT로 offline이 박힌 뒤 돌아온 경우를 덮어써야 한다.
        self._publish_presence(True)
        print(f"[Bridge-{self.rid}] MQTT connected (rc={rc}) — presence online")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic == TOPIC_CMD:
            rid = int(data.get("rid", -1))
            cmd = data.get("cmd", "")
            if rid == self.rid and cmd:
                # 수정 70: forward 목적지(서버가 알려줌). 실물 AGV는 물리적으로 앞으로
                # 갈 뿐이라 안 써도 되지만, 벤치의 가짜 로봇은 이걸로 추측을 없앤다.
                self._target_node = data.get("target_node")
                self._dispatch_cmd(cmd)

    def _dispatch_cmd(self, cmd: str, send_now: bool = False):
        code = CMD_CODE.get(cmd)
        if code is None:
            print(f"[Bridge-{self.rid}] Unknown cmd: {cmd}")
            return
        print(f"[Bridge-{self.rid}] <- cmd: {cmd}")
        if cmd != "forward":
            self._last_cmd = cmd      # DONE 수신 시 ack 타입 결정용
        else:
            # forward 시작 → 완료+안정까지 마커 발행 보류 (버퍼·타이머 상태 새로 시작)
            self._fwd_active = True
            self._fwd_marker = None
            self._fwd_settling = False
        self._pending_code = code     # arm: 다음 패킷부터 실려 나감 (EVT_ACK까지 반복)
        # [2026-07-24] 평상시(MQTT 명령)엔 여기서 직접 UART로 안 쏜다 — writer를 카메라
        #   스레드 하나로 통일한다. 예전엔 MQTT 콜백 스레드가 여기서 즉시 1발 썼는데,
        #   카메라 스레드의 10Hz offset 스트림과 합쳐 writer가 2개가 되어, 이 즉시-한-발이
        #   carrier 패킷 꽁무니에 공백(IDLE) 없이 붙는 순간 STM의 ReceiveToIdle 프레이밍이
        #   뭉개졌다(Size!=21 → RX FAIL). 간헐적 회전 오차/리프트 오작동의 유력 원인.
        #   → _pending_code만 장전하고, 카메라 스레드의 다음 _uart_send_packet(≤100ms)이
        #     이 코드를 얹어 보낸다 = jw_headless/opencv_aruco의 '큐에 담고 루프가 얹어
        #     보내기' 단일-writer 구조 복원. (명령은 로봇이 마커 위에 선 상태에서 오므로
        #     카메라 스트림이 흐르고 있다 = 전달 보장)
        #   단 park_lift처럼 카메라 루프가 이미 멈춘 종료 컨텍스트에선 아무도 안 보내므로
        #   send_now=True로 직접 보낸다 — 그땐 단일 스레드라 프레임 충돌도 없다.
        if send_now:
            self._uart_send_packet()

    # ─── 카메라 offset 공급 (같은 라파 카메라 스레드가 호출) ───────────────────

    def set_marker_offset(self, x_mm: float, y_mm: float, yaw_deg: float):
        """카메라 스레드가 최신 ArUco offset 갱신 → STM으로 패킷 스트리밍.

        주원이 카메라가 매 프레임 UART에 쓰던 것을 bridge가 인수받음.
        command은 평소 0(carrier), MQTT 명령 수신 시에만 실림.
        같은 라파 안 메모리 공유라 MQTT 불필요. 단순 변수 대입(원자적)이라 락 불요.

        수정 67: **UART 전송 주기를 카메라 프레임률에서 분리한다** (UART_OFFSET_HZ 상한).
        예전엔 매 프레임 무조건 쐈다 → 카메라를 빠르게 만들면 STM 부하가 조용히 따라 올랐다.
        최신 값은 항상 갱신해두고, 실제 전송만 제한한다 (오래된 값을 보내는 일은 없다).
        """
        self._latest_offset = (float(x_mm), float(y_mm), float(yaw_deg))

        now = time.time()
        if now - self._last_offset_tx < self._offset_period:
            return                      # 아직 주기 안 됨 — 값만 갱신하고 전송은 생략
        self._last_offset_tx = now
        self._uart_send_packet()

    # ─── 발행 ─────────────────────────────────────────────────────────────────

    def publish_marker(self, marker_id: int, heading_deg: Optional[int] = None,
                       heading_observed: Optional[int] = None):
        """ArUco 마커 감지 결과 서버에 보고 (카메라 스레드가 호출).

        heading_deg: **서버가 제어에 그대로 쓴다.** 시뮬(IsaacCamera)이 보내는 신뢰된 값.

        heading_observed (수정 69, A 배관): **서버가 비교/로그만 하고 제어엔 안 쓴다.**
          실물 카메라가 계산한 heading. HEADING_OFFSET이 아직 실측 전이라 믿을 수 없다.
          → 보내되 믿지는 않는다. 서버 로그에 '카메라 X° vs 장부 Y°'가 찍히므로,
            그 차이가 일정하면 그게 곧 HEADING_OFFSET 보정값이다.
            **캘리브레이션이 '코드 짜기'가 아니라 '로그 읽기'가 된다.**
          확정되면 server.config.TRUST_CAMERA_HEADING = True 로 밸브를 연다.

        2026-07-17: forward 주행 중(_fwd_active)엔 즉시 발행하지 않고 버퍼링만 한다.
          → 실물이 멈춘 시점(EVT_DONE)에 마지막 마커만 발행 (_handle_uart_event).
          idle/turn/lift 중엔 _fwd_active=False 라 평소대로 즉시 발행(heading 초기화 등).
        """
        if self._fwd_active:
            self._fwd_marker = (marker_id, heading_deg, heading_observed)
            return
        self._publish_marker_now(marker_id, heading_deg, heading_observed)

    def _publish_marker_now(self, marker_id: int, heading_deg: Optional[int] = None,
                            heading_observed: Optional[int] = None):
        """실제 /agv/marker 발행 (게이팅 통과분)."""
        msg = {
            "rid": self.rid,
            "marker_id": marker_id,
            "ts": int(time.time()),
        }
        if heading_deg is not None:
            msg["heading"] = heading_deg
        if heading_observed is not None:
            msg["heading_observed"] = heading_observed
        self._client.publish(TOPIC_MARKER, json.dumps(msg))
        print(f"[Bridge-{self.rid}] -> /agv/marker  id={marker_id}")

    def publish_pose(self, marker_id: int, x_mm: float, y_mm: float, yaw_deg: float):
        """연속 자세 스트림 (수정 68, B) — **트윈 전용. 서버는 구독하지 않는다.**

        카메라가 매 프레임 계산하는 (x, y, yaw)를 MQTT로도 흘린다. 지금까지는 STM으로만 갔다.
        트윈이 이걸 받아 **회전을 실시간으로 따라간다** (마커·cmd_ack 사이를 시간으로
        보간하던 것 → 실제 측정값으로 대체).

        POSE_PUBLISH_HZ 상한으로 제한 — 카메라가 빨라져도 발행량이 따라 늘지 않는다.
        """
        now = time.time()
        if now - self._last_pose_tx < self._pose_period:
            return
        self._last_pose_tx = now
        msg = {
            "rid": self.rid,
            "marker_id": marker_id,
            "x": round(float(x_mm), 1),
            "y": round(float(y_mm), 1),
            "yaw": round(float(yaw_deg), 1),
        }
        self._client.publish(TOPIC_POSE, json.dumps(msg), qos=0)

    def publish_cmd_ack(self, cmd: str):
        """명령 완료 서버에 보고"""
        msg = {"type": "cmd_ack", "rid": self.rid, "cmd": cmd, "status": "done"}
        self._client.publish(TOPIC_CMD_ACK, json.dumps(msg))
        print(f"[Bridge-{self.rid}] -> /agv/cmd_ack  cmd={cmd}")

    # ─── UART 송신 (ASCII <cmd,x,y,yaw>) ──────────────────────────────────────

    def _uart_send_packet(self):
        """현재 armed 명령(_pending_code) + 최신 카메라 offset → 주원이 ASCII 패킷 전송.

        set_marker_offset(프레임마다) + _dispatch_cmd(명령 시)에서 호출.
        _pending_code=0이면 carrier 패킷(오프셋만, STM PID 정렬용).
        """
        x, y, yaw = self._latest_offset
        if self._pending_code != 0:   # [진단] 명령 패킷에 실려 나가는 오프셋 — turn 상쇄(dyaw) 확인용
            print(f"[Bridge-{self.rid}]    → STM cmd={self._pending_code} 오프셋 x={x:+.1f} y={y:+.1f} yaw={yaw:.1f}")
        # 주원이 포맷: <command,±xxxx,±yyyy,±wwww>  (offset은 ×10 정수, STM이 /10 복원)
        msg = f"<{self._pending_code},{int(x * 10):+05d},{int(y * 10):+05d},{int(yaw * 10):+05d}>"
        self._uart_write(msg.encode())

    def _uart_write(self, data: bytes):
        if not UART_ENABLED or not self._serial:
            return
        try:
            with self._uart_lock:      # 두 스레드가 한 패킷을 통째로 배타적으로 씀 (바이트 섞임 방지)
                self._serial.write(data)
                self._serial.flush()
        except Exception as e:
            print(f"[Bridge-{self.rid}] UART write error: {e}")

    # ─── UART 수신 스레드 (단일 바이트 이벤트) ────────────────────────────────

    def _uart_read_loop(self):
        """STM32 이벤트(단일 바이트) 수신 → cmd_ack 발행"""
        while self._running:
            try:
                b = self._serial.read(1) if self._serial else b""  # timeout=0.1로 블록 방지
            except Exception as e:
                print(f"[Bridge-{self.rid}] UART read error: {e}")
                time.sleep(0.1)
                continue
            if not b:
                continue
            self._handle_uart_event(b[0])

    def _handle_uart_event(self, event: int):
        """STM32 단일 바이트 이벤트 → cmd_ack 발행"""
        # [진단 2026-07-24] STM→브릿지 원시 이벤트 수신을 보이게 한다.
        #   지금까지 EVT_ACK 수신과 dedup/guard 드롭이 전부 조용해서, STM이 신호를 보냈는지
        #   브릿지가 버렸는지 로그로 구분이 안 됐다(ACK 유실/수신단 hang 추적 불가).
        #   이 줄로 STM이 실제로 뭘·언제 보냈는지가 다 찍힌다(3× 반복도 그대로 보인다).
        _evt_name = {EVT_DONE: "DONE", EVT_ACK: "ACK"}.get(event, f"?0x{event:02X}")
        print(f"[Bridge-{self.rid}] <- STM {_evt_name}")
        # ── 반복 이벤트 병합 (2026-07-18) ──────────────────────────────────────
        # STM이 EVT_DONE/EVT_ACK를 3회 반복 전송한다(위 EVENT_DEDUP_SECS 주석). 같은 값이
        # 창 안에 또 오면 반복분이므로 버린다 — 첫 바이트만 처리해 '유령 ack'을 막는다.
        # (앞 명령의 2·3번째 반복이 다음 명령 ack으로 새던 리프트 no-op의 근본 차단)
        now = time.time()
        last = self._evt_last_ts.get(event, 0.0)
        self._evt_last_ts[event] = now      # 반복이 이어지는 동안 창을 계속 갱신
        if now - last < EVENT_DEDUP_SECS:
            return
        if event == EVT_DONE:
            # ── 수신확인(EVT_ACK) 전에 온 DONE 은 버린다 (2026-07-18) ─────────────
            # STM 프로토콜은 항상 EVT_ACK(수신) → EVT_DONE(완료) 순서다. _pending_code 는
            # 명령을 arm할 때 코드로 세팅되고 EVT_ACK 가 와야 0 으로 풀린다. 즉 아직 !=0 이면
            # STM 이 '현재 명령을 받았다'고 알린 적조차 없는데 완료가 온 것 = 받은 적 없는
            # 명령을 완료했을 리 없으니 앞 명령의 잔여/유령 0x81 이다 → 버린다.
            # (dedup 창을 넘겨 새는 유령 ack까지 프로토콜 레벨에서 원천 차단. 실측: lift_up
            #  dispatch 후 _pending_code=3 그대로인데 0x81이 와 16ms 유령 ack이 터지던 문제)
            if self._pending_code != 0:
                print(f"[Bridge-{self.rid}]    → DONE 버림 (ACK 미수신, pending_code={self._pending_code})")
                return
            # forward 완료(2026-07-17): 바로 마커를 발행하지 않고 FORWARD_SETTLE_SECS 만큼 기다린
            #   뒤 발행한다(_forward_settle_publish). 모터 멈춘 직후 IMU yaw가 흔들려 곧바로 turn을
            #   내면 no-op이 되기 때문(위 상수 주석). 그동안 카메라 offset이 계속 흘러 yaw가 안정된다.
            #   STM은 EVT_DONE을 3회 반복 → 첫 번째만 타이머 예약(_fwd_settling 가드).
            if self._fwd_active and not self._fwd_settling:
                self._fwd_settling = True
                threading.Timer(FORWARD_SETTLE_SECS, self._forward_settle_publish).start()
            # turn/lift만 _last_cmd가 있어 cmd_ack 발행. forward는 _last_cmd=None → 스킵.
            cmd = self._last_cmd
            self._last_cmd = None
            if cmd is not None:
                if cmd in ("lift_up", "lift_down") and LIFT_SETTLE_SECS > 0:
                    # 리프트 물리 정착까지 기다린 뒤 ack (성급 출발 방지, LIFT_SETTLE_SECS 주석 참조).
                    # 서버는 이 ack 전엔 다음 명령을 안 내므로 = 리프트가 정착할 때까지 로봇이 안 움직인다.
                    threading.Timer(LIFT_SETTLE_SECS, self.publish_cmd_ack, args=(cmd,)).start()
                elif cmd in ("turn_left", "turn_right", "turn_180") and TURN_SETTLE_SECS > 0:
                    # 회전 오버슛이 목표각으로 정착할 때까지 기다린 뒤 ack (TURN_SETTLE_SECS 주석 참조).
                    threading.Timer(TURN_SETTLE_SECS, self.publish_cmd_ack, args=(cmd,)).start()
                else:
                    self.publish_cmd_ack(cmd)
        elif event == EVT_ACK:
            self._pending_code = 0

    def _forward_settle_publish(self):
        """forward EVT_DONE 후 안정 시간이 지나면 호출(Timer). 버퍼된 마커를 발행 → forward ACK.

        settle 동안 카메라가 계속 _fwd_marker를 갱신하므로, 여기서 발행하는 건 '안정된 뒤'의
        마커다. 발행 직전 _fwd_active=False로 풀어 이후 마커는 평소대로 즉시 발행되게 한다.
        """
        if not self._fwd_settling:
            return                      # 새 forward가 끼어들어 무효화됨 → 이 타이머는 버린다
        self._fwd_active = False
        self._fwd_settling = False
        buffered = self._fwd_marker
        self._fwd_marker = None
        if buffered is not None:
            self._publish_marker_now(*buffered)

    # ─── UART 초기화 ──────────────────────────────────────────────────────────

    # ─── 리프트 상태 동기화 (2026-07-21) ──────────────────────────────────────
    # STM의 lift_state는 리셋 시 무조건 0(내려감)으로 초기화되는데, 리프트를 올린 채
    # 껐다 켜면 '실제=위 / STM=아래'로 어긋난다. 그 상태에서 첫 lift_up은 이미 상단인
    # 리프트를 더 밀어올려 탈조만 하고(펄스는 나가므로 EVT_DONE은 정상 도착) 겉보기엔
    # "리프트를 안 했다"가 된다. 리밋스위치가 없어 STM 혼자서는 이 어긋남을 알 수 없다.
    #   → 정상 종료 시 내려둔다(park_lift). 비정상 종료(정전·pkill -9)는 코드로 막지
    #     않는다 — 시작할 때 올라간 리프트를 강제로 맞추려면 하드스톱을 밀어야 해서
    #     기구에 무리가 간다. 그 경우는 '손으로 내리고 STM 리셋'(무해·동일 결과)으로
    #     대응한다. STM은 리셋 시 lift_state=0이므로 그것만으로 동기화된다.

    def _wait_cmd_done(self, timeout: float) -> bool:
        """방금 arm한 명령의 EVT_DONE 대기. no-op이면 DONE이 안 와서 False."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._pending_code == 0 and self._last_cmd is None:
                return True
            time.sleep(0.05)
        return False

    def park_lift(self, timeout: float = 4.0):
        """종료 직전 리프트를 내려 다음 런의 lift_state(=0)와 물리 위치를 맞춘다.

        이미 내려가 있으면 STM의 `if (lift_state)` 가드에 걸려 no-op이라 EVT_DONE이
        오지 않는다 → timeout까지 기다렸다 그냥 진행한다(정상 경로).
        """
        if not UART_ENABLED or not self._serial:
            return
        print(f"[Bridge-{self.rid}] 종료 정리: lift_down")
        self._dispatch_cmd("lift_down", send_now=True)  # 카메라 루프 이미 정지 → 직접 송신
        if self._wait_cmd_done(timeout):
            print(f"[Bridge-{self.rid}] 리프트 내림 완료")
        else:
            print(f"[Bridge-{self.rid}] 리프트 이미 내려가 있음(no-op) — 진행")

    def open_uart(self):
        """UART 포트 열기 + 수신 스레드 시작"""
        if not UART_ENABLED:
            print(f"[Bridge-{self.rid}] UART_ENABLED=False, UART 비활성 모드")
            return
        try:
            import serial
            self._serial = serial.Serial(UART_PORT, UART_BAUD, timeout=0.1)
            self._running = True
            self._uart_thread = threading.Thread(
                target=self._uart_read_loop, daemon=True
            )
            self._uart_thread.start()
            print(f"[Bridge-{self.rid}] UART opened: {UART_PORT} @ {UART_BAUD}bps")
        except Exception as e:
            print(f"[Bridge-{self.rid}] UART open failed: {e}")


if __name__ == "__main__":
    import sys
    rid = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    b = Bridge(rid)
    b.open_uart()
    b.connect()
    print(f"[Bridge-{rid}] 실행 중... Ctrl+C로 종료")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        b.disconnect()

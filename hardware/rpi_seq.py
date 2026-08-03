"""
rpi_seq.py — 단일 스레드 순차 브릿지 (jw_headless 구조 그대로 + MQTT 큐)
TU Capstone Design - AGV 물류 피킹 시스템

  주원이 jw_headless.py(검증된 무오류 코드)를 뼈대로, 입력만 stdin → MQTT 명령 큐로 교체.
  기존 bridge_rpi.py(멀티스레드: 카메라 스레드 + UART 읽기 스레드 + MQTT 콜백 스레드)의
  스레드/타이밍 차이를 통째로 제거한다.

핵심 원칙 (jw_headless와 동일):
  1. **UART 읽기·쓰기가 한 루프 안** (별도 스레드 없음). MQTT paho 스레드는 큐에 담기만 함.
  2. **DONE 게이트**: 새 명령은 EVT_DONE을 받은 뒤에만 STM에 나간다 (waiting_for_cmd).
     STM은 부팅 시 DONE을 한 번 쏘므로(main.c) 첫 명령도 그걸로 열린다.
  3. **command 코드는 EVT_ACK를 받을 때까지 매 프레임 실려 나간다** (jw_headless L109와 동일).
     해제점은 EVT_ACK 하나뿐.
  4. **검출된 마커 전부**에 대해 프레임을 쓴다 (jw_headless L138 루프와 동일).
  5. **마커가 안 보이면 아무것도 안 보낸다** (jw_headless는 write가 검출 블록 안에만 있음).

  > 3~5는 "브릿지가 변수인가"를 없애기 위한 원본 정합성 조항이다. 우리 쪽 개선이라고
  > 생각했던 것들(1패킷 전송 / 첫 마커만 / 마커 없을 때 직전 오프셋)이 전부 원본과의
  > 차이를 만들고 있었고, 그 차이가 남아 있는 한 "STM이냐 브릿지냐"를 못 가린다.
  > 되살리고 싶으면 코드를 고치지 말고 env로: NO_MARKER_FALLBACK=1 (5번만 해제)

서버 연동 (bridge_rpi.py에서 최소한만 가져옴):
  - /agv/cmd 구독 → 명령을 큐에 push (paho 스레드)
  - /agv/marker 발행: 새 마커 감지 시 (서버의 도착 인지 = forward 완료 신호)
  - /agv/cmd_ack 발행: turn/lift 의 EVT_DONE 시 (forward는 marker로 대신)
  - /agv/presence: 접속/이탈 (retain + LWT)

진단 로그 — "펌웨어/서버/브릿지 중 누구 탓인가"를 로그만 보고 가르기 위한 것:
  SERVER  서버가 뭘 시켰나      = /agv/cmd 원문 JSON + 수신시각 + 큐 깊이
  GATE    브릿지가 얼마나 붙들었나 = DONE→발행 지연, 정착 대기 시간
  TX      브릿지가 뭘 보냈나     = STM에 나간 바이트 원문 + 몇 번째 패킷
  RX      STM이 어떻게 반응했나  = ACK/DONE 도착 시각차
  POSE    실제로 움직였나       = 명령 전후 카메라 실측 (회전각·도착 마커)
  → 명령이 틀렸다=서버 / 원문이 안 나갔거나 늦었거나 두 번 나갔다=브릿지 /
    제때 정확히 나갔는데 실측이 틀리다=펌웨어

실행:  AGV_ID=1 python3 -m hardware.rpi_seq [--preview]
       로그: stdout(사람용) + ~/rpi_seq_{rid}_MMDD_HHMM.jsonl(기계용, 분석기 입력)
"""

import os
import sys
import time
import json
import pickle
import queue

import cv2
import numpy as np
import serial
import paho.mqtt.client as mqtt
from picamera2 import Picamera2

from hardware.config import (
    MQTT_HOST, MQTT_PORT,
    TOPIC_CMD, TOPIC_MARKER, TOPIC_CMD_ACK, TOPIC_CMD_START, TOPIC_PRESENCE,
    UART_PORT, UART_BAUD, CALIB_FILE,
)

# ─── 명령/이벤트 코드 (주원이 rpi_uart.h와 동일) ───
CMD_CODE = {
    "forward": 1, "stop": 2, "lift_up": 3, "lift_down": 4,
    "turn_left": 5, "turn_right": 6, "turn_180": 7,
}
ACK_CMDS = ("turn_left", "turn_right", "turn_180", "lift_up", "lift_down")  # cmd_ack 발행 대상
EVT_DONE = 0x81
EVT_ACK = 0xFF

# 명령 정착 대기: STM이 DONE을 보낸(=로봇이 멈춘) 시점부터 이 시간이 지나야 다음 명령을
# STM에 내보낸다 (어떤 명령이든). 그동안 루프는 계속 돌아 로봇이 안정되고, 그 뒤 안정된
# offset과 함께 명령이 나간다 → 회전/직진 목표가 흔들린 자세에서 정해지는 것을 막는다.
# env로 튜닝: SEND_SETTLE_SECS.  (jw_headless에는 없는 유일한 전송 측 차이 — 사람이 손으로
# 칠 때의 타이핑 지연에 해당하는 자리라, 0으로 두면 원본보다 오히려 급해진다)
SETTLE_SECS = float(os.environ.get("SEND_SETTLE_SECS", "1.2"))

# jw_headless 동일화 해제 스위치: 1이면 마커를 놓쳤을 때 직전 오프셋으로 명령을 내보낸다.
# 기본 0 = 원본과 동일(마커 없으면 침묵). 켜면 원본과 달라지므로 "브릿지가 변수인가"를
# 검증하는 동안은 0으로 둘 것. (2026-07-26 실측: 노드 33에서 마커가 시야를 벗어나
#  forward가 6.8초간 안 나감 — 이 스위치가 그 상황을 메우지만, 원본에는 없는 동작이다)
NO_MARKER_FALLBACK = os.environ.get("NO_MARKER_FALLBACK", "0") == "1"

# STM으로 나가는 프레임을 **하나도 빠짐없이** 찍는다 (carrier `<0,...>` 포함).
# 명령 프레임(CMD)과 carrier가 나란히 보여야 "EVT_ACK 받고 command를 0으로 되돌렸나 /
# EVT_DONE 뒤에 다시 실었나"가 로그만으로 증명된다 — 주원이 물어본 그 지점.
# 화면이 시끄러우면 TX_LOG_CARRIER=0 (명령 프레임은 그래도 전부 찍힌다).
# 명령 프레임만 보려면: grep 'CMD #'
TX_LOG_CARRIER = os.environ.get("TX_LOG_CARRIER", "1") == "1"

# 회전 실측 로그용 (판정 전용 — 거동에는 일절 개입하지 않는다)
TURN_DELTA = {"turn_left": -90.0, "turn_right": +90.0, "turn_180": 180.0}
FRESH_SECS = 1.0        # 이 시간 안에 본 마커만 '지금 자세'로 인정


def _ts() -> str:
    """벽시계 시:분:초.밀리 — 서버/STM 로그와 시각을 맞추기 위해 모든 줄에 붙인다."""
    t = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int(t*1000)%1000:03d}"


def _angdiff(a: float, b: float) -> float:
    """a-b를 -180~+180으로 정규화 (359°와 1°의 차이를 2°로 본다)."""
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def _rid() -> int:
    try:
        return int(os.environ.get("AGV_ID", "1"))
    except ValueError:
        return 1


def run(rid: int, preview: bool = False):
    # ─── 로그 (사람용 stdout + 기계용 JSONL) ───
    jpath = os.path.expanduser(time.strftime(f"~/rpi_seq_{rid}_%m%d_%H%M.jsonl"))
    jf = open(jpath, "a", buffering=1)

    def say(msg: str):
        print(f"[{_ts()}][Seq-{rid}] {msg}", flush=True)

    def jlog(event: str, **kw):
        try:
            jf.write(json.dumps({"t": round(time.time(), 3), "ev": event, **kw},
                                ensure_ascii=False) + "\n")
        except Exception:
            pass

    say(f"기계용 로그: {jpath}")

    # ─── MQTT (paho 스레드는 큐 push / publish 만; UART는 절대 안 건드림) ───
    cmd_queue = queue.Queue()

    client = mqtt.Client(client_id=f"seq_{rid}_{os.getpid()}")
    client.will_set(TOPIC_PRESENCE,
                    json.dumps({"rid": rid, "online": False}),
                    qos=1, retain=True)

    def on_connect(c, u, flags, rc):
        c.subscribe(TOPIC_CMD)
        c.publish(TOPIC_PRESENCE, json.dumps({"rid": rid, "online": True}),
                  qos=1, retain=True)
        say(f"MQTT connected (rc={rc}) — presence online")
        jlog("mqtt_connect", rc=rc)

    def on_message(c, u, msg):
        raw = msg.payload.decode(errors="replace")
        try:
            data = json.loads(raw)
        except Exception:
            jlog("cmd_bad", raw=raw)
            say(f"WARN 파싱 실패한 명령 무시: {raw!r}")
            return
        if int(data.get("rid", -1)) != rid or not data.get("cmd"):
            return
        # ★[서버 판별] 서버가 보낸 원문을 그대로 남긴다. 나중에 "서버가 엉뚱한 걸 시켰나"를
        #   따질 때 이 줄이 유일한 증거다. target_node는 forward에만 실려온다(수정 70).
        item = (data["cmd"], data.get("target_node"), time.time())
        cmd_queue.put(item)
        say(f"SERVER <- {raw}  (큐 깊이 {cmd_queue.qsize()})")
        jlog("cmd_recv", cmd=data["cmd"], target=data.get("target_node"),
             raw=raw, qdepth=cmd_queue.qsize())

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    def publish_marker(marker_id: int):
        client.publish(TOPIC_MARKER,
                       json.dumps({"rid": rid, "marker_id": int(marker_id),
                                   "ts": int(time.time())}))
        say(f"MQTT -> /agv/marker id={marker_id}")
        jlog("mqtt_out", topic="marker", marker_id=int(marker_id))

    def publish_cmd_ack(name: str):
        client.publish(TOPIC_CMD_ACK,
                       json.dumps({"type": "cmd_ack", "rid": rid,
                                   "cmd": name, "status": "done"}))
        say(f"MQTT -> /agv/cmd_ack cmd={name}")
        jlog("mqtt_out", topic="cmd_ack", cmd=name)

    def publish_cmd_start(name: str):
        # 트윈 전용: STM이 명령을 받아 실제로 동작을 시작한 시점(EVT_ACK) 알림.
        # 트윈이 이 시각에 애니메이션을 시작해 실물과 출발을 맞춘다. 서버는 구독 안 함.
        client.publish(TOPIC_CMD_START,
                       json.dumps({"rid": rid, "cmd": name}))
        jlog("mqtt_out", topic="cmd_start", cmd=name)

    # ─── 카메라 (jw_headless와 동일한 셋업/검출) ───
    with open(CALIB_FILE, "rb") as f:
        calib = pickle.load(f)
    camera_matrix = calib["camera_matrix"]
    dist_coeffs = calib["dist_coeffs"]

    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    detector = cv2.aruco.ArucoDetector(aruco_dict, cv2.aruco.DetectorParameters())

    marker_size = 15
    half = marker_size / 2
    marker_3d_edges = np.array([[-half, half, 0], [half, half, 0],
                                [half, -half, 0], [-half, -half, 0]], dtype="float32")

    picam2 = Picamera2()
    picam2.configure(picam2.create_preview_configuration())
    picam2.start()
    time.sleep(2)

    frame0 = picam2.capture_array()
    h, w = frame0.shape[:2]
    map1, map2 = cv2.initUndistortRectifyMap(
        camera_matrix, dist_coeffs, None, camera_matrix, (w, h), cv2.CV_16SC2)

    prev_web = None
    mp = None
    if preview:
        try:
            import hardware.mjpeg_preview as mp
            prev_web = mp.start_preview_server(8000)
            say("웹 프리뷰 -> http://<라파IP>:8000")
        except Exception as e:
            say(f"프리뷰 비활성: {e}")
            mp = None

    # ─── UART (단일 스레드: 이 루프가 읽기+쓰기 다 함. timeout은 jw_headless와 동일) ───
    uart = serial.Serial(UART_PORT, UART_BAUD, timeout=1)
    say(f"UART {UART_PORT}@{UART_BAUD} — 단일 스레드 순차 브릿지 시작 "
        f"(SETTLE={SETTLE_SECS}s, NO_MARKER_FALLBACK={int(NO_MARKER_FALLBACK)})")
    jlog("start", uart=UART_PORT, baud=UART_BAUD, settle=SETTLE_SECS,
         no_marker_fallback=NO_MARKER_FALLBACK)

    # ─── 상태 (jw_headless의 command/waiting_for_cmd 그대로) ───
    command = 0                 # STM으로 스트림하는 코드 (0=carrier). EVT_ACK에서만 0으로 복귀
    waiting_for_cmd = False     # EVT_DONE 받고 다음 명령을 기다리는 중? (부팅 DONE이 True로 연다)
    cur_name = None             # 현재 in-flight 명령 이름 (cmd_ack 발행/dedup용)
    cur_target = None           # 그 명령의 target_node (forward에만 있음)
    start_pending = False       # 이번 명령의 /agv/cmd_start(트윈 시작신호)를 아직 안 보냈나
    prev_marker = None          # 마커 발행 dedup (새 마커일 때만 발행)
    done_ts = time.time()       # 마지막 EVT_DONE(=로봇 멈춤) 시각 — 정착 게이트 기준

    # ─── 진단용 상태 ───
    recv_ts = 0.0               # 그 명령을 서버에서 받은 시각
    dispatch_ts = 0.0           # 현재 명령을 STM에 보내기로 결정한 시각
    ack_ts = 0.0                # 그 명령의 EVT_ACK 도착 시각
    tx_count = 0                # 이번 명령이 STM으로 나간 패킷 수 (원본은 ACK까지 반복)
    tx_first_yaw = tx_last_yaw = 0.0
    yaw_at_dispatch = None      # 명령 발행 시점 카메라 yaw (회전 실측용)
    mid_at_dispatch = None
    stall_warned = False        # 오래 걸린다는 경고를 이미 냈나
    last_status_ts = 0.0        # 주기 상태로그 시각
    last_seen_ts = 0.0          # 마커를 마지막으로 본 시각
    multi_warn_ts = 0.0         # 다중검출 경고 도배 방지
    nomark_warn_ts = 0.0        # '마커 없어서 보류' 경고를 마지막으로 낸 시각
    cur_x = cur_y = cur_yaw = 0.0
    cur_mid = None
    frames = 0                  # 총 프레임
    frames_seen = 0             # 마커가 잡힌 프레임

    try:
        while True:
            now = time.time()

            # 1) UART 이벤트 (단일 스레드 읽기)
            if uart.in_waiting > 0:
                ev = uart.read(1)[0]
                # 원시 이벤트 로그: STM이 보낸 바이트를 하나하나 다 찍는다(3회 반복도 그대로).
                # hang 판별용 — STM이 아무것도 안 보냈나 vs 보냈나가 여기서 구분된다.
                ev_name = {EVT_DONE: "DONE", EVT_ACK: "ACK"}.get(ev, f"?0x{ev:02X}")
                say(f"RX   <- STM {ev_name}")
                jlog("rx", name=ev_name, byte=ev, cmd=cur_name)

                if ev == EVT_DONE:
                    waiting_for_cmd = True
                    if dispatch_ts and cur_name is not None:
                        tot = now - dispatch_ts
                        a2d = (now - ack_ts) if ack_ts else -1.0
                        fresh = (now - last_seen_ts) < FRESH_SECS

                        # ★[펌웨어 판별] 실제로 얼마나 돌았나 — 카메라는 IMU와 독립인 실측기라
                        #   STM 로그(USART3) 없이도 회전량을 잴 수 있다. 지시각과 벌어지면 펌웨어.
                        turn_actual = turn_err = None
                        want = TURN_DELTA.get(cur_name)
                        if want is not None and yaw_at_dispatch is not None and fresh:
                            turn_actual = _angdiff(cur_yaw, yaw_at_dispatch)
                            turn_err = _angdiff(turn_actual, want)

                        # ★[서버/펌웨어 판별] 서버가 가라던 노드에 실제로 도착했나
                        arrived_ok = None
                        if cur_name == "forward" and cur_target is not None and fresh:
                            arrived_ok = (cur_mid == cur_target)

                        line = (f"DONE {cur_name} 총 {tot:.2f}s (ACK후 {a2d:.2f}s), "
                                f"패킷 {tx_count}개, mk={cur_mid} "
                                f"x={cur_x:+.1f} y={cur_y:+.1f} yaw={cur_yaw:.1f}")
                        if turn_actual is not None:
                            line += (f"  ROT 실측 {turn_actual:+.1f}° "
                                     f"(지시 {want:+.0f}°, 오차 {turn_err:+.1f}°)")
                        if arrived_ok is False:
                            line += f"  WARN 서버 target={cur_target} 인데 도착 mk={cur_mid}"
                        if not fresh:
                            line += "  WARN 마커 오래됨 — 실측 신뢰 불가"
                        say(line)
                        jlog("done", cmd=cur_name, dt_dispatch=round(tot, 3),
                             dt_ack=round(a2d, 3), packets=tx_count,
                             mid=cur_mid, x=cur_x, y=cur_y, yaw=cur_yaw,
                             turn_actual=turn_actual, turn_expect=want,
                             turn_err=turn_err, target=cur_target,
                             arrived_ok=arrived_ok, fresh=fresh)
                        dispatch_ts = 0.0
                        stall_warned = False

                    done_ts = now   # 정착 게이트 기준: 여기서부터 SETTLE_SECS 뒤에 다음 명령
                    # turn/lift 완료는 cmd_ack로. forward 완료는 marker가 대신 → 스킵.
                    # cur_name을 여기서 None으로 지우므로 3회 반복 DONE에도 한 번만 발행됨.
                    if cur_name in ACK_CMDS:
                        publish_cmd_ack(cur_name)
                    cur_name = None
                    cur_target = None
                    yaw_at_dispatch = None

                elif ev == EVT_ACK:
                    # ★[브릿지 판별] 이 명령이 몇 패킷 나갔고, 그동안 yaw가 얼마나 흔들렸나.
                    #   패킷 수가 크다 = ACK가 늦게/유실돼 STM이 같은 명령을 여러 번 봤다
                    #   첫 yaw != 마지막 yaw = 명령 시점에 자세가 안 정착 (SETTLE 부족)
                    #   둘 다 정상인데 각도가 틀리면 → 브릿지 무죄, STM(dyaw/IMU) 쪽
                    if ack_ts == 0.0:
                        ack_ts = now
                        d = (now - dispatch_ts) if dispatch_ts else -1.0
                        say(f"ACK  cmd={cur_name} 발행->ACK {d:.3f}s, 전송 {tx_count}패킷, "
                            f"yaw {tx_first_yaw:.1f} → {tx_last_yaw:.1f}"
                            f"  → command=0 초기화 (이후 carrier만 나가야 정상)")
                        jlog("ack", cmd=cur_name, dt_dispatch=round(d, 3),
                             packets=tx_count, yaw_first=tx_first_yaw,
                             yaw_last=tx_last_yaw)
                    # ★ 해제점은 여기 하나뿐 (jw_headless L109와 동일)
                    command = 0
                    # 트윈에 "실물이 지금 동작 시작" 알림 (명령당 1회, ACK 3회 반복 방어)
                    if start_pending and cur_name is not None:
                        publish_cmd_start(cur_name)
                        start_pending = False

            # 2) DONE 게이트 + 정착 게이트: DONE 받았고 + 멈춘 지 SETTLE_SECS 지났고 + 큐에 명령 있으면 시작
            if (waiting_for_cmd and not cmd_queue.empty()
                    and now - done_ts >= SETTLE_SECS):
                name, target, rts = cmd_queue.get()
                code = CMD_CODE.get(name)
                if code is None:
                    say(f"WARN 모르는 명령 무시: {name}")
                    jlog("cmd_unknown", cmd=name)
                else:
                    command = code        # EVT_ACK까지 매 프레임 실려 나감 (원본과 동일)
                    cur_name = name
                    cur_target = target
                    recv_ts = rts
                    start_pending = True  # EVT_ACK 오면 트윈에 시작신호 1회 발행
                    tx_count = 0
                    dispatch_ts = now
                    ack_ts = 0.0
                    stall_warned = False
                    waiting_for_cmd = False
                    yaw_at_dispatch = cur_yaw if (now - last_seen_ts) < FRESH_SECS else None
                    mid_at_dispatch = cur_mid
                    # ★[브릿지 판별] 서버 수신 → 실제 발행까지 브릿지가 얼마나 붙들었나.
                    #   held가 크면 원인은 정착 게이트(settle) 아니면 앞 명령이 안 끝난 것.
                    say(f"GATE dispatch {name}(code={code})"
                        + (f" target={target}" if target is not None else "")
                        + f"  서버수신후 {now - rts:.2f}s, DONE후 {now - done_ts:.2f}s, "
                        + f"출발 yaw={yaw_at_dispatch if yaw_at_dispatch is None else round(yaw_at_dispatch,1)} mk={mid_at_dispatch}")
                    jlog("dispatch", cmd=name, code=code, target=target,
                         held=round(now - rts, 3), after_done=round(now - done_ts, 3),
                         yaw0=yaw_at_dispatch, mid0=mid_at_dispatch)

            # 3-0) 주기 상태 로그 + 멈춤 감지
            #   명령과 명령 사이가 로그 공백이었다. 0.5초마다 현재 위치·자세를 찍어서
            #   "회전 중인가 / 멈췄나 / 옆으로 밀리나"가 보이게 한다.
            if now - last_status_ts >= 0.5:
                last_status_ts = now
                busy = ""
                if dispatch_ts:
                    el = now - dispatch_ts
                    busy = f"  [{cur_name} 진행 {el:.1f}s, tx {tx_count}]"
                    # 명령이 10초 넘게 안 끝나면 = STM이 완료 판정을 못 하는 것
                    if el > 10.0 and not stall_warned:
                        stall_warned = True
                        why = ("ACK조차 안 옴 → STM이 프레임을 못 받았거나 버림"
                               if ack_ts == 0.0 else "ACK는 왔는데 DONE이 안 옴 → 목표 미달/물리적 걸림")
                        say(f"STALL 멈춤 의심: '{cur_name}' {el:.1f}초째 DONE 없음. {why}")
                        jlog("stall", cmd=cur_name, elapsed=round(el, 1),
                             acked=(ack_ts != 0.0), packets=tx_count)
                det = f"{frames_seen}/{frames}" if frames else "0/0"
                say(f".    mk={cur_mid} x={cur_x:+6.1f} y={cur_y:+6.1f} "
                    f"yaw={cur_yaw:6.1f}  검출 {det}{busy}")
                jlog("status", mid=cur_mid, x=cur_x, y=cur_y, yaw=cur_yaw,
                     seen=frames_seen, frames=frames, cmd=cur_name, tx=tx_count)
                frames = frames_seen = 0

            # 3) 카메라 검출 (jw_headless 그대로)
            frames += 1
            frame = picam2.capture_array()
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            und = cv2.remap(frame_rgb, map1, map2, cv2.INTER_LINEAR)
            corners, ids, _ = detector.detectMarkers(und)

            if ids is not None and len(corners) > 0:
                frames_seen += 1
                last_seen_ts = now
                marker_ids = np.ravel(ids)
                if len(corners) > 1 and now - multi_warn_ts >= 1.0:
                    # 매 프레임 찍으면 초당 15~26줄 도배 → 1초에 한 번만
                    multi_warn_ts = now
                    say(f"WARN 마커 {len(corners)}개 동시검출: "
                        f"{[int(m) for m in marker_ids]} "
                        f"— 원본과 동일하게 전부 전송한다(옆칸 마커면 이게 원인)")
                    jlog("multi_marker", ids=[int(m) for m in marker_ids])

                # ★ jw_headless L138: 검출된 마커마다 프레임을 쓴다.
                #   마커가 둘이면 한 카메라 프레임에 두 패킷이 나가고, 명령 코드도 둘 다 실린다.
                #   위험해 보이지만 원본이 그렇다 — 차이를 만들지 않는 게 지금 목적이다.
                for i, c in enumerate(corners):
                    corner = np.array(c).reshape((4, 2))
                    _, rvec, tvec = cv2.solvePnP(marker_3d_edges, corner,
                                                 camera_matrix, dist_coeffs)
                    x = -round(float(tvec[0][0]), 1)
                    y = round(float(tvec[1][0]), 1)
                    R, _ = cv2.Rodrigues(rvec)
                    yaw = np.arctan2(R[1, 0], R[0, 0])
                    yaw_deg = -round(float(np.rad2deg(yaw)), 1)
                    if yaw_deg < 0.0:
                        yaw_deg += 360.0
                    elif yaw_deg >= 360.0:
                        yaw_deg -= 360.0

                    mid = int(marker_ids[i])
                    if i == 0:
                        # 우리 자세/서버 보고는 첫 마커 기준 (원본은 서버가 없어서 이 개념이 없다)
                        cur_mid, cur_x, cur_y, cur_yaw = mid, x, y, yaw_deg
                        if mid != prev_marker:
                            publish_marker(mid)
                            prev_marker = mid

                    msg = f"<{command},{int(x * 10):+05d},{int(y * 10):+05d},{int(yaw_deg * 10):+05d}>"
                    uart.write(msg.encode())

                    # ★[브릿지 판별] STM으로 나간 바이트 원문 그대로. 명령 프레임은 ★,
                    #   나머지는 carrier. 이 줄들이 있으면 "브릿지가 안 보냈다"도,
                    #   "ACK 후에도 명령을 계속 보냈다"도 즉시 판명된다.
                    if command != 0:
                        if tx_count == 0:
                            tx_first_yaw = yaw_deg
                        tx_last_yaw = yaw_deg
                        tx_count += 1
                        say(f"TX CMD #{tx_count} {msg}  cmd={cur_name} mk={mid}")
                        jlog("tx", n=tx_count, frame=msg, cmd=cur_name,
                             mid=mid, x=x, y=y, yaw=yaw_deg)
                    elif TX_LOG_CARRIER:
                        say(f"TX carrier {msg}  (mk={mid})")

                if prev_web is not None:
                    cv2.putText(und, f"ID:{cur_mid} ({cur_x},{cur_y}) {cur_yaw}deg cmd={command}",
                                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            else:
                # ★ jw_headless는 여기서 아무것도 안 보낸다 (write가 검출 블록 안에만 있음).
                #   명령이 걸려 있으면 마커가 다시 보일 때까지 그대로 대기한다 — 그동안
                #   command는 살아 있으므로(ACK 전) 마커가 돌아오는 순간 나간다.
                #   그 '보류' 구간이 눈에 보여야 하므로 1초에 한 번 찍는다.
                if command != 0:
                    if NO_MARKER_FALLBACK:
                        msg = (f"<{command},{int(cur_x * 10):+05d},"
                               f"{int(cur_y * 10):+05d},{int(cur_yaw * 10):+05d}>")
                        uart.write(msg.encode())
                        if tx_count == 0:
                            tx_first_yaw = cur_yaw
                        tx_last_yaw = cur_yaw
                        tx_count += 1
                        say(f"TX CMD #{tx_count} {msg}  WARN 마커 없음, 직전 오프셋 (원본과 다름)")
                        jlog("tx", n=tx_count, frame=msg, cmd=cur_name,
                             mid=None, stale=True)
                    elif now - nomark_warn_ts >= 1.0:
                        nomark_warn_ts = now
                        held = now - dispatch_ts if dispatch_ts else 0.0
                        say(f"HOLD 마커 없음 — '{cur_name}' 보류 {held:.1f}s "
                            f"(원본 동작. 되살리려면 NO_MARKER_FALLBACK=1)")
                        jlog("hold_no_marker", cmd=cur_name, held=round(held, 1))

            if prev_web is not None and mp is not None:
                try:
                    mp.push_frame(prev_web, und)
                except Exception:
                    pass

    except KeyboardInterrupt:
        say("종료 중...")
    finally:
        # 종료 정리: 리프트 내리기 (단일 스레드라 여기서 직접 보내도 충돌 없음)
        try:
            say("종료 정리: lift_down")
            t0 = time.time()
            while time.time() - t0 < 3.0:
                uart.write(f"<{CMD_CODE['lift_down']},{0:+05d},{0:+05d},{0:+05d}>".encode())
                if uart.in_waiting > 0 and uart.read(1)[0] == EVT_DONE:
                    break
                time.sleep(0.1)
        except Exception:
            pass
        client.publish(TOPIC_PRESENCE, json.dumps({"rid": rid, "online": False}),
                       qos=1, retain=True)
        client.loop_stop()
        client.disconnect()
        picam2.stop()
        uart.close()
        jlog("stop")
        jf.close()
        say(f"종료 완료 — 기계용 로그: {jpath}")


if __name__ == "__main__":
    run(_rid(), preview=("--preview" in sys.argv))

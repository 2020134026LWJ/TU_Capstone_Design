"""drive_jw.py — jw_headless를 '사람 대신' 타이핑해서 몰아주는 드라이버
TU Capstone Design - AGV 물류 피킹 시스템

**왜 만들었나 (2026-07-25)**
  실물에서 rpi_seq(우리 브릿지)로 돌리면 2~3번째 명령에서 로봇이 튀는데,
  jw_headless(주원이 검증된 코드)에 사람이 직접 치면 안 그런다.
  코드를 아무리 대조해도 차이를 못 찾았다 → **대조를 포기하고 변수를 없앤다.**

  이 도구는 jw_headless를 **하나도 안 고치고** 그대로 subprocess로 띄운 뒤,
  stdin에 숫자를 써넣는다. 즉 UART·카메라·STM에 닿는 코드가 **바이트 단위로 사람이
  쓰던 그것과 동일**하고, 바뀌는 건 "누가 타이핑하는가" 하나뿐이다.

  결과 해석:
    · 여기서도 튄다  → 브릿지 무죄. 사람이 느리게 치는 것 말고는 차이가 없으므로
                      원인은 STM(또는 '명령 간격') 쪽으로 확정된다.
    · 여기선 안 튄다 → rpi_seq에 아직 못 찾은 차이가 있다. --settle을 줄여가며
                      어느 간격부터 재현되는지 이분탐색하면 그 차이가 시간임이 드러난다.

**동작**
  jw_headless가 동작을 마치면 stdout에 `EVT_DONE ...` 프롬프트를 찍는다.
  그걸 보고 --settle 초 기다린 뒤 다음 명령 숫자를 stdin에 써넣는다.
  (STM은 이벤트를 3회 반복 전송 → DONE이 여러 줄 찍힌다. 마지막 DONE 이후로
   --settle이 지나야 보내므로 자연히 하나로 합쳐진다. rpi_seq의 done_ts 게이트와 같은 논리.)

**실행 (라파에서)**
    python3 tools/drive_jw.py --script ~/jw_headless.py
    python3 tools/drive_jw.py --script ~/jw_headless.py --settle 3.0 --repeat 3
    python3 tools/drive_jw.py --script ~/jw_headless.py --seq forward,forward,turn_180

  ⚠ 서버(server.main)와 rpi_seq는 **끄고** 돌릴 것. UART/카메라는 한 프로세스만 잡는다.
  ⚠ jw_headless 켠 다음에 STM 리셋 (부팅 DONE으로 첫 명령이 열린다).
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

# MQTT 모드에서만 필요 (없으면 --seq 모드는 그대로 동작)
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

_HERE = os.path.dirname(os.path.abspath(__file__))

TOPIC_CMD = "/agv/cmd"
TOPIC_MARKER = "/agv/marker"
TOPIC_CMD_ACK = "/agv/cmd_ack"
TOPIC_PRESENCE = "/agv/presence"
ACK_CMDS = ("turn_left", "turn_right", "turn_180", "lift_up", "lift_down")

CMD_CODE = {
    "forward": 1, "stop": 2, "lift_up": 3, "lift_down": 4,
    "turn_left": 5, "turn_right": 6, "turn_180": 7,
}

# 기본 시퀀스: 직진으로 나갔다 180도 돌아 돌아오기.
# 드리프트가 쌓이는지 눈으로 보기 제일 좋고, --repeat으로 계속 반복된다.
DEFAULT_SEQ = "forward,forward,forward,turn_180,forward,forward,forward,turn_180"

DONE_MARK = "EVT_DONE"     # jw_headless / opencv_aruco 가 완료 시 찍는 문자열


def _ts() -> str:
    now = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(now))}.{int(now * 1000) % 1000:03d}"


def _reader(stream, q):
    """자식 프로세스 stdout을 줄 단위로 읽어 큐에 넣기만 한다(블로킹 방지)."""
    for line in iter(stream.readline, ""):
        q.put(line.rstrip("\n"))
    q.put(None)


def main():
    p = argparse.ArgumentParser(
        description="jw_headless를 사람 대신 타이핑해서 몰아주는 드라이버")
    p.add_argument("--script", required=True,
                   help="jw_headless.py 경로 (수정하지 않고 그대로 띄운다)")
    p.add_argument("--seq", default=DEFAULT_SEQ,
                   help=f"명령 시퀀스, 쉼표 구분 (기본: {DEFAULT_SEQ})")
    p.add_argument("--settle", type=float, default=2.0,
                   help="DONE 후 다음 명령까지 대기(초). 사람의 타이핑 지연에 해당. 기본 2.0")
    p.add_argument("--repeat", type=int, default=1,
                   help="시퀀스 반복 횟수 (기본 1)")
    p.add_argument("--out", default=None, help="로그 저장 경로 (기본 ~/drive_jw_MMDD_HHMM.log)")
    p.add_argument("--mqtt", action="store_true",
                   help="고정 시퀀스 대신 **서버 명령**(/agv/cmd)을 받아서 타이핑한다")
    p.add_argument("--host", default="UB-Region5.local", help="MQTT 브로커 (--mqtt 모드)")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--rid", type=int, default=int(os.environ.get("AGV_ID", "1")),
                   help="이 AGV 번호 (기본: 환경변수 AGV_ID)")
    p.add_argument("--home", type=int, default=None,
                   help="시작 노드. --mqtt 모드에서 켜자마자 한 번 마커로 보고한다 "
                        "(생략 시 server/data/robot_config.json 의 home_node)")
    args = p.parse_args()

    names = [s.strip() for s in args.seq.split(",") if s.strip()]
    bad = [n for n in names if n not in CMD_CODE]
    if bad:
        print(f"알 수 없는 명령: {bad}\n사용 가능: {list(CMD_CODE)}", file=sys.stderr)
        return 1
    plan = names * max(1, args.repeat)

    out_path = args.out or os.path.expanduser(
        time.strftime("~/drive_jw_%m%d_%H%M.log"))
    log = open(out_path, "a", buffering=1)

    def emit(msg):
        line = f"[{_ts()}] {msg}"
        print(line, flush=True)
        log.write(line + "\n")

    emit(f"=== drive_jw 시작 — {args.script}")
    if args.mqtt:
        emit(f"=== MQTT 모드: 서버 명령을 받아 타이핑 (rid={args.rid}, settle {args.settle}s)")
    else:
        emit(f"=== 시퀀스 {len(plan)}개 (settle {args.settle}s): {' '.join(plan)}")
    emit("=== 지금 STM을 리셋하세요 (부팅 DONE을 받아야 첫 명령이 나갑니다)")

    # ─── MQTT 모드: 서버 ↔ 드라이버 배선 ───────────────────────────────
    #   받는다: /agv/cmd (내 rid만)  → 큐 → jw_headless stdin 에 숫자로 타이핑
    #   보낸다: forward 완료 → /agv/marker (marker_id = 서버가 준 target_node)
    #           turn/lift 완료 → /agv/cmd_ack
    #
    #   ⚠ jw_headless 는 마커 ID 를 출력하지 않는다. 그래서 '실제로 어디 도착했는지'가
    #     아니라 **서버가 가라고 한 곳(target_node)** 을 도착으로 되돌려준다.
    #     이 도구의 목적은 "사람이 칠 때와 같은 코드로 로봇이 제대로 움직이나"를 보는 것이고,
    #     제대로 갔는지는 **눈으로** 판정한다. 위치 검증용이 아니다.
    mqtt_cli = None
    server_q = queue.Queue()
    if args.mqtt:
        if mqtt is None:
            emit("!! paho-mqtt 가 없습니다: sudo apt install python3-paho-mqtt")
            return 1
        mqtt_cli = mqtt.Client(client_id=f"drivejw_{args.rid}_{os.getpid()}")
        mqtt_cli.will_set(TOPIC_PRESENCE,
                          json.dumps({"rid": args.rid, "online": False}),
                          qos=1, retain=True)

        def _on_connect(c, u, f, rc):
            c.subscribe(TOPIC_CMD)
            c.publish(TOPIC_PRESENCE, json.dumps({"rid": args.rid, "online": True}),
                      qos=1, retain=True)
            emit(f"=== MQTT 연결됨 (rc={rc}) — presence online")

        def _on_message(c, u, msg):
            try:
                d = json.loads(msg.payload.decode())
            except Exception:
                return
            if int(d.get("rid", -1)) == args.rid and d.get("cmd"):
                server_q.put((d["cmd"], d.get("target_node")))

        mqtt_cli.on_connect = _on_connect
        mqtt_cli.on_message = _on_message
        mqtt_cli.connect(args.host, args.port, 60)
        mqtt_cli.loop_start()

        # ★ 시작 마커 (없으면 서버가 태스크를 절대 안 준다)
        #   서버는 heading_initialized 인 로봇에만 배정하는데, 그건 **첫 마커 보고**로 켜진다
        #   (managers/robot.py get_available_robot). rpi_seq 는 카메라가 홈 마커를 보자마자
        #   보고해서 저절로 켜졌지만, jw_headless 는 마커 ID 를 출력하지 않아 우리가 알 수 없다.
        #   → 켤 때 로봇이 홈에 있다는 전제로 한 번 보고해 부트스트랩한다.
        #   (안 하면 "주문 넣었는데 아무 명령도 안 나감" = 닭과 달걀 교착)
        home = args.home
        if home is None:
            try:
                cfg = os.path.join(_HERE, "..", "server", "data", "robot_config.json")
                with open(cfg, encoding="utf-8") as f:
                    home = json.load(f)["robots"][str(args.rid)]["home_node"]
            except Exception as e:
                emit(f"!! home 노드를 알 수 없습니다({e}). --home 으로 직접 주세요")
                return 1
        time.sleep(1.0)     # 연결/구독 자리잡을 시간
        mqtt_cli.publish(TOPIC_MARKER, json.dumps(
            {"rid": args.rid, "marker_id": int(home), "ts": int(time.time())}))
        emit(f"=== 시작 마커 보고: node {home} (heading 초기화용). "
             f"로봇이 실제로 여기 있어야 합니다")

    def feedback(name, target):
        """방금 끝난 명령을 서버에 보고 → 서버가 다음 명령을 낸다."""
        if mqtt_cli is None:
            return
        if name == "forward":
            if target is None:
                emit("  !! forward 인데 target_node 가 없어 도착 보고 불가")
                return
            mqtt_cli.publish(TOPIC_MARKER, json.dumps(
                {"rid": args.rid, "marker_id": int(target), "ts": int(time.time())}))
            emit(f"  → /agv/marker id={target}")
        elif name in ACK_CMDS:
            mqtt_cli.publish(TOPIC_CMD_ACK, json.dumps(
                {"type": "cmd_ack", "rid": args.rid, "cmd": name, "status": "done"}))
            emit(f"  → /agv/cmd_ack cmd={name}")

    # jw_headless 는 'camera_calibration.pkl' 을 **현재 디렉토리 기준**으로 연다.
    # 드라이버를 어디서 실행하든 되도록, 자식의 cwd 를 스크립트가 있는 폴더로 맞춘다.
    script_dir = os.path.dirname(os.path.abspath(args.script)) or "."
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.abspath(args.script)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        cwd=script_dir,
    )

    q = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, q), daemon=True).start()

    idx = 0
    last_done_ts = None      # 마지막 DONE 을 본 시각 (반복 DONE 이 오면 계속 갱신)
    sent_ts = 0.0
    in_flight = None         # 방금 타이핑해서 실행 중인 (명령이름, target_node)
    done_n = 0

    def more_work():
        return (not server_q.empty()) if args.mqtt else (idx < len(plan))

    try:
        while True:
            # 1) 자식 출력 흡수 (전부 그대로 보여준다 — 원본 로그가 진단의 핵심)
            try:
                line = q.get(timeout=0.05)
            except queue.Empty:
                line = ""
            if line is None:
                emit("!! jw_headless 프로세스가 종료됨")
                break
            if line:
                emit(f"  | {line}")
                if DONE_MARK in line:
                    last_done_ts = time.time()
                    # 반복 DONE(3회) 중 첫 번째에서만 서버 보고 — in_flight 를 즉시 비운다
                    if in_flight is not None:
                        feedback(*in_flight)
                        in_flight = None
                        done_n += 1

            # 2) 실행 중인 게 없고 + DONE 후 settle 이 지났고 + 할 일이 있으면 '타이핑'
            if (in_flight is None and last_done_ts is not None
                    and time.time() - last_done_ts >= args.settle and more_work()):
                if args.mqtt:
                    name, target = server_q.get()
                    label = f"[서버 #{done_n + 1}]"
                else:
                    name, target = plan[idx], None
                    idx += 1
                    label = f"[{idx}/{len(plan)}]"
                code = CMD_CODE.get(name)
                if code is None:
                    emit(f"  !! 모르는 명령 무시: {name}")
                    continue
                proc.stdin.write(f"{code}\n")
                proc.stdin.flush()
                gap = time.time() - sent_ts if sent_ts else 0.0
                sent_ts = time.time()
                in_flight = (name, target)
                emit(f"★ {label} 입력: {code}  ({name}"
                     + (f" → node {target}" if target is not None else "") + ")"
                     + (f"  — 직전 명령과 간격 {gap:.1f}s" if gap else ""))
                last_done_ts = None      # 다음 DONE 을 기다린다

            # 3) 고정 시퀀스 모드는 다 치면 끝 (MQTT 모드는 Ctrl-C 까지 계속)
            if not args.mqtt and idx >= len(plan) and in_flight is None:
                emit("=== 시퀀스 완료. Ctrl-C 로 종료 (jw_headless는 계속 실행 중)")
                while True:
                    line = q.get()
                    if line is None:
                        break
                    emit(f"  | {line}")
                break

    except KeyboardInterrupt:
        emit("=== 사용자 중단")
    finally:
        if mqtt_cli is not None:
            try:
                mqtt_cli.publish(TOPIC_PRESENCE,
                                 json.dumps({"rid": args.rid, "online": False}),
                                 qos=1, retain=True)
                mqtt_cli.loop_stop()
                mqtt_cli.disconnect()
            except Exception:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        emit(f"=== 종료. 로그: {out_path}")
        log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

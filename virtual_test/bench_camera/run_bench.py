"""
벤치 테스트 — 라파 + 진짜 카메라만으로 서버↔AGV 통신 검증 (STM·로봇 없음)
TU Capstone Design - AGV 물류 피킹 시스템

목적:
  주원이가 로봇(STM+모터)을 가지고 있는 동안, 라파+카메라만으로
  '서버 ↔ 라파' 통신 루프가 실제로 도는지 확인한다.

    진짜 ArUco 감지 → /agv/marker 발행 → 서버가 위치 갱신 → /agv/cmd 수신 → 화면 출력

  검증되는 것: MQTT 연결, 마커 ID = 노드번호 매핑, 서버 상태머신 진행, 명령 수신
  검증 안 되는 것: 실제 주행/회전 정확도 (STM 영역 — 주원이가 정밀화 중)

왜 별도 파일인가:
  실물 배포 코드(hardware/bridge_rpi.py, config.py)에 테스트용 분기를 남기지 않기 위해.
  SIL과 같은 방식으로 모듈 전역만 monkeypatch 한다 (br.UART_ENABLED = False).

STM이 없으면 막히는 지점 (이 파일이 메꾸는 것):
  forward → 완료 신호 = 마커 보고. 사람이 다음 노드 마커를 카메라에 보여주면 됨. (OK)
  turn/lift → 완료 신호 = STM DONE(0x81). STM이 없으니 영영 안 옴 → 서버가 멈춤.
    → 여기서 가짜 DONE(cmd_ack)을 대신 보낸다. STM 동작 시간만큼 지연도 흉내낸다.

공간은 신경 쓸 필요 없다:
  서버는 'AGV가 어떤 마커를 봤는가'로만 위치를 안다. 바닥에 격자로 깔 필요 없이
  마커 카드를 손으로 카메라에 보여주면 된다. (인쇄: python3 -m hardware.make_marker_sheet)

사용 (라파에서, repo 루트):
  python3 -m virtual_test.bench_camera.run_bench 1      # AGV-1로 동작
  python3 -m virtual_test.bench_camera.run_bench 1 --ack-delay 2.0
  ※ 서버 IP는 hardware/config.py 의 MQTT_HOST
"""

import argparse
import sys
import threading
import time

import hardware.bridge_rpi as br
from hardware.config import CALIB_FILE, SHOW_PREVIEW


class VirtualRobot:
    """--auto-walk 전용 — '사람이 마커를 보여주는 행위'를 자동화한 가짜 로봇.

    실물 로봇의 인과관계를 그대로 흉내낸다: forward 명령을 받고 → 그 방향의 다음 노드로
    '이동'한 뒤 → 그 노드의 마커를 발행. (사람이 손으로 카드를 보여주는 것과 동일)
    타이머로 마커를 미리 쏘면 명령 순서와 어긋나 서버/트윈이 헛돈다 — 그래서 인과가 중요.

    heading은 서버와 같은 규약(0=N/90=E/180=S/270=W), 초기값도 서버 가정과 동일한 0.
    """

    def __init__(self, start_node: int, map_path: str):
        import json
        with open(map_path) as f:
            cfg = json.load(f)
        self.nodes = {n["id"]: (n["x"], n["y"]) for n in cfg["nodes"]}
        self.adj = {}
        for e in cfg["edges"]:
            a, b = int(e["from"]), int(e["to"])
            self.adj.setdefault(a, []).append(b)
            self.adj.setdefault(b, []).append(a)
        self.node = start_node
        self.heading = 0          # 서버 기본값과 동일 (북)

    def apply_turn(self, cmd: str):
        delta = {"turn_right": 90, "turn_left": 270, "turn_180": 180}[cmd]
        self.heading = (self.heading + delta) % 360

    def next_node(self):
        """현재 heading 방향의 인접 노드 (없으면 None)"""
        cx, cy = self.nodes[self.node]
        dx, dy = {0: (0, 1), 90: (1, 0), 180: (0, -1), 270: (-1, 0)}[self.heading]
        for nb in self.adj.get(self.node, []):
            nx, ny = self.nodes[nb]
            if abs((nx - cx) - dx) < 0.01 and abs((ny - cy) - dy) < 0.01:
                return nb
        return None


class BenchBridge(br.Bridge):
    """실물 Bridge + '가짜 STM' — turn/lift의 DONE을 대신 발행.

    실물 Bridge를 상속만 하므로 MQTT 경로(토픽/페이로드/발행 시점)는 진짜와 100% 동일.
    바뀌는 건 STM이 채워야 할 완료 신호를 타이머가 채운다는 것뿐.
    """

    def __init__(self, rid: int, ack_delay: float, robot: "VirtualRobot | None" = None):
        super().__init__(rid=rid)
        self.ack_delay = ack_delay
        self.robot = robot          # --auto-walk 일 때만 (없으면 사람이 마커를 보여줌)

    def _dispatch_cmd(self, cmd: str):
        super()._dispatch_cmd(cmd)          # 로그 + UART 시도(비활성이라 no-op)

        if cmd == "forward":
            if self.robot is None:
                print(f"    → forward: 다음 노드의 마커를 카메라에 보여주세요 "
                      f"(그게 도착 신호입니다)")
                return
            # 가짜 로봇: '이동'한 뒤에 도착 마커 발행 (실물의 인과관계)
            nxt = self.robot.next_node()
            if nxt is None:
                print(f"    → forward: heading {self.robot.heading}° 방향에 노드 없음")
                return
            print(f"    → (가짜 로봇) {self.robot.node} → {nxt} 주행 중... "
                  f"{self.ack_delay}s 후 마커 {nxt} 감지")
            t = threading.Timer(self.ack_delay, self._fake_arrival, args=(nxt,))
            t.daemon = True
            t.start()
            return

        print(f"    → (가짜 STM) {cmd} 실행 중... {self.ack_delay}s 후 완료 보고")
        t = threading.Timer(self.ack_delay, self._fake_stm_done, args=(cmd,))
        t.daemon = True
        t.start()

    def _fake_stm_done(self, cmd: str):
        """STM의 DONE(0x81) 대신 — 실물에선 _handle_uart_event가 하는 일"""
        if self.robot is not None and cmd in ("turn_left", "turn_right", "turn_180"):
            self.robot.apply_turn(cmd)      # 가짜 로봇도 실제로 돌아야 다음 forward가 맞음
        self._last_cmd = None
        self._pending_code = 0              # 실물 EVT_ACK 수신 시 동작과 동일
        self.publish_cmd_ack(cmd)

    def _fake_arrival(self, node: int):
        """가짜 로봇이 다음 노드에 도착 — 카메라가 마커를 봤을 때와 동일한 발행"""
        self.robot.node = node
        self.publish_marker(node)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("rid", nargs="?", type=int, default=1, help="AGV 번호 (기본 1)")
    p.add_argument("--ack-delay", type=float, default=1.5,
                   help="가짜 STM의 turn/lift 동작 시간 (초, 기본 1.5)")
    p.add_argument("--no-camera", action="store_true",
                   help="카메라 없이 브릿지만 (PC에서 트윈/서버 흐름 예행연습용). "
                        "마커는 밖에서 발행: mosquitto_pub -t /agv/marker "
                        "-m '{\"rid\":1,\"marker_id\":10}'")
    p.add_argument("--auto-walk", type=int, metavar="START_NODE",
                   help="--no-camera 와 함께: 가짜 로봇이 명령대로 '주행'하며 마커를 "
                        "자동 발행 (사람 대신). 예: --auto-walk 9  (AGV-1 홈)")
    p.add_argument("--no-preview", action="store_true",
                   help="OpenCV 미리보기 창 끄기 (라파에 모니터가 없을 때 필수). "
                        "감지 결과는 콘솔에 출력됨")
    args = p.parse_args()

    # SIL과 동일한 monkeypatch — 배포 config는 손대지 않는다
    br.UART_ENABLED = False

    print(f"[벤치 AGV-{args.rid}] 시작 — STM/모터 없음, 카메라만")
    print(f"  서버(MQTT): {br.MQTT_HOST}:{br.MQTT_PORT}")

    robot = None
    if args.auto_walk:
        import os
        map_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "server", "data", "map.json")
        robot = VirtualRobot(start_node=args.auto_walk, map_path=map_path)

    bridge = BenchBridge(rid=args.rid, ack_delay=args.ack_delay, robot=robot)
    bridge.open_uart()     # UART_ENABLED=False → '비활성 모드' 출력만
    bridge.connect()

    if args.no_camera:
        # PC 예행연습 — 카메라 없이 명령 수신/ACK만. 마커는 밖에서 발행한다.
        print(f"[벤치 AGV-{args.rid}] 카메라 없음 모드 — 명령 수신 + 가짜 STM만")
        if robot is not None:
            time.sleep(1.0)
            print(f"  (가짜 로봇) 홈 노드 {robot.node} 마커 발행 — 실물 전원 인가와 동일")
            bridge.publish_marker(robot.node)
        else:
            print("  마커 발행 예: mosquitto_pub -t /agv/marker "
                  f"-m '{{\"rid\":{args.rid},\"marker_id\":10}}'")
        print()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(f"\n[벤치 AGV-{args.rid}] 종료 중...")
        finally:
            bridge.disconnect()
        return

    from hardware.camera import RpiCamera   # picamera2 — 라파에서만 import 가능
    preview = SHOW_PREVIEW and not args.no_preview   # 헤드리스 라파면 --no-preview
    camera = RpiCamera(CALIB_FILE, show_preview=preview)
    print(f"[벤치 AGV-{args.rid}] 카메라 준비 완료 (미리보기: {'ON' if preview else 'OFF'})")
    print("  마커를 카메라에 보여주세요. 서버가 명령을 내리면 여기에 출력됩니다.")
    print("  ※ yaw 부호 확인: 마커를 시계방향 90° 돌렸을 때 yaw가 90 쪽인지 270 쪽인지 보세요.\n")

    prev_marker = None     # 같은 마커 중복 발행 방지 (rpi_main과 동일 규칙)
    last_log = 0.0
    try:
        while True:
            marker_id, x_mm, y_mm, yaw_deg = camera.detect()
            if marker_id is not None:
                bridge.set_marker_offset(x_mm, y_mm, yaw_deg)   # UART 없음 → 값만 확인용
                if marker_id != prev_marker:
                    print(f"[카메라] 마커 {marker_id} 감지 "
                          f"(x={x_mm}mm y={y_mm}mm yaw={yaw_deg}°) → 서버 보고")
                    bridge.publish_marker(marker_id)
                    prev_marker = marker_id
                elif not preview and time.time() - last_log > 1.0:
                    # 미리보기가 없으니 같은 마커라도 1초마다 오프셋을 찍어준다
                    # (yaw 부호·x/y 스케일을 눈으로 확인하는 유일한 창구)
                    print(f"    마커 {marker_id}: x={x_mm}mm y={y_mm}mm yaw={yaw_deg}°")
                    last_log = time.time()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print(f"\n[벤치 AGV-{args.rid}] 종료 중...")
    finally:
        camera.release()
        bridge.disconnect()


if __name__ == "__main__":
    main()

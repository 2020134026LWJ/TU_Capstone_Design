# hardware/ — AGV 실물(RPi) 코드

실물 AGV(STM32 + RPi) 코드. **Isaac Sim 전용 코드는 `isaac_simulation/`으로 분리**됨.

## 구조

```
hardware/
├── bridge_rpi.py     ★ 실물 bridge (주원이 STM ASCII 프로토콜 + 카메라 offset)
├── camera.py         ★ 실물 카메라 (주원이 opencv 비전 기반, detect → id/x/y/yaw)
├── rpi_main.py       ★ RPi 진입점 (bridge_rpi + camera 엮기)
├── sil/              ★ SIL — 가짜 STM + 가상 UART로 통신 검증 (실물 없이)
│   ├── mock_stm.py
│   └── run_sil.py
├── AGV_Control.zip               (주원이) 실제 STM32F7 CubeIDE 펌웨어
├── opencv_arucomarker_detection_v4.py  (주원이) 카메라 원본 — camera.py가 이 비전 로직 참고
└── camera_calibration.pkl        (주원이) 카메라 캘리브레이션
```

> ★ = 사용자 코드. Isaac 전용(`bridge_isaac.py`·`isaac_hw.py`·`IsaacCamera`)은 `isaac_simulation/`,
> stm32 임시 스켈레톤은 `archive/hardware_stm32_skeleton/`으로 옮겨짐.

---

## 흐름 (실물)

```
서버 ──MQTT /agv/cmd──→ bridge_rpi ──UART <cmd,x,y,yaw>──→ STM32
camera ──(id,x,y,yaw)──→ bridge.set_marker_offset(x,y,yaw)  (offset을 UART 패킷에 합성)
                       └→ bridge.publish_marker(id, heading) (위치 추적용, 서버로)
STM32 ──0x81/0xFF──→ bridge_rpi ──/agv/cmd_ack──→ 서버
```
- `camera.py` = 비전(ArUco offset/id 계산)만, UART/명령은 `bridge_rpi`가 담당 (역할 분리)
- 같은 라파 안 camera↔bridge는 **메모리 공유**(함수 호출), MQTT는 라파↔서버만

---

## UART 프로토콜 (주원이 STM 기준)

**송신** (bridge_rpi → STM): ASCII `<command,±xxxx,±yyyy,±wwww>` (21바이트)
- command 1자리(1~7), x/y/yaw = (mm·deg)×10 정수 (STM이 /10 복원)
- offset = camera가 `set_marker_offset()`으로 공급한 최신값

| forward | stop | lift_up | lift_down | turn_left | turn_right | turn_180 |
|---|---|---|---|---|---|---|
| 1 | 2 | 3 | 4 | 5 | 6 | 7 |

**수신** (STM → bridge_rpi): 단일 바이트 `0x81`=DONE / `0xFF`=ACK

**STM 실제 응답**(main.c): 명령 → ACK 즉시 → 동작 → DONE 1번. 항상 ACK→DONE, 중복 없음.

---

## SIL — 통신 검증 (실물 없이)

```bash
cd TU_Capstone_Design
python3 -m hardware.sil.run_sil
```
가짜 STM(`mock_stm.py`) + 가상 UART(pty)로 `bridge_rpi` 명령-응답 검증.
**결과**: 정상 흐름(하나씩) 견고. 진짜 위험 = **통신 유실(DONE 누락) 시 멈춤**(복구 없음).

---

## 통합 결정 요약 (2026-06-29 논의)

- **STM 펌웨어 0수정** — bridge를 주원이 ASCII 프로토콜에 맞춤 (STM은 PID/IMU 엮여 못 건드림)
- **역할 분담** — 주원이 = 카메라 비전 / 사용자 = bridge(UART 다리). camera가 `set_marker_offset`·`publish_marker` 호출
- **bridge Isaac/실물 분리** — 한 파일에 두 모드 섞으니 한쪽 고치다 다른쪽 깨짐 → `bridge_isaac.py`(시뮬) / `bridge_rpi.py`(실물)
- **같은 라파 = 메모리 공유** — camera↔bridge는 함수 호출(MQTT 불필요), MQTT는 라파↔서버(다른 머신)만
- **실물 카메라 = 주원이 opencv 비전 복붙**(`camera.py`), 시뮬 `IsaacCamera`는 별개(proximity)
- **검증 = SIL**(위 참조) — 정상 흐름 견고, 가상 race(중복·순서)는 STM 구조상 안 남, **진짜 위험 = 통신 유실(DONE 누락 멈춤)**

## 미팅에서 확정할 것 (`bridge_rpi.py` 의 TODO)

1. **carrier 흐름** — STM이 command=0 패킷을 계속 받아야 하나
2. **forward 완료신호** — forward도 DONE 1번 옴 → 서버 marker와 조율
3. **통신 유실 복구** — DONE 누락 timeout 주체 + STM 상태 조회 가능한가
4. **heading 변환** — camera yaw_deg(0~360) → 서버 heading(0=N/90=E)

---

## 실물 전환

1. `bridge_rpi.py`: `UART_ENABLED = True`, `MQTT_HOST = "서버IP"`
2. UART 포트 `/dev/ttyAMA10` (주원이 카메라와 동일)
3. 실행: `python3 -m hardware.rpi_main 1` (AGV-1) / `2` (AGV-2)

## 의존성
```
paho-mqtt >= 1.6   pyserial >= 3.5   picamera2   opencv-python   numpy
```

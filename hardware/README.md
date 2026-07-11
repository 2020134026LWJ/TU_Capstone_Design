# hardware/ — AGV 실물(RPi) 코드

실물 AGV(STM32 + RPi) 코드. **Isaac Sim 전용 코드는 `isaac_simulation/`으로 분리**됨.
역할 분담: **비전(카메라) = 주원이 / UART 다리(bridge) = 사용자**. 같은 라파 안에서 함수 호출로 엮고, MQTT는 라파↔서버(다른 머신)에만 쓴다.

---

## 빠른 실행 (clone & run)

```bash
# 1) 라파마다 한 번: 자기가 몇 번 AGV인지 ~/.bashrc 에 박기
echo 'export AGV_ID=1' >> ~/.bashrc   # 2번 라파는 =2

# 2) 배포 설정은 hardware/config.py 한 곳만 (서버 IP / UART / 카메라)

# 3) 실행 (repo 루트에서)
python3 -m hardware.rpi_main          # AGV_ID 환경변수로 자동
# 또는 인자로:  python3 -m hardware.rpi_main 1
```

> `AGV_ID`도 인자도 없으면 **에러로 멈춤**(기본 1 금지 — 두 라파가 같은 코드라 '둘 다 1' 충돌 방지).

---

## 구조

```
hardware/
├── rpi_main.py       ★ RPi 진입점 (camera + bridge 엮기, AGV_ID 해석)
├── camera.py         ★ 실물 카메라 — 비전 전용 (주원이 opencv 비전 그대로, detect→id/x/y/yaw)
├── bridge_rpi.py     ★ 실물 bridge — MQTT ↔ UART (주원이 STM ASCII 프로토콜 + 카메라 offset)
├── config.py         ★ 배포 설정 한 곳 (MQTT_HOST / UART_* / CALIB_FILE / SHOW_PREVIEW)
├── stm32/
│   └── rpi_uart.c                      (주원이) STM UART 송수신 — 신호 3회 반복 반영본 (660a43e)
├── AGV_Control.zip                     (주원이) 실제 STM32F7 CubeIDE 펌웨어 (전체 프로젝트)
├── opencv_arucomarker_detection_v4.py  (주원이) 카메라 원본 — camera.py가 이 비전 로직을 그대로 옮김
└── camera_calibration.pkl              (주원이) 카메라 캘리브레이션
```

> ★ = 사용자 코드. Isaac 전용(`bridge_isaac.py`·`isaac_hw.py`·`IsaacCamera`)은 `isaac_simulation/`,
> stm32 임시 스켈레톤은 `archive/hardware_stm32_skeleton/`에.
> `stm32/rpi_uart.c`는 주원이가 별도로 올린 최신 STM 소스(전체 프로젝트는 `AGV_Control.zip`).

---

## 흐름 (실물)

```
서버 ──MQTT /agv/cmd──→ bridge_rpi ──UART <cmd,x,y,yaw>──→ STM32
camera.detect() ──(id,x,y,yaw)──→ rpi_main 루프
        ├→ bridge.set_marker_offset(x,y,yaw)  : 매 프레임 offset을 UART 패킷에 실어 스트리밍
        └→ bridge.publish_marker(id)          : 새 마커일 때만 위치 보고 (서버)
STM32 ──0x81(DONE)/0xFF(ACK)──→ bridge_rpi ──/agv/cmd_ack──→ 서버
```

- `camera.py` = 비전(ArUco offset/id)만, UART/명령은 `bridge_rpi`가 담당 (역할 분리)
- 같은 라파 안 camera↔bridge는 **메모리 공유**(함수 호출), MQTT는 라파↔서버만
- 서버 발행은 **새 마커일 때만**(`prev_marker` 가드) — 같은 마커 중복 트리거 방지. offset은 매 프레임 STM으로 계속 흐름.

---

## UART 프로토콜 (주원이 STM 기준 — 3자 일치 검증됨)

bridge_rpi 송신 / 주원이 카메라 원본 / STM 파서, 셋이 byte 단위로 동일함을 확인.

**송신** (bridge_rpi → STM): ASCII `<command,±xxxx,±yyyy,±wwww>` (21바이트)
- command 1자리(1~7, **0 = carrier/무명령**), x/y/yaw = (mm·deg)×10 정수 (STM이 /10 복원)
- offset = camera가 `set_marker_offset()`으로 공급한 최신값
- STM 파서 위치: cmd=buf[1], x=buf[3], y=buf[9], yaw=buf[15] / 프레임 `[0]=='<'`, `[20]=='>'`
- ⚠️ offset 절댓값은 **±999.9(=±9999) 이내** 가정 (넘으면 6자리 → 21바이트 깨짐)

| forward | stop | lift_up | lift_down | turn_left | turn_right | turn_180 |
|---|---|---|---|---|---|---|
| 1 | 2 | 3 | 4 | 5 | 6 | 7 |

**수신** (STM → bridge_rpi): 단일 바이트 `0x81`=DONE / `0xFF`=ACK (각 **3회 반복 송신** — 아래 통신유실 참조)

**전송 흐름**: command는 평소 0(carrier, offset만 스트리밍), MQTT 명령 오면 `_pending_code`에 실림 → `EVT_ACK` 오면 0 복귀. STM은 `STATE_READY`일 때만 command를 읽으므로(이동 중엔 무시) 다음 명령을 미리 보내도 안전.

**완료 신호 처리**:
- **forward 완료 = 카메라 마커**(서버가 forward의 ACK로 사용). → bridge는 **forward DONE을 서버로 안 보냄**(중복/오ack 방지).
- **turn/lift 완료 = cmd_ack(DONE)**.
- **heading** = 서버가 경로 기반 계산. 카메라 yaw는 STM offset에만 쓰고 서버엔 heading 미전송. (절대방위가 필요하면 마커 부착 방향 + 보정상수 K로 변환하거나 STM IMU 보고 — 미정)

---

## 통신 유실 대비 ✅

raw UART는 ACK/DONE이 1회 송신이라, **DONE 유실 → 멈춤 / ACK 유실 → 이중 실행** 위험.
(MQTT는 TCP 위라 재전송 보장되어 무관 — UART만 안전망 없음)

- **해결**(660a43e): STM `Send_Event`가 **ACK·DONE을 3회 반복 송신**(`HAL_UART_Transmit` ×3, 사이 `HAL_Delay(10)`). 한 함수만 고쳐 호출 4군데(forward/회전/리프트 DONE + ACK) 전부 커버.
- **우리 bridge는 이미 중복 안전**: DONE 3개 → 첫 번째만 cmd_ack(`_last_cmd` None으로 나머지 skip) / ACK 3개 → `_pending_code=0` 멱등. **우리 수정 0.**
- HAL_Delay 20ms는 ACK=이동 시작 전 / DONE=모터 멈춘 후라 무해, RX는 DMA라 그 동안에도 수신.

---

## SIL — 통신 검증 (실물 없이)

SIL 하니스는 **`virtual_test/software_in_the_loop/`** 로 이동됨 (알고리즘 테스트와 함께 `virtual_test/`에서 관리). 가짜 STM + 가상 UART(pty)로 `bridge_rpi`의 명령-응답을 실물 없이 검증한다 (상세는 `run_sil.py` docstring).

```bash
python3 -m virtual_test.software_in_the_loop.run_sil   # repo 루트에서
```

---

## 설정 (`config.py`)

| 항목 | 값 | 비고 |
|---|---|---|
| `MQTT_HOST` | `172.30.1.26` | PC 서버 IP (핫스팟). 네트워크 바뀌면 여기만 |
| `UART_PORT` / `UART_BAUD` | `/dev/ttyAMA10` / `115200` | 주원이 카메라와 동일 |
| `UART_ENABLED` | `True` | 실물 전용 (SIL은 monkeypatch로 덮음) |
| `CALIB_FILE` | (자동) | `hardware/camera_calibration.pkl`, cwd 무관 |
| `SHOW_PREVIEW` | `True` | 헤드리스(디스플레이 없는) 라파면 `False` |
| **AGV_ID** | — | config 아님 → **라파별 `.bashrc` `export AGV_ID=1/2`** |

---

## 남은 일 (HIL — 실물 붙여서)

- **heading**: 마커 부착 방향 기준 보정상수 K 측정 (또는 IMU 출처 결정)
- **turn 방향**: 서버 "turn_left" = AGV 실제 좌회전인지(handedness) 확인
- **marker_id == node_id**: 바닥 ArUco를 노드 번호와 1:1 인쇄/배치
- 카메라 실동작(주원이 하드 + calibration) / 2대 동시(rid별) / picamera2·opencv 설치
- 통신유실 3회 반복 라이브 확인(거의 닫혔지만 실측)

---

## 의존성
```
paho-mqtt >= 1.6   pyserial >= 3.5   picamera2   opencv-python   numpy
```

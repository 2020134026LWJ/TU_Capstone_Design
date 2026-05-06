
.# hardware/ — AGV 하드웨어 추상화

## 구조

```
hardware/
├── __init__.py
├── bridge.py      ← MQTT ↔ UART 브릿지 (Isaac Sim / RPi 공통)
├── camera.py      ← RpiCamera / IsaacCamera 공통 인터페이스
├── isaac_hw.py    ← IsaacMotors (Isaac Sim 전용 가상 모터)
├── rpi_main.py    ← RPi 진입점
└── stm32/         ← STM32 C 펌웨어
```

---

## 아키텍처

```
서버 (PC)
  │  MQTT /agv/cmd
  ▼
Bridge (bridge.py)
  │  UART 115200bps          (RPi 모드)
  │  콜백                    (Isaac Sim 모드)
  ▼
STM32 / IsaacAGV
  │  PWM / 가상 이동
  ▼
모터/리프트
```

AGV → 서버:
- `/agv/marker` — ArUco 마커 감지 (marker_id + heading)
- `/agv/cmd_ack` — 명령 완료 (turn / lift 완료)

서버 → AGV:
- `/agv/cmd` — 이동/회전/리프트 명령

---

## Bridge 두 모드

```python
# Isaac Sim 모드 (step6_visual.py)
bridge = Bridge(rid=1, cmd_handler=agv._on_cmd_from_bridge)
bridge.connect()

# RPi 실물 모드 (rpi_main.py)
bridge = Bridge(rid=1)   # cmd_handler=None → UART 모드
bridge.open_uart()        # UART 포트 열기 + 수신 스레드
bridge.connect()
```

---

## 실행

```bash
# AGV-1
python3 hardware/rpi_main.py 1

# AGV-2
python3 hardware/rpi_main.py 2
```

---

## 실물 전환 (Isaac Sim → RPi)

### 1. UART 활성화

```bash
# /boot/config.txt 에 추가
enable_uart=1

sudo reboot
```

### 2. bridge.py 상단 설정

```python
UART_ENABLED = True        # False → UART 비활성 (시뮬 모드)
UART_PORT    = "/dev/ttyAMA0"
UART_BAUD    = 115200

MQTT_HOST = "192.168.x.x"  # 서버 PC IP
```

### 3. 카메라 캘리브레이션 파일 준비

```bash
# camera_calibration.pkl 생성 후 hardware/ 폴더에 배치
# camera_matrix, dist_coeffs 포함
```

---

## MQTT 프로토콜

### 서버 → RPi (`/agv/cmd`)

```json
{"rid": 1, "cmd": "forward"}
{"rid": 1, "cmd": "turn_left"}
{"rid": 1, "cmd": "turn_right"}
{"rid": 1, "cmd": "turn_180"}
{"rid": 1, "cmd": "lift_up"}
{"rid": 1, "cmd": "lift_down"}
```

### RPi → 서버 (`/agv/marker`)

```json
{"rid": 1, "marker_id": 14, "heading": 90, "ts": 1700000000}
```

`heading`: 서버 기준 (0=North, 90=East, 180=South, 270=West)

### RPi → 서버 (`/agv/cmd_ack`)

```json
{"type": "cmd_ack", "rid": 1, "cmd": "turn_left", "status": "done"}
{"type": "cmd_ack", "rid": 1, "cmd": "lift_up",   "status": "done"}
```

---

## UART 패킷 프로토콜 (RPi ↔ STM32)

### 패킷 구조

```
[0xAA] [CMD] [LEN] [PAYLOAD...] [CRC]

CRC = CMD ^ LEN ^ payload[0] ^ payload[1] ^ ...
```

### RPi → STM32 명령

| CMD  | 이름          | PAYLOAD |
|------|---------------|---------|
| 0x01 | MOVE_FORWARD  | 없음    |
| 0x02 | STOP          | 없음    |
| 0x03 | LIFT_UP       | 없음    |
| 0x04 | LIFT_DOWN     | 없음    |
| 0x05 | ROTATE_LEFT   | 없음    |
| 0x06 | ROTATE_RIGHT  | 없음    |
| 0x07 | ROTATE_180    | 없음    |

### STM32 → RPi 이벤트

| CMD  | 이름         | PAYLOAD           |
|------|--------------|-------------------|
| 0x81 | MOVE_DONE    | 없음 (현재 미사용) |
| 0x82 | ROTATE_DONE  | 없음               |
| 0x83 | LIFT_DONE    | `[1]`=up, `[0]`=down |
| 0xFF | ACK          | 없음 (명령 수신 확인) |

### 이벤트 → cmd_ack 매핑

| STM32 이벤트          | 서버에 보고하는 cmd_ack                              |
|-----------------------|------------------------------------------------------|
| ROTATE_DONE           | 마지막 turn 명령 (turn_left / turn_right / turn_180) |
| LIFT_DONE(payload=1)  | `lift_up`                                            |
| LIFT_DONE(payload=0)  | `lift_down`                                          |

---

## 의존성

```
paho-mqtt >= 1.6.0
pyserial >= 3.5
picamera2              # 실물 카메라 (RPi OS 기본 내장)
opencv-python          # ArUco 감지
numpy
```

```bash
pip install paho-mqtt pyserial opencv-python numpy
```

---

## 문제 해결

### UART 권한 오류
```bash
sudo chmod 666 /dev/ttyAMA0
# 또는 dialout 그룹 추가
sudo usermod -aG dialout $USER
```

### MQTT 연결 실패
- 서버에서 Mosquitto 실행 확인: `sudo systemctl status mosquitto`
- 방화벽 1883 포트 확인
- MQTT_HOST IP 확인

### ArUco 감지 실패
- `camera_calibration.pkl` 파일 존재 여부 확인
- 마커 크기 (`_marker_size = 0.05m`) 실제와 일치 확인
- 조명 조건 확인

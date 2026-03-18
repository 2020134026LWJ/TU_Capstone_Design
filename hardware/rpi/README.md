# RPi Bridge

MQTT ↔ UART 양방향 브릿지 모듈

서버(MQTT)와 STM32(UART) 사이에서 명령/이벤트를 중계합니다.

## 구조

```
서버 (PC)  ←── MQTT ──→  RPi Bridge  ←── UART ──→  STM32
```

## 설치

### 1. 의존성 설치

```bash
cd rpi
pip install -r requirements.txt
```

### 2. 설정 변경

`bridge.py` 파일 상단의 설정을 환경에 맞게 수정:

```python
# MQTT 설정 (서버 IP로 변경)
MQTT_HOST = "192.168.x.x"   # 서버 컴퓨터 IP
MQTT_PORT = 1883

# UART 설정
UART_PORT = "/dev/ttyAMA0"  # RPi UART 포트
UART_BAUD = 115200
UART_ENABLED = True         # 실제 하드웨어 사용 시 True
```

### 3. RPi UART 활성화

```bash
# /boot/config.txt에 추가
sudo echo "enable_uart=1" >> /boot/config.txt

# 재부팅
sudo reboot
```

## 실행

```bash
python bridge.py
```

로봇 수 지정:
```bash
python bridge.py 2    # 로봇 2대
```

## 시뮬레이션 모드

실제 STM32 없이 테스트하려면:

```python
UART_ENABLED = False   # 시뮬레이션 모드
```

이 모드에서는 MQTT만 사용하며, Webots 시뮬레이션과 연동됩니다.

## UART 프로토콜

```
패킷 구조: [0xAA] [CMD] [LEN] [PAYLOAD...] [CRC]
CRC = CMD ^ LEN ^ payload[0] ^ payload[1] ^ ...
```

### RPi → STM32 명령

| CMD | 이름 | 설명 |
|-----|------|------|
| 0x01 | MOVE_TO_NODE | 노드로 이동 |
| 0x02 | STOP | 정지 |
| 0x03 | LIFT_UP | 리프트 올리기 |
| 0x04 | LIFT_DOWN | 리프트 내리기 |

### STM32 → RPi 이벤트

| CMD | 이름 | 설명 |
|-----|------|------|
| 0x81 | MOVE_DONE | 이동 완료 |
| 0x83 | LIFT_DONE | 리프트 완료 |
| 0x85 | MARKER_PASSED | 마커 통과 |

## 문제 해결

### MQTT 연결 실패
- 서버에서 Mosquitto가 실행 중인지 확인
- 방화벽에서 1883 포트 열기
- IP 주소 확인

### UART 통신 실패
- `/dev/ttyAMA0` 권한 확인: `sudo chmod 666 /dev/ttyAMA0`
- 배선 확인 (TX-RX 크로스)
- 보드레이트 일치 확인

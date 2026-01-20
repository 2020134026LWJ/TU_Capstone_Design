# AGV Webots Simulation

AGV 물류 시스템 Webots 시뮬레이션

## 파일 역할

| 파일 | 역할 |
|------|------|
| `map.json` | 9x5 그리드 맵 정의 (45개 노드) |
| `server.py` | A* 경로 계획, MQTT로 경로 발행 |
| `bridge.py` | 경로 수신 → AGV에 이동 명령 전달 |
| `controllers/agv_mqtt_controller/` | Webots AGV 제어 (MQTT 통신) |
| `worlds/warehouse_9x5.wbt` | Webots 환경 (로봇 2대, 마커) |

## 시스템 흐름

```
server.py → bridge.py → Webots AGV
(경로계획)    (명령중계)    (로봇이동)
```

## 실행 방법

```bash
# 1. Webots 실행
webots ~/Projects/TU_Capstone_Design/webots_simulation/worlds/warehouse_9x5.wbt

# 2. Bridge 실행 (새 터미널)
cd ~/Projects/TU_Capstone_Design/webots_simulation
python3 bridge.py

# 3. Server 실행 (새 터미널) - 경로 계획 시작
python3 server.py
```

## 필수 설치

```bash
sudo snap install webots
sudo apt install mosquitto python3-paho-mqtt
```

## 설정 변경

로봇 시작/목표 위치 변경: `server.py`의 `starts`, `goals` 수정

# AGV Webots Simulation

AGV 물류 피킹 시스템 Webots 시뮬레이션

## 디렉토리 구조

```
webots_simulation/
│
├── server/                     # 모듈화된 AGV 서버 (v3)
│   ├── main.py                 # 서버 진입점
│   ├── config.py               # 설정 관리
│   ├── path_planner.py         # A* 경로 계획
│   ├── mqtt_publisher.py       # MQTT 발행
│   ├── websocket_handler.py    # WebSocket 서버 (Admin UI 연결)
│   ├── request_handler.py      # 요청 처리
│   ├── robot_manager.py        # 로봇 상태 관리
│   └── README.md               # 서버 설계 문서
│
├── controllers/                # Webots 컨트롤러
│   ├── agv_controller/         # 기본 AGV 컨트롤러
│   └── agv_mqtt_controller/    # MQTT 연동 컨트롤러
│
├── worlds/                     # Webots 월드 파일
│   ├── warehouse.wbt           # 기본 창고
│   └── warehouse_9x5.wbt       # 9x5 그리드 창고 (메인)
│
├── protos/                     # 커스텀 PROTO 파일 (현재 비어있음)
├── plugins/                    # Webots 플러그인 (현재 비어있음)
│
├── bridge.py                   # MQTT 브릿지 (서버 ↔ AGV)
├── map.json                    # 9x5 그리드 맵 (45개 노드)
└── robot_config.json           # 로봇 설정 (2대)
```

## 시스템 흐름

```
[Admin UI] ──WebSocket──> [Server] ──MQTT──> [bridge.py] ──MQTT──> [AGV]
  (8765)                    │                    │
                            │                    └─ /agv/lowcmd 발행
                            └─ /agv/plan 발행
```

## 실행 방법

### 1. 서버 실행
```bash
cd webots_simulation
python -m server.main
```
- WebSocket 서버: `ws://localhost:8765`
- MQTT 발행: `/agv/plan`

### 2. 브릿지 실행
```bash
python bridge.py
```
- `/agv/plan` 수신 → `/agv/lowcmd` 발행

### 3. Webots 시뮬레이션 실행
```bash
webots worlds/warehouse_9x5.wbt
```

## 요청 형식 (Admin UI → Server)

### 작업 요청
```json
{
  "type": "task_request",
  "worker_id": 1,
  "worker_marker": 1,
  "shelf_marker": 23
}
```

### 상태 요청
```json
{
  "type": "status_request"
}
```

## 맵 구조 (9x5 그리드)

```
 1  2  3  4  5  6  7  8  9
10 11 12 13 14 15 16 17 18
19 20 21 22 23 24 25 26 27
28 29 30 31 32 33 34 35 36
37 38 39 40 41 42 43 44 45
```

- 마커 ID = 노드 ID (1:1 매핑)
- AGV-1 홈: 노드 1
- AGV-2 홈: 노드 37

## 필수 설치

```bash
# Webots
sudo snap install webots

# MQTT 브로커
sudo apt install mosquitto

# Python 패키지
pip install websockets paho-mqtt
```

## 설정 변경

| 항목 | 파일 | 설명 |
|------|------|------|
| 로봇 설정 | `robot_config.json` | 로봇 ID, 이름, 홈 노드 |
| 맵 구조 | `map.json` | 노드, 엣지 정의 |
| 서버 설정 | `server/config.py` | 포트, 경로 등 |

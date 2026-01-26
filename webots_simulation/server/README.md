# AGV 서버 설계 문서

## 1. 전체 시스템 구조

```
┌─────────────┐     WebSocket      ┌─────────────┐      MQTT       ┌─────────────┐
│  Admin UI   │ ──────────────────>│   Server    │ ───────────────>│  bridge.py  │
│  (관리자)    │    port 8765       │  (이 서버)   │   /agv/plan     │             │
└─────────────┘                    └─────────────┘                 └──────┬──────┘
                                                                          │
                                                                          │ MQTT
                                                                          │ /agv/lowcmd
                                                                          v
                                                                   ┌─────────────┐
                                                                   │    AGV      │
                                                                   │  (Webots)   │
                                                                   └─────────────┘
```

**데이터 흐름:**
1. Admin UI에서 "작업자 1번이 선반 23번으로 가야 해" 라고 요청
2. Server가 경로를 계산 (A* 알고리즘)
3. 계산된 경로를 MQTT로 bridge.py에 전송
4. bridge.py가 AGV에게 이동 명령 전달


## 2. 모듈별 역할

### 📁 파일 구조
```
server/
├── __init__.py          # 패키지 초기화
├── config.py            # 설정값 관리
├── main.py              # 서버 시작점
├── websocket_handler.py # WebSocket 통신
├── request_handler.py   # 요청 처리
├── path_planner.py      # 경로 계획 (A*)
├── mqtt_publisher.py    # MQTT 발행
└── robot_manager.py     # 로봇 상태 관리
```

### 각 모듈 설명

#### `config.py` - 설정 관리
모든 설정값을 한 곳에서 관리합니다.
```python
- MQTT 호스트/포트: localhost:1883
- WebSocket 포트: 8765
- 맵 파일 경로: map.json
- 로봇 설정 파일: robot_config.json
```

#### `websocket_handler.py` - WebSocket 서버
Admin UI와 통신하는 창구입니다.
```
역할:
- 클라이언트 연결 관리
- JSON 메시지 수신
- 응답 전송
```

#### `request_handler.py` - 요청 처리
받은 요청을 분석하고 적절한 처리를 합니다.
```
지원하는 요청 타입:
1. task_request    - 작업 요청 (경로 계획)
2. status_request  - 상태 조회
3. robot_status    - 로봇 상태 업데이트
```

#### `path_planner.py` - 경로 계획
A* 알고리즘으로 최적 경로를 찾습니다.
```
기능:
- map.json 로드
- A* 알고리즘 (시간 기반 충돌 회피)
- 다중 로봇 경로 계획 (Prioritized Planning)
```

#### `mqtt_publisher.py` - MQTT 발행
계산된 경로를 bridge.py로 전송합니다.
```
토픽: /agv/plan
형식: JSON (job_id, robots, speed 포함)
```

#### `robot_manager.py` - 로봇 관리
로봇들의 상태를 추적합니다.
```
관리 정보:
- 로봇 ID, 이름
- 현재 위치 (노드)
- 상태 (idle, busy, error)
- 작업 큐
```


## 3. 통신 프로토콜

### Admin UI → Server (WebSocket)

**작업 요청:**
```json
{
  "type": "task_request",
  "worker_id": 1,          // 작업자 번호 (= 로봇 ID)
  "worker_marker": 37,     // 현재 위치 마커
  "shelf_marker": 23       // 목표 선반 마커
}
```

**상태 요청:**
```json
{
  "type": "status_request"
}
```

### Server → Admin UI (WebSocket)

**작업 응답:**
```json
{
  "type": "task_response",
  "success": true,
  "worker_id": 1,
  "robot_id": 1,
  "start_node": 37,
  "goal_node": 23,
  "path": [37, 38, 29, 20, 21, 22, 23],
  "path_length": 7,
  "mqtt_published": true
}
```

**상태 응답:**
```json
{
  "type": "status_response",
  "success": true,
  "mqtt_connected": true,
  "robots": {
    "total_robots": 2,
    "idle": 2,
    "busy": 0,
    "robots": [...]
  }
}
```

### Server → bridge.py (MQTT)

**토픽:** `/agv/plan`
```json
{
  "job_id": 1737886123,
  "planner": "prioritized_astar_with_time_on_graph",
  "robots": [
    {
      "rid": 1,
      "start": 37,
      "goal": 23,
      "node_path": [37, 38, 29, 20, 21, 22, 23],
      "timed_path": [
        {"node": 37, "t": 0},
        {"node": 38, "t": 1},
        ...
      ]
    }
  ],
  "speed": 0.3
}
```


## 4. 마커 ↔ 노드 매핑

**1:1 매핑 방식** (가장 단순)
```
마커 ID = 노드 ID

예시:
- 마커 1  → 노드 1
- 마커 23 → 노드 23
- 마커 45 → 노드 45
```

### 9x5 그리드 맵 노드 배치
```
 1  2  3  4  5  6  7  8  9
10 11 12 13 14 15 16 17 18
19 20 21 22 23 24 25 26 27
28 29 30 31 32 33 34 35 36
37 38 39 40 41 42 43 44 45
```


## 5. 실행 방법

### 서버 실행
```bash
cd /home/lwj/Projects/TU_Capstone_Design/webots_simulation
/home/lwj/anaconda3/bin/python -m server.main
```

### 테스트 (Python)
```python
import asyncio
import websockets
import json

async def test():
    async with websockets.connect('ws://localhost:8765') as ws:
        # 작업 요청
        request = {
            "type": "task_request",
            "worker_id": 1,
            "worker_marker": 1,
            "shelf_marker": 23
        }
        await ws.send(json.dumps(request))
        response = await ws.recv()
        print(response)

asyncio.run(test())
```

### 전체 시스템 테스트
```bash
# 터미널 1: 서버 실행
python -m server.main

# 터미널 2: bridge.py 실행
python bridge.py

# 터미널 3: Webots 시뮬레이션 실행

# 터미널 4: 테스트 요청 전송
```


## 6. 의존성

```
websockets==16.0    # WebSocket 서버
paho-mqtt==2.1.0    # MQTT 클라이언트
```

설치:
```bash
/home/lwj/anaconda3/bin/pip install websockets paho-mqtt
```


## 7. 설정 파일

### `robot_config.json`
```json
{
  "robots": {
    "1": {"home_node": 1, "name": "AGV-1"},
    "2": {"home_node": 37, "name": "AGV-2"}
  }
}
```

### `map.json`
- 45개 노드 (9x5 그리드)
- 양방향 엣지
- 각 엣지 cost = 1


## 8. 흐름도 예시

```
[Admin UI] 작업 요청: worker_id=1, 마커 1→23
                │
                ▼
[WebSocketHandler] JSON 수신
                │
                ▼
[RequestHandler] 요청 파싱 및 검증
                │
                ├── 마커 1 → 노드 1 변환
                ├── 마커 23 → 노드 23 변환
                │
                ▼
[PathPlanner] A* 경로 계산
                │
                └── 결과: [1, 2, 3, 4, 13, 14, 23]
                │
                ▼
[RobotManager] 로봇 1에 작업 할당
                │
                ▼
[MQTTPublisher] /agv/plan 토픽으로 발행
                │
                ▼
[bridge.py] 경로 수신 → AGV 제어
```

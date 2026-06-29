# Controllers

Webots 시뮬레이션에서 AGV를 제어하는 컨트롤러 모음

## 폴더 구조

```
controllers/
├── agv_controller/               # 기본 AGV 컨트롤러 (테스트용, MQTT 없이 단독 실행)
│   └── agv_controller.py
│
└── agv_mqtt_controller/          # MQTT 연동 AGV 컨트롤러 (현재 사용)
    ├── main.py                   # AGVMainController 임포트 + 실행 진입점
    ├── agv_controller.py         # 메인 AGV 로직 (메인루프, 마커감지, 리프트)
    ├── aruco_detector.py         # ArUco 마커 감지 (cv2, _last_marker 트래킹)
    ├── mqtt_handler.py           # MQTT 발행/수신 (publish_marker, publish_arrived, publish_position, publish_resume)
    ├── navigation.py             # 경로추종 + NODE_WAIT 상태 관리 (8×4 그리드)
    ├── hardware/                 # 실물/시뮬 하드웨어 추상화 레이어
    │   ├── base.py               # CollisionSensorInterface 기본 클래스
    │   ├── webots_hw.py          # Webots Supervisor API 구현
    │   └── raspi_hw.py           # Raspberry Pi 실물 하드웨어 구현
    ├── paho/                     # paho-mqtt 라이브러리 (Webots 내부 Python 환경용)
    └── runtime.ini               # Webots 런타임 설정
```

## 컨트롤러 설명

### `agv_controller/`
- 기본 테스트용 컨트롤러
- MQTT 없이 단독 실행 가능

### `agv_mqtt_controller/` (현재 사용)
- **Supervisor** 모드로 동작 (Robot이 아닌 Supervisor)
- MQTT를 통해 서버와 직접 통신 (bridge 없이)
- `/agv/plan` 토픽에서 경로 계획을 수신 → 주행
- `/agv/shelf_cmd` 토픽에서 선반 리프트 명령 수신
- `/agv/control` 토픽에서 `resume` 명령 수신 → NODE_WAIT 해제
- `/agv/arrived`, `/agv/marker` 토픽으로 상태 보고
- **리프트 메커니즘**: SliderJoint + LinearMotor로 선반 들어올리기/내려놓기
- **Supervisor API**: `getFromDef("SHELF_11")` 등으로 선반 3D 위치 동기화
- **NODE_WAIT**: 중간 노드 도착 시 자동 정지 → 서버 resume 명령 대기

## MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 (노드 경로 + 타임스텝) |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/control` | Server → AGV | resume 명령 (NODE_WAIT 해제) |
| `/agv/arrived` | AGV → Server | 최종 목표 노드 도착 알림 |
| `/agv/shelf_ack` | AGV → Server | 리프트 동작 완료 알림 |
| `/agv/marker` | AGV → Server | ArUco 마커 인식 (트리거 노드 통과 시) |

## 마커 발행 흐름

```
agv_controller.run() → 매 5 timestep → aruco_detector.detect()
  → 새 마커 감지 시(prev_marker 체크) → mqtt_handler.publish_marker()
  → /agv/marker → 서버 _handle_mqtt_marker() → handle_marker_trigger()
```

## NODE_WAIT 흐름 (수정 19)

```
navigation._on_node_reached() → state="NODE_WAIT", 모터 정지
  → agv_controller._on_intermediate_node() → mqtt_handler.publish_position(node)
  → /agv/arrived (type="robot_position") → 서버 _handle_robot_position()
  → 서버 _try_resume_waiting_robots() → 충돌 없으면 publish_resume(rid)
  → /agv/control → agv_controller._handle_control() → navigation.resume()
```

## 리프트 메커니즘

### 동작 흐름

1. **pickup** 명령 수신 (`/agv/shelf_cmd`)
   - LinearMotor로 리프트 상승
   - Supervisor API로 선반 DEF 노드의 translation을 AGV 위치로 이동
   - 완료 후 `/agv/shelf_ack` 전송

2. **putdown** 명령 수신 (`/agv/shelf_cmd`)
   - LinearMotor로 리프트 하강
   - Supervisor API로 선반을 현재 노드의 그리드 좌표로 복원
   - 완료 후 `/agv/shelf_ack` 전송

### Supervisor API 사용

```python
# 선반 DEF 노드 접근
shelf_node = self.getFromDef("SHELF_11")
translation_field = shelf_node.getField("translation")

# 선반 위치 이동 (AGV 위로)
translation_field.setSFVec3f([agv_x, lift_height, agv_z])

# 선반 위치 복원 (그리드 좌표)
translation_field.setSFVec3f([grid_x, 0.0, grid_z])
```

## 사용 방법

Webots에서 로봇의 controller 필드를 설정:
- 기본 테스트: `agv_controller`
- MQTT 연동 (현재 사용): `agv_mqtt_controller`

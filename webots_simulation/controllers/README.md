# Controllers

Webots 시뮬레이션에서 AGV를 제어하는 컨트롤러 모음

## 폴더 구조

```
controllers/
├── agv_controller/           # 기본 AGV 컨트롤러 (테스트용)
│   └── agv_controller.py
│
└── agv_mqtt_controller/      # MQTT 연동 AGV 컨트롤러 (현재 사용)
    ├── agv_mqtt_controller.py
    ├── paho/                 # paho-mqtt 라이브러리 (로컬)
    └── runtime.ini           # Webots 런타임 설정
```

## 컨트롤러 설명

### `agv_controller/`
- 기본 테스트용 컨트롤러
- MQTT 없이 단독 실행 가능

### `agv_mqtt_controller/` (현재 사용)
- **Supervisor** 모드로 동작 (Robot이 아닌 Supervisor)
- MQTT를 통해 서버와 직접 통신 (bridge 없이)
- `/agv/plan` 토픽에서 경로 계획을 직접 수신하여 주행
- `/agv/shelf_cmd` 토픽에서 선반 리프트 명령 수신
- `/agv/arrived`, `/agv/shelf_ack` 토픽으로 상태 보고
- **리프트 메커니즘**: SliderJoint + LinearMotor로 선반 들어올리기/내려놓기
- **Supervisor API**: `getFromDef("SHELF_9")` 등으로 선반 3D 위치 동기화
- `paho/` 폴더에 paho-mqtt 라이브러리 포함 (Webots 내부 Python 환경용)

## MQTT 토픽

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/plan` | Server → AGV | 경로 계획 (노드 경로 + 타임스텝) |
| `/agv/shelf_cmd` | Server → AGV | 선반 리프트 명령 (pickup/putdown) |
| `/agv/arrived` | AGV → Server | 목표 노드 도착 알림 |
| `/agv/shelf_ack` | AGV → Server | 리프트 동작 완료 알림 |

## 리프트 메커니즘

AGV의 `extensionSlot`에 SliderJoint + LinearMotor를 추가하여 선반을 물리적으로 들어올린다.

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
shelf_node = self.getFromDef("SHELF_9")
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

# Controllers

Webots 시뮬레이션에서 AGV를 제어하는 컨트롤러 모음

## 폴더 구조

```
controllers/
├── agv_controller/           # 기본 AGV 컨트롤러
│   └── agv_controller.py
│
└── agv_mqtt_controller/      # MQTT 연동 AGV 컨트롤러
    ├── agv_mqtt_controller.py
    ├── paho/                 # paho-mqtt 라이브러리 (로컬)
    └── runtime.ini           # Webots 런타임 설정
```

## 컨트롤러 설명

### `agv_controller/`
- 기본 테스트용 컨트롤러
- MQTT 없이 단독 실행 가능

### `agv_mqtt_controller/`
- MQTT를 통해 bridge.py와 통신
- `/agv/lowcmd` 토픽에서 이동 명령 수신
- `/agv/state` 토픽으로 현재 상태 발행
- `paho/` 폴더에 paho-mqtt 라이브러리 포함 (Webots 내부 Python 환경용)

## MQTT 토픽 (v4)

| 토픽 | 방향 | 설명 |
|------|------|------|
| `/agv/lowcmd` | bridge → AGV | 저수준 이동 명령 |
| `/agv/state` | AGV → bridge | 현재 상태 (노드, 상태) |
| `/agv/plan` | server → bridge | 전체 경로 계획 |
| `/agv/shelf_cmd` | server → bridge | 선반 리프트 명령 |
| `/agv/arrived` | bridge → server | 도착 알림 |

## 사용 방법

Webots에서 로봇의 controller 필드를 설정:
- 기본 테스트: `agv_controller`
- MQTT 연동: `agv_mqtt_controller`

## v4 변경사항

- 맵이 9×5 → 7×7+작업대2로 변경됨
- 선반 노드는 통과 불가 (출발/도착만)
- 선반 리프트 명령 추가 (`/agv/shelf_cmd`)
- 로봇 상태 6단계로 확장

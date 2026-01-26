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

## 사용 방법

Webots에서 로봇의 controller 필드를 설정:
- 기본 테스트: `agv_controller`
- MQTT 연동: `agv_mqtt_controller`

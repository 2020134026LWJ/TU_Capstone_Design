# TU_Capstone_Design  
AGV 기반 물류 피킹 시스템 졸업작품

## 📌 프로젝트 개요
본 프로젝트는 **AGV(Automated Guided Vehicle)** 를 활용한  
물류창고 피킹 자동화 시스템을 설계·구현하는 졸업작품이다.  
이동식 선반을 AGV가 자동으로 운반하여 작업 효율을 향상시키는 것을 목표로 한다.

## 🎯 개발 목표
- AGV 기반 선반 이송 시스템 구현
- 아루코 마커 기반 경로 인식 및 주행
- 중앙 서버를 통한 경로 제어 및 작업 관리
- STM32 + Raspberry Pi 기반 제어 시스템 설계

## 🧩 시스템 구성
- **AGV 제어부**: STM32 (모터, 센서, 액추에이터 제어)
- **상위 제어부**: Raspberry Pi (경로 수신, 마커 인식)
- **중앙 서버**: 경로 계산 및 작업 관리
- **통신**: MQTT / WebSocket

## 🛠 사용 기술
- STM32 (CubeIDE)
- Raspberry Pi 5
- C / C++
- Python
- OpenCV (ArUco Marker)
- Git / GitHub

# V2 Single File Server (2025.01.20)

단일 파일 서버 버전 (Webots 시뮬레이션용)

## 파일 목록
- `server.py` - 9x5 그리드 맵 + 다중 로봇 경로 계획

## 특징
- 9x5 그리드 맵 (45 노드)
- Prioritized A* with Time Reservation
- MQTT 발행 (/agv/plan)
- 단일 파일 구조

## 한계점
- 모듈화 없음
- WebSocket 미지원 (Admin UI 연결 불가)
- 하드코딩된 시작/목표 위치

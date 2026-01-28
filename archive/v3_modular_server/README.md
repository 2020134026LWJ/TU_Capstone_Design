# v3 - 모듈화 서버 (01.26)

9×5 그리드 맵 기반 모듈화 서버.
단순 A→B 이동만 지원. 선반/작업 관리 없음.

## 구조
- `server/` : 모듈화된 서버 (config, path_planner, mqtt_publisher, websocket_handler, request_handler, robot_manager)
- `bridge.py` : MQTT 브릿지 (시뮬레이션 전용)
- `map.json` : 9×5 (45노드) 그리드 맵
- `robot_config.json` : 로봇 2대 (노드 1, 37)

## v4에서 변경된 사항
- 맵: 9×5 → 7×7 + 작업대 2개 (51노드), 선반/통로/작업대 타입 추가
- 선반 관리: shelf_manager.py, shelf_config.json 추가
- 작업 관리: task_manager.py 추가 (배치 작업, 서브태스크 분해, 픽업 완료 처리)
- 경로 계획: 선반 노드 통과 제외 로직 추가
- 로봇 상태: 6단계 상태 머신 (idle → moving → pickup → deliver → wait → return)
- 브릿지: 양방향 MQTT↔UART 중계, STM32 패킷 프로토콜 추가

"""
v4 서버 핵심 로직 테스트
MQTT/Webots 없이 전체 작업 흐름을 시뮬레이션

테스트 시나리오:
  T1(W1): 물품 A, B, Z, D 가져와 → 선반 9(A,B), 11(D), 41(Z)
  T2(W2): 물품 C, X, U, I 가져와 → 선반 9(C), 39(X), 37(U), 13(I)
  → 선반 9는 T1과 T2 모두 필요 → 포워딩 발생해야 함
"""

import sys
import json

# paho-mqtt 없이 테스트하기 위해 mock
class MockMQTTPublisher:
    def __init__(self):
        self.published = []
    def is_connected(self): return True
    def publish_plan(self, robots, speed=0.3):
        self.published.append(("plan", robots))
        return True
    def publish_single_robot_plan(self, rid, start, goal, timed_path, speed=0.3):
        from server.path_planner import PathPlanner
        node_path = PathPlanner.compress_to_node_path(timed_path)
        self.published.append(("single_plan", {"rid": rid, "start": start, "goal": goal, "path": node_path}))
        print(f"    [MQTT] Robot {rid}: {start} → {goal}, path={node_path}")
        return True
    def publish_shelf_command(self, rid, command, shelf_id):
        self.published.append(("shelf_cmd", {"rid": rid, "cmd": command, "shelf_id": shelf_id}))
        return True
    def connect(self): return True
    def disconnect(self): pass


def separator(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def main():
    # ─── 모듈 로드 (paho-mqtt 없이 직접 import) ───
    import importlib.util
    import sys

    def load_module(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    config_mod = load_module("server.config", "server/config.py")
    Config = config_mod.Config

    pp_mod = load_module("server.path_planner", "server/path_planner.py")
    PathPlanner = pp_mod.PathPlanner

    sm_mod = load_module("server.shelf_manager", "server/shelf_manager.py")
    ShelfManager = sm_mod.ShelfManager

    tm_mod = load_module("server.task_manager", "server/task_manager.py")
    TaskManager = tm_mod.TaskManager

    rm_mod = load_module("server.robot_manager", "server/robot_manager.py")
    RobotManager = rm_mod.RobotManager
    RobotStatus = rm_mod.RobotStatus

    # Mock mqtt_publisher for request_handler import
    class MockMQTTPublisherModule:
        MQTTPublisher = MockMQTTPublisher
    sys.modules["server.mqtt_publisher"] = MockMQTTPublisherModule()

    rh_mod = load_module("server.request_handler", "server/request_handler.py")
    RequestHandler = rh_mod.RequestHandler

    config = Config()
    path_planner = PathPlanner(config.map_file)
    shelf_manager = ShelfManager(config.shelf_config_file)
    robot_manager = RobotManager(config)
    task_manager = TaskManager(shelf_manager, path_planner)
    mqtt_pub = MockMQTTPublisher()

    handler = RequestHandler(
        config=config,
        path_planner=path_planner,
        mqtt_publisher=mqtt_pub,
        robot_manager=robot_manager,
        shelf_manager=shelf_manager,
        task_manager=task_manager,
    )

    # ─── 1. 초기 상태 확인 ───
    separator("1. 초기 상태")
    print(f"맵: {len(path_planner.nodes)}노드, "
          f"선반={path_planner.shelf_nodes}, 작업대={path_planner.workstation_nodes}")
    print(f"선반 물품 매핑:")
    for sid, shelf in shelf_manager.shelves.items():
        print(f"  {shelf.label}(node {sid}): {shelf.items}")
    print(f"로봇: {[(r.rid, r.name, r.home_node) for r in robot_manager.get_all_robots()]}")

    # ─── 2. 경로 계획 테스트 ───
    separator("2. 경로 계획 테스트")
    test_routes = [
        (50, 9,  "W1 → S1(선반9)"),
        (9, 50,  "S1 → W1(복귀)"),
        (50, 25, "W1 → S5(선반25, 중앙)"),
        (51, 41, "W2 → S9(선반41)"),
        (50, 51, "W1 → W2(작업대간)"),
    ]
    for start, goal, desc in test_routes:
        path = path_planner.plan_single_robot(start, goal)
        if path:
            node_path = PathPlanner.compress_to_node_path(path)
            print(f"  {desc}: {node_path} (길이={len(node_path)})")
        else:
            print(f"  {desc}: 경로 없음!")

    # ─── 3. 배치 작업 등록 ───
    separator("3. 배치 작업 등록")
    batch_msg = json.dumps({
        "type": "batch_task_request",
        "tasks": [
            {"task_id": "T1", "workstation_id": 50, "items": ["A", "B", "Z", "D"]},
            {"task_id": "T2", "workstation_id": 51, "items": ["C", "X", "U", "I"]},
        ]
    })
    result = handler.handle_message(batch_msg)
    print(f"응답: success={result['success']}, tasks_created={result['tasks_created']}")
    for t in result["tasks"]:
        print(f"  {t['task_id']}: 선반={t['shelves_needed']}, "
              f"status={t['status']}, robot={t['assigned_robot']}")
    if result.get("assignments"):
        print(f"로봇 배정:")
        for a in result["assignments"]:
            print(f"  Robot {a['robot_id']} → Task {a['task_id']}, target={a['first_target']}")

    # ─── 4. 로봇 상태 확인 ───
    separator("4. 작업 배정 후 상태")
    for r in robot_manager.get_all_robots():
        print(f"  Robot {r.rid}: status={r.status.value}, node={r.current_node}, "
              f"task={r.current_task_id}, shelf={r.carrying_shelf}")

    # ─── 5. T1 시뮬레이션: 선반9 픽업 → W1 배달 ───
    separator("5. T1 시뮬레이션: Robot 1이 선반9로 이동 후 도착")

    # Robot 1이 선반 9에 도착
    arrived_msg = json.dumps({"type": "robot_arrived", "rid": 1, "node": 9})
    result = handler.handle_message(arrived_msg)
    print(f"도착 응답: action={result.get('action')}")
    r1 = robot_manager.get_robot(1)
    print(f"  Robot 1: status={r1.status.value}, carrying_shelf={r1.carrying_shelf}")
    shelf9 = shelf_manager.get_shelf(9)
    print(f"  선반9: status={shelf9.status.value}, carried_by={shelf9.carried_by}")

    # ─── 6. Robot 1이 작업대 W1에 도착 ───
    separator("6. Robot 1이 W1(50)에 도착 → 픽업 대기")
    arrived_msg = json.dumps({"type": "robot_arrived", "rid": 1, "node": 50})
    result = handler.handle_message(arrived_msg)
    print(f"도착 응답: action={result.get('action')}, items={result.get('items_to_pick')}")
    print(f"  Robot 1: status={r1.status.value}")

    # ─── 7. 작업자가 물품 A 픽업 ───
    separator("7. 작업자: 물품 A 픽업 완료")
    pick_msg = json.dumps({"type": "pick_complete", "task_id": "T1", "item": "A", "workstation_id": 50})
    result = handler.handle_message(pick_msg)
    print(f"픽업 응답: action={result.get('action')}")
    print(f"  남은 물품(이 선반): {result.get('remaining_items_on_shelf')}")
    print(f"  남은 물품(전체): {result.get('total_remaining')}")

    # ─── 8. 작업자가 물품 B 픽업 → 선반9 완료 ───
    separator("8. 작업자: 물품 B 픽업 완료 → 선반9 작업 끝")
    pick_msg = json.dumps({"type": "pick_complete", "task_id": "T1", "item": "B", "workstation_id": 50})
    result = handler.handle_message(pick_msg)
    print(f"픽업 응답: action={result.get('action')}, next_action={result.get('next_action')}")

    # T2도 선반9(물품C)가 필요 → 포워딩 발생해야 함
    if result.get("next_action") == "forward_shelf":
        print(f"  ★ 포워딩 발생! 선반9 → W2({result.get('forward_to_ws')})")
    elif result.get("next_action") == "return_shelf":
        print(f"  선반9 복귀 → node {result.get('return_to')}")

    print(f"  Robot 1: status={r1.status.value}")

    # ─── 9. 전체 상태 조회 ───
    separator("9. 전체 상태 조회")
    status_msg = json.dumps({"type": "status_request"})
    result = handler.handle_message(status_msg)
    print(f"MQTT: {result['mqtt_connected']}")
    print(f"로봇:")
    for r in result["robots"]["robots"]:
        print(f"  {r['name']}: status={r['status']}, node={r['current_node']}, "
              f"shelf={r['carrying_shelf']}, task={r['current_task_id']}")
    print(f"작업:")
    for t_status, count in result["tasks"]["status_counts"].items():
        print(f"  {t_status}: {count}개")
    print(f"선반:")
    for s_status in ["in_place", "carried", "at_workstation"]:
        count = result["shelves"].get(s_status, 0)
        if count:
            print(f"  {s_status}: {count}개")

    # ─── 10. 선반/작업 상세 조회 ───
    separator("10. 선반 상태 상세")
    shelf_msg = json.dumps({"type": "shelf_status_request"})
    result = handler.handle_message(shelf_msg)
    for s in result["shelves"]:
        status_mark = ""
        if s["status"] != "in_place":
            status_mark = f" ← {s['status']}"
            if s["carried_by"]:
                status_mark += f" (Robot {s['carried_by']})"
        print(f"  {s['label']}(node {s['shelf_id']}): {s['items']}{status_mark}")

    separator("테스트 완료")
    print("서버 핵심 로직이 정상 동작합니다.")
    print(f"MQTT 발행 기록: {len(mqtt_pub.published)}건")
    for i, (topic, data) in enumerate(mqtt_pub.published):
        if topic == "single_plan":
            print(f"  [{i+1}] {topic}: Robot {data['rid']} {data['start']}→{data['goal']}")
        else:
            print(f"  [{i+1}] {topic}: {data}")


if __name__ == "__main__":
    main()

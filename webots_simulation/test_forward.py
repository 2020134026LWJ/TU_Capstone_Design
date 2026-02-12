"""
선반 포워딩 테스트 - 서버 로직 단위 검증 (Webots/MQTT 불필요)

두 사용자 주문에 겹치는 선반이 있을 때:
  1. 사용자1이 선반 픽업 완료 → 포워딩 결정되는지
  2. FORWARD_SHELF 도착 처리가 정상 동작하는지
  3. 사용자2의 GO_TO_SHELF 목적지가 수정되는지
  4. start==goal (이미 작업대에 있는 선반) 처리가 되는지

실행:
    cd webots_simulation
    python test_forward.py
"""

import os
import sys
import json

# 서버 모듈 임포트
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from server.config import Config
from server.path_planner import PathPlanner
from server.shelf_manager import ShelfManager
from server.robot_manager import RobotManager, RobotStatus
from server.task_manager import TaskManager, SubTaskType, TaskStatus


def create_test_system():
    """테스트용 시스템 초기화 (MQTT/WebSocket 없이)"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config = Config(base_dir=base_dir)

    path_planner = PathPlanner(os.path.join(base_dir, "config", "map.json"))
    shelf_manager = ShelfManager(os.path.join(base_dir, "config", "shelf_config.json"))
    robot_manager = RobotManager(config)
    task_manager = TaskManager(shelf_manager, path_planner)

    return config, path_planner, shelf_manager, robot_manager, task_manager


def print_header(text):
    print(f"\n{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}")


def print_step(num, text):
    print(f"\n--- Step {num}: {text} ---")


def print_subtask_list(task, highlight_idx=None):
    """서브태스크 목록 출력"""
    for i, st in enumerate(task.subtasks):
        marker = " >>>" if i == highlight_idx else "    "
        status = st.status.value[:4]
        items = f" items={st.items_to_pick}" if st.items_to_pick else ""
        print(f"{marker} [{status}] ST{i}: {st.subtask_type.value} "
              f"shelf={st.shelf_id} target={st.target_node}{items}")


def test_forwarding():
    """포워딩 시나리오 테스트 (4x8 맵)"""
    config, planner, shelf_mgr, robot_mgr, task_mgr = create_test_system()

    print_header("선반 포워딩 테스트")
    print("  사용자1 (WS33): 드롭스(선반11) + 퍼지(선반15)")
    print("  사용자2 (WS34): 롤리팝(선반11) + 구미(선반20)")
    print("  → 선반11 겹침: 사용자1 완료 후 WS34로 포워딩 예상")

    # ──── Task 생성 ────
    print_step(1, "작업 생성 (batch)")

    t1 = task_mgr.create_task(
        task_id="T1", workstation_id=33,
        items=["드롭스", "퍼지"],
        optimized_shelf_sequence=[11, 15],
    )
    t2 = task_mgr.create_task(
        task_id="T2", workstation_id=34,
        items=["롤리팝", "구미"],
        optimized_shelf_sequence=[20, 11],
    )

    assert t1 and t2, "작업 생성 실패"
    print(f"  T1 shelves: {t1.shelf_sequence}")
    print(f"  T2 shelves: {t2.shelf_sequence}")

    # shelf_demand 확인
    demand_11 = task_mgr.shelf_demand.get(11, [])
    print(f"\n  선반11 수요: {len(demand_11)}개")
    for d in demand_11:
        print(f"    - task={d['task_id']}, ws={d['workstation_id']}, items={d['items']}")
    assert len(demand_11) == 2, f"선반11 수요가 2개여야 함, 실제: {len(demand_11)}"

    # ──── 로봇 배정 ────
    print_step(2, "로봇 배정")

    r1 = robot_mgr.get_robot(1)
    r2 = robot_mgr.get_robot(2)

    # T1 → AGV-1, T2 → AGV-2
    first_st1 = task_mgr.start_task("T1", 1)
    robot_mgr.set_robot_status(1, RobotStatus.MOVING_TO_SHELF)
    r1.current_task_id = "T1"

    first_st2 = task_mgr.start_task("T2", 2)
    robot_mgr.set_robot_status(2, RobotStatus.MOVING_TO_SHELF)
    r2.current_task_id = "T2"

    print(f"  AGV-1: {r1.current_node} → {first_st1.target_node} (선반11)")
    print(f"  AGV-2: {r2.current_node} → {first_st2.target_node} (선반20)")

    # ──── AGV-1: 선반11 도착 → 픽업 → 배달 → 대기 ────
    print_step(3, "AGV-1: 선반11 도착 → 픽업 → WS33 배달 → 대기")

    # GO_TO_SHELF 완료
    robot_mgr.update_robot_position(1, 11)
    result = task_mgr.handle_subtask_complete("T1")
    # PICKUP_SHELF
    shelf_mgr.mark_shelf_picked_up(11, 1)
    robot_mgr.set_carrying_shelf(1, 11)
    result = task_mgr.handle_subtask_complete("T1")
    # DELIVER_TO_WS
    robot_mgr.update_robot_position(1, 33)
    shelf_mgr.mark_shelf_at_workstation(11, 33)
    result = task_mgr.handle_subtask_complete("T1")
    # WAIT_PICKING
    robot_mgr.set_robot_status(1, RobotStatus.WAITING_FOR_PICK)

    current = t1.get_current_subtask()
    print(f"  현재 서브태스크: {current.subtask_type.value}, items={current.items_to_pick}")
    assert current.subtask_type == SubTaskType.WAIT_PICKING
    assert "드롭스" in current.items_to_pick

    # ──── AGV-2: 선반20 도착 → 픽업 → 배달 → 대기 (병렬) ────
    print_step(4, "AGV-2: 선반20 도착 → 픽업 → WS34 배달 → 대기")

    robot_mgr.update_robot_position(2, 20)
    task_mgr.handle_subtask_complete("T2")
    shelf_mgr.mark_shelf_picked_up(20, 2)
    robot_mgr.set_carrying_shelf(2, 20)
    task_mgr.handle_subtask_complete("T2")
    robot_mgr.update_robot_position(2, 34)
    shelf_mgr.mark_shelf_at_workstation(20, 34)
    task_mgr.handle_subtask_complete("T2")
    robot_mgr.set_robot_status(2, RobotStatus.WAITING_FOR_PICK)

    current2 = t2.get_current_subtask()
    print(f"  현재 서브태스크: {current2.subtask_type.value}, items={current2.items_to_pick}")
    assert current2.subtask_type == SubTaskType.WAIT_PICKING

    # ──── 사용자1: 드롭스 픽업 완료 → 포워딩 결정 ────
    print_step(5, "사용자1: '드롭스' 픽업 완료 → 포워딩 결정?")

    result = task_mgr.handle_item_picked("T1", "드롭스")
    print(f"  결과: {json.dumps(result, ensure_ascii=False, indent=2)}")

    assert result["action"] == "shelf_done", f"Expected shelf_done, got {result['action']}"
    assert result["next_action"] == "forward", f"Expected forward, got {result.get('next_action')}"
    assert result["forward_to_ws"] == 34, f"Expected forward to 34, got {result.get('forward_to_ws')}"

    print(f"\n  -> 포워딩 결정됨! 선반11 → WS34로 포워딩")

    # RETURN_SHELF이 FORWARD_SHELF로 변환되었는지 확인
    forward_st = t1.get_current_subtask()
    print(f"  현재 서브태스크: {forward_st.subtask_type.value}, target={forward_st.target_node}")
    assert forward_st.subtask_type == SubTaskType.FORWARD_SHELF
    assert forward_st.target_node == 34

    # ──── AGV-1: 선반11를 WS34로 운반 → FORWARD_SHELF 도착 ────
    print_step(6, "AGV-1: 선반11 → WS34 운반 (FORWARD_SHELF)")

    robot_mgr.set_robot_status(1, RobotStatus.DELIVERING_TO_WS)
    robot_mgr.update_robot_position(1, 34)

    # FORWARD_SHELF 도착 처리
    shelf_mgr.mark_shelf_at_workstation(11, 34)
    robot_mgr.set_carrying_shelf(1, None)

    # 상대방 작업 수정
    modified = task_mgr.handle_shelf_forwarded(11, 34)
    print(f"  handle_shelf_forwarded 결과: {modified}")
    assert modified == "T2", f"Expected T2 modified, got {modified}"

    # T2의 GO_TO_SHELF(선반11) 목적지 확인
    for st in t2.subtasks:
        if st.shelf_id == 11 and st.subtask_type == SubTaskType.GO_TO_SHELF:
            print(f"  T2 GO_TO_SHELF(선반11): target_node = {st.target_node}")
            assert st.target_node == 34, f"Expected target 34, got {st.target_node}"
            print(f"  -> T2의 GO_TO_SHELF 목적지가 34(WS2)로 변경됨!")

    # AGV-1의 작업 진행 (다음 선반 or 완료)
    result = task_mgr.handle_subtask_complete("T1")
    print(f"  AGV-1 다음: {result['action']}")

    if result["action"] == "next_subtask":
        next_st = t1.get_current_subtask()
        print(f"  AGV-1 다음 선반: {next_st.subtask_type.value}, target={next_st.target_node}")

    # ──── 사용자2: 구미 픽업 완료 → 선반20 복귀 ────
    print_step(7, "사용자2: '구미' 픽업 완료 → 선반20 복귀")

    result = task_mgr.handle_item_picked("T2", "구미")
    print(f"  결과: action={result['action']}, next={result.get('next_action')}")
    assert result["next_action"] == "return", f"Expected return, got {result.get('next_action')}"

    # 선반20 복귀
    robot_mgr.set_robot_status(2, RobotStatus.RETURNING_SHELF)
    return_to = result.get("return_to", 20)
    robot_mgr.update_robot_position(2, return_to)
    shelf_mgr.mark_shelf_returned(20, return_to)
    robot_mgr.set_carrying_shelf(2, None)

    result = task_mgr.handle_subtask_complete("T2")
    print(f"  AGV-2 다음: {result['action']}")

    # ──── AGV-2: 포워딩된 선반11를 WS34에서 수령 ────
    print_step(8, "AGV-2: 포워딩된 선반11 수령 (이미 WS34에 있음)")

    next_st = t2.get_current_subtask()
    print(f"  현재 서브태스크: {next_st.subtask_type.value}, "
          f"shelf={next_st.shelf_id}, target={next_st.target_node}")
    assert next_st.subtask_type == SubTaskType.GO_TO_SHELF
    assert next_st.target_node == 34, "GO_TO_SHELF target should be 34 (forwarded)"

    # AGV-2의 현재 위치 (선반20 복귀 후 위치)
    print(f"  AGV-2 현재 위치: {r2.current_node}")
    print(f"  GO_TO_SHELF 목적지: {next_st.target_node}")

    # 시나리오: AGV-2가 WS34(노드34)에 있는 경우 → start == goal
    if r2.current_node == next_st.target_node:
        print(f"  ! start == goal! (둘 다 {r2.current_node})")
        print(f"  → _plan_and_publish_move에서 즉시 도착 처리 필요")
    else:
        print(f"  AGV-2가 {r2.current_node} → {next_st.target_node}로 이동 필요")

    # GO_TO_SHELF(34) 도착 시뮬레이션
    robot_mgr.update_robot_position(2, 34)
    task_mgr.handle_subtask_complete("T2")  # GO_TO_SHELF done

    # PICKUP_SHELF
    next_st = t2.get_current_subtask()
    assert next_st.subtask_type == SubTaskType.PICKUP_SHELF
    shelf_mgr.mark_shelf_picked_up(11, 2)
    robot_mgr.set_carrying_shelf(2, 11)
    task_mgr.handle_subtask_complete("T2")  # PICKUP done

    # DELIVER_TO_WS(34) - 이미 34에 있으므로 start == goal
    next_st = t2.get_current_subtask()
    assert next_st.subtask_type == SubTaskType.DELIVER_TO_WS
    assert next_st.target_node == 34
    print(f"  DELIVER_TO_WS target={next_st.target_node}, AGV-2 at={r2.current_node}")
    print(f"  → start == goal! 즉시 도착 처리됨")

    # 도착 처리
    shelf_mgr.mark_shelf_at_workstation(11, 34)
    task_mgr.handle_subtask_complete("T2")  # DELIVER done

    # WAIT_PICKING
    next_st = t2.get_current_subtask()
    assert next_st.subtask_type == SubTaskType.WAIT_PICKING
    assert "롤리팝" in next_st.items_to_pick
    robot_mgr.set_robot_status(2, RobotStatus.WAITING_FOR_PICK)

    print(f"  -> AGV-2 WAIT_PICKING 상태, items={next_st.items_to_pick}")

    # ──── 사용자2: 롤리팝 픽업 완료 → 선반11 복귀 ────
    print_step(9, "사용자2: '롤리팝' 픽업 완료 → 선반11 복귀")

    result = task_mgr.handle_item_picked("T2", "롤리팝")
    print(f"  결과: action={result['action']}, next={result.get('next_action')}")
    assert result["next_action"] == "return"

    # 선반11 복귀
    return_to = result.get("return_to", 11)
    robot_mgr.update_robot_position(2, return_to)
    shelf_mgr.mark_shelf_returned(11, return_to)
    robot_mgr.set_carrying_shelf(2, None)
    result = task_mgr.handle_subtask_complete("T2")

    print(f"  T2 최종 상태: {result['action']}")
    assert result["action"] == "task_complete", f"Expected task_complete, got {result['action']}"

    # ──── 최종 확인 ────
    print_header("테스트 결과")

    t1_status = t1.status.value
    t2_status = t2.status.value

    # T1은 아직 선반15 작업이 남아있을 수 있음
    print(f"  T1 상태: {t1_status} (현재 서브태스크: {t1.current_subtask_idx}/{len(t1.subtasks)})")
    print(f"  T2 상태: {t2_status} (현재 서브태스크: {t2.current_subtask_idx}/{len(t2.subtasks)})")

    assert t2.status == TaskStatus.COMPLETED, f"T2 should be completed, got {t2_status}"

    print(f"\n  선반11 최종 위치: node {shelf_mgr.get_shelf(11).current_node}, "
          f"status={shelf_mgr.get_shelf(11).status.value}")
    print(f"  선반20 최종 위치: node {shelf_mgr.get_shelf(20).current_node}, "
          f"status={shelf_mgr.get_shelf(20).status.value}")

    print(f"\n  -> 모든 포워딩 테스트 통과!")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    test_forwarding()

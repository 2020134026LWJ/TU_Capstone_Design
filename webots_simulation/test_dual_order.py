"""
2명 작업자 동시 주문 테스트
- 사용자1, 사용자2가 동시에 주문
- 2대의 AGV가 동시에 이동 (prioritized planning)
"""

import asyncio
import json
import websockets

WS_URL = "ws://localhost:8765"


async def test_dual_order():
    """두 작업자 동시 주문 테스트"""
    async with websockets.connect(WS_URL) as ws:
        print("=" * 60)
        print("  2명 작업자 동시 주문 테스트")
        print("=" * 60)

        # 1. 초기 상태 확인
        await ws.send(json.dumps({"type": "status_request"}))
        resp = json.loads(await ws.recv())
        print("\n[초기 상태]")
        for r in resp["robots"]["robots"]:
            print(f"  {r['name']}: node={r['current_node']}, status={r['status']}")

        # 2. 두 작업자 동시 주문 (batch_task_request 사용)
        print("\n[동시 주문 등록]")
        batch_request = {
            "type": "batch_task_request",
            "tasks": [
                {"task_id": "USER1_ORDER", "workstation_id": 33, "items": ["드롭스", "퍼지"]},
                {"task_id": "USER2_ORDER", "workstation_id": 34, "items": ["구미", "무설탕 캔디"]},
            ]
        }
        await ws.send(json.dumps(batch_request))
        resp = json.loads(await ws.recv())

        print(f"  등록 결과: success={resp.get('success')}, tasks_created={resp.get('tasks_created')}")
        for task in resp.get("tasks", []):
            print(f"    {task['task_id']}: robot={task.get('assigned_robot')}, shelves={task.get('shelves_needed')}")

        if resp.get("assignments"):
            print("\n[로봇 배정 (동시 이동)]")
            for a in resp["assignments"]:
                print(f"  Robot {a['robot_id']} -> Task {a['task_id']}, target={a['first_target']}, planned={a['path_planned']}")

        # 3. 로봇 상태 확인
        await ws.send(json.dumps({"type": "status_request"}))
        resp = json.loads(await ws.recv())
        print("\n[주문 후 로봇 상태]")
        for r in resp["robots"]["robots"]:
            print(f"  {r['name']}: node={r['current_node']}, status={r['status']}, task={r['current_task_id']}")

        print("\n" + "=" * 60)
        print("  두 로봇이 동시에 움직이는지 Webots에서 확인하세요!")
        print("=" * 60)


async def test_sequential_orders():
    """두 작업자 순차 주문 테스트 (start_order 사용)"""
    async with websockets.connect(WS_URL) as ws:
        print("=" * 60)
        print("  2명 작업자 순차 주문 테스트")
        print("=" * 60)

        # 1. 사용자1 주문
        print("\n[사용자1 주문]")
        await ws.send(json.dumps({"type": "start_order", "사용자ID": 1, "주문번호": 1}))
        resp = json.loads(await ws.recv())
        print(f"  응답: success={resp.get('success')}, items={resp.get('items')}")

        # 2. 사용자2 주문 (바로 연속해서)
        print("\n[사용자2 주문]")
        await ws.send(json.dumps({"type": "start_order", "사용자ID": 2, "주문번호": 1}))
        resp = json.loads(await ws.recv())
        print(f"  응답: success={resp.get('success')}, items={resp.get('items')}")

        # 3. 로봇 상태 확인
        await ws.send(json.dumps({"type": "status_request"}))
        resp = json.loads(await ws.recv())
        print("\n[주문 후 로봇 상태]")
        for r in resp["robots"]["robots"]:
            print(f"  {r['name']}: node={r['current_node']}, status={r['status']}, task={r['current_task_id']}")


if __name__ == "__main__":
    import sys

    print("사용법:")
    print("  python test_dual_order.py        # 동시 주문 테스트 (batch)")
    print("  python test_dual_order.py seq    # 순차 주문 테스트 (start_order)")
    print()

    if len(sys.argv) > 1 and sys.argv[1] == "seq":
        asyncio.run(test_sequential_orders())
    else:
        asyncio.run(test_dual_order())

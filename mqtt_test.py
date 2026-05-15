"""
MQTT 테스트 클라이언트 - CLI 기반 GUI 시뮬레이터

데모 흐름 (자동 체인):
    > 시작                 사용자1,2 첫 주문 동시 시작 (자동 체인 활성화)
    (AGV가 선반 가져오면)
    > 완료 1               사용자1 선반 픽업 완료 (수동)
    > 완료 2               사용자2 선반 픽업 완료
    > 주문완료 1           사용자1 현재 주문 종료 → 다음 주문 자동 시작
    > 주문완료 2           사용자2 현재 주문 종료 → 다음 주문 자동 시작

수동 모드 (디버깅용):
    > 시작 1 3             사용자1 주문3 단독 시작 (체인 비활성화)
    > 주문완료 1 2         사용자1 주문2 종료 (수동, 자동 체인 안 함)

테스트 시나리오:
    주문1 (포워딩):       시작 (1-2 공유)
    주문2 (인터셉트):     주문완료 1 → 즉시 주문완료 2 (시간차)
    주문3 (staging):      사용자1만 (사용자2는 주문3 없음 → 자동 스킵)
    주문4 (PICK 차단):    완료 1 누르지 말고 사용자2 주문4 진행
"""

import json
import os
import sys
import time
import paho.mqtt.client as mqtt

# DB에서 주문 ID 목록 로드용
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server.db_loader import DBLoader

MQTT_BROKER = "localhost"
MQTT_PORT = 1883

TOPIC_ORDER_START    = "warehouse/order/start"
TOPIC_SHELF_COMPLETE = "warehouse/shelf/complete"
TOPIC_ORDER_COMPLETE = "warehouse/order/complete"


class MQTTCLIClient:

    def __init__(self):
        self.client = mqtt.Client()
        self.connected = False

    def connect(self):
        try:
            self.client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self.client.loop_start()
            time.sleep(0.3)
            self.connected = True
            print(f"  MQTT 브로커 연결됨 ({MQTT_BROKER}:{MQTT_PORT})")
            return True
        except Exception as e:
            print(f"  MQTT 연결 실패: {e}")
            print("  MQTT 브로커를 먼저 실행하세요: mosquitto -v")
            return False

    def _publish(self, topic, msg):
        payload = json.dumps(msg, ensure_ascii=False)
        self.client.publish(topic, payload)
        print(f"  [발행→{topic}] {payload}")

    def start_order(self, user_id, order_id=1):
        self._publish(TOPIC_ORDER_START, {
            "사용자ID": user_id,
            "주문번호": order_id,
        })

    def shelf_complete(self, user_id):
        self._publish(TOPIC_SHELF_COMPLETE, {
            "사용자ID": user_id,
        })

    def order_complete(self, user_id, order_id=1):
        self._publish(TOPIC_ORDER_COMPLETE, {
            "사용자ID": user_id,
            "주문번호": order_id,
        })

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()


class OrderChain:
    """사용자별 주문 ID 목록을 관리하고 자동 체인 진행을 처리"""

    def __init__(self, db_dir: str):
        self.loader = DBLoader(db_dir)
        # 사용자별 주문 ID 리스트 (정렬됨)
        self.orders = {
            1: self.loader.get_all_orders(1),
            2: self.loader.get_all_orders(2),
        }
        # 사용자별 다음 발행할 주문 인덱스
        self.next_idx = {1: 0, 2: 0}
        # 사용자별 현재 진행 중인 주문 ID (주문완료 자동 발행용)
        self.current = {1: None, 2: None}
        self.chain_active = False

    def start_all(self, client: MQTTCLIClient):
        """자동 체인 시작: 두 사용자의 첫 주문 동시 발행"""
        self.next_idx = {1: 0, 2: 0}
        self.current = {1: None, 2: None}
        self.chain_active = True
        print(f"  [체인] 사용자1 주문 목록: {self.orders[1]}")
        print(f"  [체인] 사용자2 주문 목록: {self.orders[2]}")
        self._advance(client, 1)
        self._advance(client, 2)

    def advance_after_complete(self, client: MQTTCLIClient, user_id: int):
        """주문완료 명령 시 현재 주문 종료 + 다음 주문 자동 시작"""
        if not self.chain_active:
            return False
        current_order = self.current.get(user_id)
        if current_order is None:
            print(f"  [체인] 사용자{user_id}: 진행 중 주문 없음")
            return False
        client.order_complete(user_id, current_order)
        self.current[user_id] = None
        self._advance(client, user_id)
        return True

    def _advance(self, client: MQTTCLIClient, user_id: int):
        """사용자의 next_idx 위치 주문 발행 후 인덱스 증가"""
        idx = self.next_idx[user_id]
        order_list = self.orders[user_id]
        if idx >= len(order_list):
            print(f"  [체인] 사용자{user_id}: 모든 주문 완료")
            return
        order_id = order_list[idx]
        self.next_idx[user_id] += 1
        self.current[user_id] = order_id
        print(f"  [체인] 사용자{user_id} 주문{order_id} 시작 ({idx+1}/{len(order_list)})")
        client.start_order(user_id, order_id)


def print_help():
    print(f"""
{'='*60}
  AGV 물류 시스템 - MQTT 테스트 CLI
{'='*60}
  데모 자동 체인:
    시작                     사용자1,2 첫 주문 동시 시작 (체인 활성화)
    완료 <user>              선반 픽업 완료 (수동 유지)
    주문완료 <user>          현재 주문 종료 + 다음 주문 자동 시작

  수동 모드 (디버깅):
    시작 <user> [order]      특정 주문만 단독 시작
    주문완료 <user> <order>  특정 주문 명시적 종료 (체인 진행 안 함)

  종료                       프로그램 종료
{'─'*60}
  주문1 (포워딩):     시작 (1-2 공유)
  주문2 (인터셉트):   주문완료 1 → 즉시 주문완료 2 (시간차)
  주문3 (staging):    사용자1만 (사용자2 주문3 없음 → 자동 스킵)
  주문4 (PICK 차단):  완료 1 누르지 말고 진행
{'='*60}""")


def main():
    client = MQTTCLIClient()

    if not client.connect():
        return

    # DB에서 사용자별 주문 목록 로드
    db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server", "Database")
    chain = OrderChain(db_dir)

    print_help()

    try:
        while True:
            try:
                cmd = input("\n> ").strip()
            except EOFError:
                break

            if not cmd:
                continue

            if cmd in ("종료", "quit", "q"):
                break

            elif cmd in ("도움", "help", "?"):
                print_help()

            # "시작" 단독 → 자동 체인 시작
            elif cmd == "시작":
                chain.start_all(client)

            elif cmd.startswith("주문완료"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    user_id = int(parts[1])
                    # order_id 명시 → 수동 모드 (체인 진행 안 함)
                    if len(parts) >= 3 and parts[2].isdigit():
                        client.order_complete(user_id, int(parts[2]))
                    else:
                        # 체인 모드: 현재 주문 종료 + 다음 주문 자동 시작
                        if not chain.advance_after_complete(client, user_id):
                            print(f"  체인 미활성 — 사용법: 주문완료 {user_id} <order>")
                else:
                    print("  사용법: 주문완료 <user> [order]")

            elif cmd.startswith("시작"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    order_id = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
                    # 수동 시작은 체인 비활성화 (의도된 단독 테스트)
                    chain.chain_active = False
                    client.start_order(int(parts[1]), order_id)
                else:
                    print("  사용법: 시작 [user] [order]  /  시작 (단독 = 자동 체인)")

            elif cmd.startswith("완료"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    client.shelf_complete(int(parts[1]))
                else:
                    print("  사용법: 완료 <user>")

            else:
                print("  '시작' (체인) / '시작 <user> [order]' (단독) / '완료 <user>' / '주문완료 <user> [order]' / '종료'")

    except KeyboardInterrupt:
        print("\n")
    finally:
        client.disconnect()
        print("  종료됨")


if __name__ == "__main__":
    main()

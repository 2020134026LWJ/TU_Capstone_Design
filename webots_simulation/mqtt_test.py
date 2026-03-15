"""
MQTT 테스트 클라이언트 - CLI 기반 GUI 시뮬레이터

사용법:
    python mqtt_test.py

흐름:
    > 시작 1               사용자1 주문 시작
    > 시작 2               사용자2 주문 시작
    (AGV가 선반 가져오면)
    > 완료 1               사용자1 WS 선반 수령 완료 → AGV 반납/포워딩
    > 완료 2               사용자2 WS 선반 수령 완료 → AGV 반납/포워딩
    (선반마다 완료 누르면 됨 - 마지막 선반 완료 시 자동 종료)
"""

import json
import time
import paho.mqtt.client as mqtt

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

    def start_order(self, user_id):
        self._publish(TOPIC_ORDER_START, {
            "사용자ID": user_id,
            "주문번호": 1,
        })

    def shelf_complete(self, user_id):
        self._publish(TOPIC_SHELF_COMPLETE, {
            "사용자ID": user_id,
        })

    def order_complete(self, user_id):
        self._publish(TOPIC_ORDER_COMPLETE, {
            "사용자ID": user_id,
            "주문번호": 1,
        })

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()


def print_help():
    print(f"""
{'='*50}
  AGV 물류 시스템 - MQTT 테스트 CLI
{'='*50}
  시작 1/2      사용자1/2 주문 시작  (warehouse/order/start)
  완료 1/2      선반 완료 → 반납     (warehouse/shelf/complete)
  주문완료 1/2  주문 전체 완료       (warehouse/order/complete)
  종료          프로그램 종료
{'='*50}""")


def main():
    client = MQTTCLIClient()

    if not client.connect():
        return

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

            elif cmd.startswith("시작"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    client.start_order(int(parts[1]))
                else:
                    print("  사용법: 시작 1 또는 시작 2")

            elif cmd.startswith("완료"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    client.shelf_complete(int(parts[1]))
                else:
                    print("  사용법: 완료 1 또는 완료 2")

            elif cmd.startswith("주문완료"):
                parts = cmd.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    client.order_complete(int(parts[1]))
                else:
                    print("  사용법: 주문완료 1 또는 주문완료 2")

            else:
                print("  '시작 1/2' / '완료 1/2' / '종료'")

    except KeyboardInterrupt:
        print("\n")
    finally:
        client.disconnect()
        print("  종료됨")


if __name__ == "__main__":
    main()

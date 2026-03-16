#!/usr/bin/env python3
"""
창고 관리 서버 (노트북) - v2
- start_order 시점에 재고 차감 (동시 주문 방지)
- HTTP API: 피킹 리스트 제공
- MQTT: 주문 시작/완료 수신
- [테스트용] 선반 도착 신호 수동 전송 (콘솔 입력)
  → 추후 알고리즘 코드가 직접 shelf_arrived를 전송하면 서버 코드는 사용 안 함
"""
import sqlite3
import json
import threading
import time
from openpyxl import load_workbook
from flask import Flask, jsonify, request

# MQTT 라이브러리
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("⚠️  paho-mqtt 라이브러리가 필요합니다: pip install paho-mqtt")
    mqtt = None


class WarehouseServer:
    """창고 관리 서버"""
    
    def __init__(self, db_path='warehouse.db', mqtt_broker='localhost', mqtt_port=1883, http_port=5000):
        self.db_path = db_path
        self.mqtt_broker = mqtt_broker
        self.mqtt_port = mqtt_port
        self.http_port = http_port
        self.mqtt_client = None
        
        # 사용자별 현재 주문 추적
        self.current_orders = {}  # {user_id: order_number}
        
        # 사용자별 피킹리스트의 선반 목록 추적 (테스트용 선택지 제공)
        # {user_id: ["1-1", "2-3", ...]}
        self.user_shelf_groups = {}
        
        # DB 연결 풀 (스레드 안전)
        self.db_lock = threading.Lock()
        
        # Flask 앱
        self.app = Flask(__name__)
        self.setup_routes()
        
    def setup_routes(self):
        """Flask 라우트 설정"""
        
        @self.app.route('/api/picking/user/<int:user_id>/order/<int:order_number>', methods=['GET'])
        def get_picking_list(user_id, order_number):
            picking_list = self.generate_picking_list(user_id, order_number)
            total_orders = self.get_total_orders(user_id)
            if picking_list:
                return jsonify({
                    'status': 'success',
                    '사용자ID': user_id,
                    '주문번호': order_number,
                    '총주문수': total_orders,
                    '피킹리스트': picking_list
                }), 200
            else:
                return jsonify({
                    'status': 'error',
                    'message': '주문을 찾을 수 없습니다'
                }), 404
        
        @self.app.route('/api/inventory/status', methods=['GET'])
        def get_inventory_status():
            status = self.get_inventory_status()
            return jsonify(status), 200
        
        @self.app.route('/health', methods=['GET'])
        def health_check():
            return jsonify({'status': 'ok', 'service': 'warehouse_server'}), 200
    
    def init_mqtt(self):
        """MQTT 클라이언트 초기화"""
        if mqtt is None:
            print("❌ MQTT 라이브러리 없음")
            return False
        
        self.mqtt_client = mqtt.Client(client_id="warehouse_server")
        self.mqtt_client.on_connect = self.on_mqtt_connect
        self.mqtt_client.on_message = self.on_mqtt_message
        
        try:
            self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port, 60)
            self.mqtt_client.loop_start()
            print(f"✅ MQTT 브로커 연결: {self.mqtt_broker}:{self.mqtt_port}")
            return True
        except Exception as e:
            print(f"❌ MQTT 연결 실패: {e}")
            return False
    
    def on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT 연결 콜백"""
        if rc == 0:
            print("✅ MQTT 브로커 연결 성공")
            client.subscribe("warehouse/order/start")
            client.subscribe("warehouse/shelf/complete")
            client.subscribe("warehouse/order/complete")
            client.subscribe("warehouse/order/all_complete")
            print("📡 토픽 구독 완료")
        else:
            print(f"❌ MQTT 연결 실패: {rc}")
    
    def on_mqtt_message(self, client, userdata, msg):
        """MQTT 메시지 수신 콜백"""
        topic = msg.topic
        payload = json.loads(msg.payload.decode())
        
        print(f"\n📩 MQTT 수신: {topic}")
        print(f"   데이터: {payload}")
        
        if topic == "warehouse/order/start":
            self.handle_order_start(payload)
        elif topic == "warehouse/shelf/complete":
            self.handle_shelf_complete(payload)
        elif topic == "warehouse/order/complete":
            self.handle_order_complete(payload)
        elif topic == "warehouse/order/all_complete":
            self.handle_all_orders_complete(payload)
    
    def handle_order_start(self, data):
        """주문 시작 처리 - 즉시 재고 차감 + 선반 목록 준비"""
        user_id = data.get('사용자ID')
        order_number = data.get('주문번호')
        
        print(f"🚀 주문 시작: 사용자{user_id}, 주문번호{order_number}")
        
        success = self.deduct_inventory(user_id, order_number)
        
        if success:
            print(f"✅ 재고 즉시 차감 완료")
            self.current_orders[user_id] = order_number
            
            # [테스트용] 해당 주문의 선반 그룹 목록 추출해서 저장
            picking_list = self.generate_picking_list(user_id, order_number)
            shelf_groups = self.extract_shelf_groups(picking_list)
            self.user_shelf_groups[user_id] = shelf_groups
            
            print(f"   → 사용자{user_id} 선반 목록: {shelf_groups}")
            print(f"   → 콘솔에서 선반 도착 신호를 전송하세요 (명령어: s)")
        else:
            print(f"❌ 재고 부족! 주문을 처리할 수 없습니다.")
    
    def handle_shelf_complete(self, data):
        """선반 피킹 완료 처리"""
        user_id = data.get('사용자ID')
        shelf_number = data.get('선반번호')
        print(f"📦 선반 완료: 사용자{user_id}, 선반{shelf_number}")
    
    def handle_order_complete(self, data):
        """주문 완료 처리"""
        user_id = data.get('사용자ID')
        order_number = data.get('주문번호')
        print(f"🎉 주문 완료: 사용자{user_id}, 주문번호{order_number}")
        
        # 선반 목록 초기화
        if user_id in self.user_shelf_groups:
            del self.user_shelf_groups[user_id]
    
    def handle_all_orders_complete(self, data):
        """전체 주문 완료 처리"""
        user_id = data.get('사용자ID')
        total = data.get('총주문수')
        print(f"🏁 사용자{user_id} 전체 주문 완료! (총 {total}건)")
        
        # 현재 주문 추적 초기화
        if user_id in self.current_orders:
            del self.current_orders[user_id]
    
    def extract_shelf_groups(self, picking_list):
        """피킹리스트에서 중복 없는 선반 그룹(구역-열) 목록 추출"""
        seen = set()
        groups = []
        for item in picking_list:
            shelf = item['선반번호']
            group = '-'.join(shelf.split('-')[:2])  # "1-1-2" → "1-1"
            if group not in seen:
                seen.add(group)
                groups.append(group)
        return groups
    
    def send_shelf_arrived(self, user_id, shelf_group):
        """
        [테스트용] 선반 도착 신호 MQTT 전송
        → 추후 알고리즘 코드가 직접 이 메시지를 전송함
        """
        if not self.mqtt_client:
            print("❌ MQTT 미연결")
            return
        
        msg = {
            "type": "shelf_arrived",
            "사용자ID": user_id,
            "선반번호": shelf_group
        }
        
        self.mqtt_client.publish(
            "warehouse/shelf/arrived",
            json.dumps(msg, ensure_ascii=False)
        )
        print(f"✅ shelf_arrived 전송: 사용자{user_id}, 선반{shelf_group}")
    
    def run_test_console(self):
        """
        [테스트용] 콘솔 입력으로 선반 도착 신호 수동 전송
        
        명령어:
          s  → 선반 도착 신호 전송 (사용자ID, 선반번호 입력)
          q  → 서버 종료
        """
        print("\n" + "="*60)
        print("🧪 테스트 콘솔 시작")
        print("  s : 선반 도착 신호 전송")
        print("  q : 종료")
        print("="*60)
        
        while True:
            try:
                cmd = input("\n명령어 입력 > ").strip().lower()
                
                if cmd == 'q':
                    print("👋 종료")
                    break
                
                elif cmd == 's':
                    # 현재 주문 중인 사용자 목록 표시
                    if not self.current_orders:
                        print("⚠️  현재 주문 중인 사용자가 없습니다.")
                        print("   → GUI에서 작업시작 버튼을 먼저 누르세요.")
                        continue
                    
                    print("\n현재 주문 중인 사용자:")
                    for uid, order_num in self.current_orders.items():
                        shelves = self.user_shelf_groups.get(uid, [])
                        print(f"  사용자{uid} - 주문{order_num}번 | 선반 목록: {shelves}")
                    
                    # 사용자 선택
                    uid_input = input("사용자ID 입력 (1 or 2): ").strip()
                    try:
                        user_id = int(uid_input)
                    except ValueError:
                        print("❌ 숫자를 입력하세요.")
                        continue
                    
                    if user_id not in self.current_orders:
                        print(f"❌ 사용자{user_id}는 현재 주문 중이 아닙니다.")
                        continue
                    
                    # 선반 선택
                    shelves = self.user_shelf_groups.get(user_id, [])
                    if not shelves:
                        print("⚠️  선반 목록이 없습니다.")
                        continue
                    
                    print(f"\n사용자{user_id} 선반 목록:")
                    for i, shelf in enumerate(shelves):
                        print(f"  {i+1}. {shelf}")
                    
                    shelf_input = input("선반 번호 선택 (번호 입력): ").strip()
                    try:
                        shelf_idx = int(shelf_input) - 1
                        if shelf_idx < 0 or shelf_idx >= len(shelves):
                            print("❌ 올바른 번호를 입력하세요.")
                            continue
                        shelf_group = shelves[shelf_idx]
                    except ValueError:
                        print("❌ 숫자를 입력하세요.")
                        continue
                    
                    # 전송
                    self.send_shelf_arrived(user_id, shelf_group)
                
                else:
                    print("❌ 알 수 없는 명령어입니다. (s: 선반 도착, q: 종료)")
            
            except (EOFError, KeyboardInterrupt):
                break
    
    def get_total_orders(self, user_id):
        """사용자 주문 파일의 총 주문 수 반환"""
        order_file = f'사용자{user_id}주문.xlsx'
        try:
            wb = load_workbook(order_file)
            ws = wb.active
            order_numbers = set()
            for row in ws.iter_rows(min_row=3, values_only=True):
                order_num = row[0]
                if order_num is not None:
                    order_numbers.add(order_num)
            return len(order_numbers)
        except Exception as e:
            print(f"❌ 총 주문 수 조회 실패: {e}")
            return 0

    def generate_picking_list(self, user_id, order_number):
        """주문번호에 해당하는 피킹 리스트 생성"""
        order_file = f'사용자{user_id}주문.xlsx'
        
        try:
            wb = load_workbook(order_file)
            ws = wb.active
            picking_list = []
            
            for row in ws.iter_rows(min_row=3, values_only=True):
                order_num = row[0]
                item = row[1]
                quantity = row[2]
                
                if order_num == order_number and item and quantity:
                    shelf_number = self.get_shelf_by_item(item)
                    if shelf_number:
                        picking_list.append({
                            "선반번호": shelf_number,
                            "물건": item,
                            "개수": quantity
                        })
                    else:
                        print(f"⚠️  물건을 찾을 수 없음: {item}")
            
            picking_list = self.optimize_picking_route(picking_list)
            return picking_list
            
        except FileNotFoundError:
            print(f"❌ 주문 파일 없음: {order_file}")
            return []
        except Exception as e:
            print(f"❌ 주문 로드 오류: {e}")
            return []
    
    def get_shelf_by_item(self, item):
        """물건명으로 선반번호 조회"""
        with self.db_lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT shelf_number FROM inventory WHERE item = ?", (item,))
            result = cursor.fetchone()
            conn.close()
            return result[0] if result else None
    
    def optimize_picking_route(self, picking_list):
        """피킹 경로 최적화 (구역 → 열 → 층 순)"""
        def parse_shelf(shelf_str):
            parts = shelf_str.split('-')
            return (int(parts[0]), int(parts[1]), int(parts[2]))
        picking_list.sort(key=lambda x: parse_shelf(x['선반번호']))
        return picking_list
    
    def deduct_inventory(self, user_id, order_number):
        """주문 시작 시 재고 차감 (트랜잭션)"""
        order_file = f'사용자{user_id}주문.xlsx'
        
        with self.db_lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            try:
                cursor.execute("BEGIN TRANSACTION")
                wb = load_workbook(order_file)
                ws = wb.active
                
                for row in ws.iter_rows(min_row=3, values_only=True):
                    order_num = row[0]
                    item = row[1]
                    quantity = row[2]
                    
                    if order_num == order_number and item and quantity:
                        cursor.execute("SELECT stock FROM inventory WHERE item = ?", (item,))
                        result = cursor.fetchone()
                        
                        if not result:
                            raise Exception(f"물건을 찾을 수 없음: {item}")
                        
                        current_stock = result[0]
                        
                        if current_stock < quantity:
                            raise Exception(f"재고 부족: {item} (필요: {quantity}, 현재: {current_stock})")
                        
                        cursor.execute("""
                            UPDATE inventory SET stock = stock - ? WHERE item = ?
                        """, (quantity, item))
                        
                        new_stock = current_stock - quantity
                        print(f"   ✓ {item}: {quantity}개 차감 → 남은 재고 {new_stock}")
                
                conn.commit()
                conn.close()
                return True
                
            except Exception as e:
                cursor.execute("ROLLBACK")
                conn.close()
                print(f"❌ 재고 차감 실패: {e}")
                return False
    
    def get_inventory_status(self):
        """현재 재고 상태 조회"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT zone, COUNT(*), SUM(stock) 
            FROM inventory GROUP BY zone ORDER BY zone
        """)
        zone_stats = []
        for zone, items, total_stock in cursor.fetchall():
            zone_stats.append({'zone': zone, 'items': items, 'total_stock': total_stock})
        
        cursor.execute("SELECT item, stock FROM inventory WHERE stock <= 5 ORDER BY stock")
        low_stock = [{'item': item, 'stock': stock} for item, stock in cursor.fetchall()]
        
        conn.close()
        return {'zone_stats': zone_stats, 'low_stock': low_stock}
    
    def show_inventory_status(self):
        """현재 재고 상태 출력"""
        status = self.get_inventory_status()
        print("\n" + "="*60)
        print("📦 현재 재고 상태")
        print("="*60)
        for stat in status['zone_stats']:
            print(f"구역 {stat['zone']}: {stat['items']}개 품목, 총 {stat['total_stock']}개")
        if status['low_stock']:
            print(f"\n⚠️  재고 부족 경고:")
            for item_info in status['low_stock']:
                print(f"   {item_info['item']}: {item_info['stock']}개")
    
    def run_flask(self):
        """Flask HTTP 서버 실행 (별도 스레드)"""
        print(f"✅ HTTP API 서버 시작: http://0.0.0.0:{self.http_port}")
        self.app.run(host='0.0.0.0', port=self.http_port, threaded=True, use_reloader=False)
    
    def run(self):
        """서버 실행"""
        print("="*60)
        print("🏭 창고 관리 서버 시작")
        print("="*60)
        
        self.show_inventory_status()
        
        mqtt_ok = self.init_mqtt()
        if not mqtt_ok:
            print("⚠️  MQTT 없이 계속 실행 (HTTP API만 사용)")
        
        # Flask 서버 (별도 스레드)
        flask_thread = threading.Thread(target=self.run_flask, daemon=True)
        flask_thread.start()
        
        print("\n✅ 서버 준비 완료")
        print(f"   - HTTP API: http://localhost:{self.http_port}")
        print(f"   - MQTT: {self.mqtt_broker}:{self.mqtt_port}")
        
        # [테스트용] 콘솔 입력 루프 (메인 스레드에서 실행)
        self.run_test_console()
        
        # 종료
        if self.mqtt_client:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()


def main():
    server = WarehouseServer(
        db_path='warehouse.db',
        mqtt_broker='localhost',
        mqtt_port=1883,
        http_port=5000
    )
    server.run()


if __name__ == '__main__':
    main()

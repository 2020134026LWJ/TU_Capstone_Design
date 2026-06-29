import paho.mqtt.client as mqtt
import psycopg2
import json
import logging
from datetime import datetime

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/mqtt_server.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# DB 연결 설정
DB_CONFIG = {
    'host': 'localhost',
    'database': 'warehouse',
    'user': 'agv_user',
    'password': 'agv1234'
}

# MQTT 설정
MQTT_BROKER = "localhost"  # 로컬 테스트용 (나중에 192.168.0.100으로 변경)
MQTT_PORT = 1883
MQTT_TOPIC = "agv/algorithm"


class WarehouseServer:
    def __init__(self):
        self.mqtt_client = None
        self.db_conn = None
       
    def connect_db(self):
        """PostgreSQL 연결"""
        try:
            self.db_conn = psycopg2.connect(**DB_CONFIG)
            logger.info("DB 연결 성공")
        except Exception as e:
            logger.error(f"DB 연결 실패: {e}")
            raise
   
    def on_connect(self, client, userdata, flags, rc):
        """MQTT 연결 콜백"""
        if rc == 0:
            logger.info("MQTT Broker 연결 성공")
            client.subscribe(MQTT_TOPIC)
            logger.info(f"구독: {MQTT_TOPIC}")
        else:
            logger.error(f"MQTT 연결 실패: {rc}")
   
    def on_message(self, client, userdata, message):
        """MQTT 메시지 수신 콜백"""
        try:
            payload = message.payload.decode('utf-8')
            data = json.loads(payload)
           
            msg_type = data.get('type')
            logger.info(f"메시지 수신: {msg_type}")
            logger.info(f"   내용: {data}")
           
            if msg_type == 'start_order':
                self.handle_start_order(data)
            elif msg_type == 'shelf_complete':
                self.handle_shelf_complete(data)
            elif msg_type == 'order_complete':
                self.handle_order_complete(data)
            else:
                logger.warning(f"알 수 없는 메시지 타입: {msg_type}")
               
        except Exception as e:
            logger.error(f"메시지 처리 오류: {e}")
   
    def handle_start_order(self, data):
        """주문 시작 처리 - 재고 차감"""
        user_id = data.get('사용자ID')
        order_num = data.get('주문번호')
       
        logger.info(f"주문 시작: 사용자{user_id} - 주문{order_num}")
       
        try:
            cursor = self.db_conn.cursor()
           
            # 1. 주문 내역 조회
            table_name = f"user{user_id}_orders"
            cursor.execute(f"""
                SELECT 물건, 개수
                FROM {table_name}
                WHERE 주문번호 = %s
            """, (order_num,))
           
            orders = cursor.fetchall()
            logger.info(f"   주문 물건: {len(orders)}개")
           
            # 2. 재고 차감
            for item_name, quantity in orders:
                cursor.execute("""
                    UPDATE inventory
                    SET 재고 = 재고 - %s
                    WHERE 물건 = %s
                """, (quantity, item_name))
               
                logger.info(f"   {item_name} 재고 차감: -{quantity}")
           
            # 3. 커밋
            self.db_conn.commit()
           
            # 4. 주문 히스토리 기록
            cursor.execute("""
                INSERT INTO order_history (사용자ID, 주문번호, 상태, 시작시간)
                VALUES (%s, %s, %s, %s)
            """, (user_id, order_num, '진행중', datetime.now()))
           
            self.db_conn.commit()
            cursor.close()
           
            logger.info(f"재고 차감 완료: 사용자{user_id} - 주문{order_num}")
           
            # 5. 알고리즘 호출 (여기서는 로그만)
            logger.info(f"알고리즘 호출: 사용자{user_id} - 주문{order_num}")
           
        except Exception as e:
            logger.error(f"재고 차감 실패: {e}")
            self.db_conn.rollback()
   
    def handle_shelf_complete(self, data):
        """선반 완료 처리"""
        user_id = data.get('사용자ID')
        shelf_num = data.get('선반번호')
       
        logger.info(f"선반 완료: 사용자{user_id} - 선반{shelf_num}")
       
        # 알고리즘에 전달 (여기서는 로그만)
        logger.info(f"알고리즘에 전달: 다음 선반 요청")
   
    def handle_order_complete(self, data):
        """주문 완료 처리"""
        user_id = data.get('사용자ID')
        order_num = data.get('주문번호')
       
        logger.info(f"주문 완료: 사용자{user_id} - 주문{order_num}")
       
        try:
            cursor = self.db_conn.cursor()
           
            # 주문 히스토리 업데이트
            cursor.execute("""
                UPDATE order_history
                SET 상태 = %s, 완료시간 = %s
                WHERE 사용자ID = %s AND 주문번호 = %s AND 상태 = %s
            """, ('완료', datetime.now(), user_id, order_num, '진행중'))
           
            self.db_conn.commit()
            cursor.close()
           
            logger.info(f"주문 완료 기록: 사용자{user_id} - 주문{order_num}")
           
        except Exception as e:
            logger.error(f"주문 완료 처리 실패: {e}")
   
    def start(self):
        """서버 시작"""
        # DB 연결
        self.connect_db()
       
        # MQTT 클라이언트 설정
        self.mqtt_client = mqtt.Client()
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
       
        # MQTT 브로커 연결
        logger.info(f"MQTT Broker 연결 시도: {MQTT_BROKER}:{MQTT_PORT}")
        self.mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
       
        # 메시지 루프 시작
        logger.info("중앙서버 시작!")
        logger.info("=" * 50)
        self.mqtt_client.loop_forever()


if __name__ == "__main__":
    server = WarehouseServer()
    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("\n서버 종료")
    except Exception as e:
        logger.error(f"서버 오류: {e}")
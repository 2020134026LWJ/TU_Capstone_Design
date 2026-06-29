#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
창고 관리자 GUI - KivyMD 버전
- 실시간 재고 현황 (구역별 그리드)
- 재고 부족 경고
- MQTT 로그 모니터링
"""
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDRaisedButton
from kivymd.uix.card import MDCard
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.core.text import LabelBase
from kivy.clock import Clock
import os
import json
import sqlite3
from datetime import datetime

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("⚠️  paho-mqtt 라이브러리가 필요합니다: pip install paho-mqtt")
    mqtt = None

# 설정
SERVER_IP = 'localhost'
HTTP_PORT = 5000
MQTT_PORT = 1883

Window.size = (1200, 700)

# 한글 폰트
FONT_NAME = 'NanumGothic'
FONT_PATHS = [
    'C:/Windows/Fonts/malgun.ttf',
    '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/System/Library/Fonts/AppleGothic.ttf',
]
font_path = None
for path in FONT_PATHS:
    if os.path.exists(path):
        font_path = path
        break

if font_path:
    LabelBase.register(name=FONT_NAME, fn_regular=font_path)
    print(f"✅ 폰트 로드: {font_path}")
else:
    print("경고: 한글 폰트를 찾을 수 없습니다. 기본 폰트 사용.")
    FONT_NAME = 'Roboto'

# DB 경로 (절대경로)
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'warehouse.db')

# 색상
COLOR_BG          = (0.13, 0.13, 0.13, 1)
COLOR_ZONE_TITLE  = (0, 0.39, 0, 1)
COLOR_TIER_LABEL  = (0.40, 0.60, 0.40, 1)
COLOR_CELL_EMPTY  = (0.88, 0.88, 0.88, 1)
COLOR_STOCK_OK    = (0.85, 0.92, 0.85, 1)
COLOR_STOCK_WARN  = (1, 0.85, 0.2, 1)
COLOR_STOCK_DANGER= (1, 0.3, 0.3, 1)


def get_stock_color(stock):
    if stock <= 2:
        return COLOR_STOCK_DANGER
    elif stock <= 5:
        return COLOR_STOCK_WARN
    else:
        return COLOR_STOCK_OK


class ShelfCell(MDCard):
    """선반 한 칸 (선반번호 + 물건 + 재고)"""
    def __init__(self, shelf_number, item, stock, **kwargs):
        super().__init__(**kwargs)
        self.orientation = 'vertical'
        self.padding = 3
        self.spacing = 1
        self.radius = [4]
        self.elevation = 1
        self.md_bg_color = get_stock_color(stock)

        self.add_widget(Label(
            text=shelf_number,
            font_size='20sp',
            font_name=FONT_NAME,
            bold=True,
            color=(0.1, 0.1, 0.1, 1),
            size_hint_y=0.3,
            halign='center',
            valign='middle',
        ))
        item_lbl = Label(
            text=item,
            font_size='19sp',
            font_name=FONT_NAME,
            color=(0.1, 0.1, 0.1, 1),
            size_hint_y=0.4,
            halign='center',
            valign='middle',
        )
        item_lbl.bind(size=lambda inst, v: setattr(inst, 'text_size', v))
        self.add_widget(item_lbl)
        self.add_widget(Label(
            text=f'{stock}개',
            font_size='22sp',
            font_name=FONT_NAME,
            bold=True,
            color=(0.1, 0.1, 0.1, 1),
            size_hint_y=0.3,
            halign='center',
            valign='middle',
        ))


class AdminGUI(MDBoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.orientation = 'vertical'
        self.padding = 10
        self.spacing = 8
        self.md_bg_color = COLOR_BG

        self.log_messages = []
        self.mqtt_client = None
        self.pending_start_logs = {}  # {user_id: log_text}
        self.init_mqtt()

        self.build_header()
        self.build_inventory_grid()
        self.build_log()
        self._seed_sample_logs()   # 데모용: 로그창을 미리 채워 둠

        Clock.schedule_interval(self.refresh_data, 5)
        self.refresh_data(0)

    def init_mqtt(self):
        if mqtt is None:
            return
        try:
            self.mqtt_client = mqtt.Client(client_id="admin_gui")
            self.mqtt_client.on_message = self.on_mqtt_message
            self.mqtt_client.connect(SERVER_IP, MQTT_PORT, 60)
            self.mqtt_client.subscribe("warehouse/#")
            self.mqtt_client.loop_start()
            print(f"✅ MQTT 연결: {SERVER_IP}:{MQTT_PORT}")
        except Exception as e:
            print(f"❌ MQTT 연결 실패: {e}")

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode('utf-8'))
            msg_type = payload.get('type', 'unknown')
            timestamp = datetime.now().strftime('%H:%M:%S')

            user_id = payload.get('사용자ID')

            if msg_type == 'start_order':
                # 실제 작업이 시작됐는지 확인 후 로그 → 임시 저장
                self.pending_start_logs[user_id] = (
                    f"[{timestamp}] 사용자{user_id} 주문{payload['주문번호']} 시작"
                )
                return

            # 이 메시지로 출력할 로그를 '발생 순서대로' 모음
            lines = []

            if msg_type == 'shelf_arrived':
                # 선반 도착 = 주문 실제 시작 확인 → 보류 중인 start 로그 먼저
                if user_id in self.pending_start_logs:
                    lines.append(self.pending_start_logs.pop(user_id))
                lines.append(f"[{timestamp}] 사용자{user_id} 선반{payload['선반번호']} 도착")
            elif msg_type == 'shelf_complete':
                if user_id in self.pending_start_logs:
                    lines.append(self.pending_start_logs.pop(user_id))
                items = payload.get('품목목록', [])
                if items:
                    item_str = ', '.join(f"{i['물건']} {i['개수']}개 차감" for i in items)
                    lines.append(f"({item_str})")
                lines.append(f"[{timestamp}] 사용자{user_id} 선반{payload['선반번호']} 완료")
            elif msg_type == 'order_complete':
                lines.append(f"[{timestamp}] 사용자{user_id} 주문{payload['주문번호']} 완료")
            else:
                return

            # 모든 UI 갱신은 메인 스레드에서 순서대로 처리 (도착/시작 역전 방지)
            Clock.schedule_once(lambda dt, ls=lines: [self.add_log(t) for t in ls], 0)

            if msg_type in ['shelf_complete', 'order_complete']:
                Clock.schedule_once(lambda dt: self.refresh_data(0), 1)
        except Exception:
            pass

    def build_header(self):
        header = MDBoxLayout(orientation='horizontal', size_hint_y=None, height=55, spacing=10)

        header.add_widget(Label(
            text='창고 재고 관리 시스템',
            font_size='30sp',
            font_name=FONT_NAME,
            bold=True,
            size_hint_x=0.4,
            color=(1, 1, 1, 1),
        ))

        # 재고 초기화 버튼 (전 품목 50개로)
        reset_btn = MDRaisedButton(
            text='재고 초기화',
            font_size='18sp',
            font_name=FONT_NAME,
            md_bg_color=(0.9, 0.3, 0.3, 1),
            size_hint_x=0.15,
        )
        reset_btn.bind(on_press=self.reset_stock)
        header.add_widget(reset_btn)

        # 진행(작업) 초기화 버튼 (완료 기록/재개 지점 삭제)
        progress_btn = MDRaisedButton(
            text='작업 초기화',
            font_size='18sp',
            font_name=FONT_NAME,
            md_bg_color=(0.9, 0.6, 0.2, 1),
            size_hint_x=0.15,
        )
        progress_btn.bind(on_press=self.reset_progress)
        header.add_widget(progress_btn)

        self.time_label = Label(
            text='',
            font_size='16sp',
            font_name=FONT_NAME,
            size_hint_x=0.15,
            color=(0.8, 0.8, 0.8, 1),
            halign='center',
            valign='middle',
        )
        header.add_widget(self.time_label)

        refresh_btn = MDRaisedButton(
            text='새로고침',
            font_size='18sp',
            font_name=FONT_NAME,
            md_bg_color=(0.3, 0.6, 1, 1),
            size_hint_x=0.15,
        )
        refresh_btn.bind(on_press=self.on_refresh_btn)
        header.add_widget(refresh_btn)

        self.add_widget(header)

    def build_inventory_grid(self):
        self.main_content = MDBoxLayout(orientation='horizontal', spacing=8)
        self.inventory_container = MDBoxLayout(orientation='vertical', size_hint_x=0.65, spacing=8)
        self.main_content.add_widget(self.inventory_container)
        self.add_widget(self.main_content)

    def _build_zone_grid(self, zone, inventory_dict):
        zone_box = MDBoxLayout(orientation='vertical', spacing=3)

        title_card = MDCard(
            md_bg_color=COLOR_ZONE_TITLE,
            size_hint_y=None,
            height=32,
            radius=[4],
            elevation=0,
        )
        title_card.add_widget(Label(
            text=f'  구역 {zone}',
            font_size='22sp',
            font_name=FONT_NAME,
            bold=True,
            color=(1, 1, 1, 1),
            halign='left',
            valign='middle',
        ))
        zone_box.add_widget(title_card)

        for tier in range(3, 0, -1):
            row_box = MDBoxLayout(orientation='horizontal', spacing=3)

            tier_card = MDCard(
                md_bg_color=COLOR_TIER_LABEL,
                size_hint_x=0.13,
                radius=[4],
                elevation=0,
            )
            tier_card.add_widget(Label(
                text=f'{tier}단',
                font_size='18sp',
                font_name=FONT_NAME,
                bold=True,
                color=(1, 1, 1, 1),
                halign='center',
                valign='middle',
            ))
            row_box.add_widget(tier_card)

            for row in range(1, 5):
                key = (zone, row, tier)
                if key in inventory_dict:
                    shelf_number, item, stock = inventory_dict[key]
                    cell = ShelfCell(shelf_number, item, stock)
                else:
                    cell = MDCard(
                        md_bg_color=COLOR_CELL_EMPTY,
                        radius=[4],
                        elevation=0,
                    )
                    cell.add_widget(Label(
                        text='-',
                        font_size='18sp',
                        font_name=FONT_NAME,
                        color=(0.6, 0.6, 0.6, 1),
                        halign='center',
                        valign='middle',
                    ))
                row_box.add_widget(cell)

            zone_box.add_widget(row_box)

        return zone_box

    def update_inventory_grid(self):
        self.inventory_container.clear_widgets()
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT shelf_number, item, stock, zone, row, tier
                FROM inventory
                ORDER BY zone, row, tier
            """)
            rows = cursor.fetchall()
            conn.close()
        except Exception as e:
            print(f"❌ 재고 로드 실패: {e}")
            return

        inventory_dict = {}
        for shelf_number, item, available, zone, row, tier in rows:
            if zone and row and tier:
                inventory_dict[(zone, row, tier)] = (shelf_number, item, available)

        zones_box = MDBoxLayout(orientation='vertical', spacing=10)
        for zone in [1, 2]:
            zones_box.add_widget(self._build_zone_grid(zone, inventory_dict))

        self.inventory_container.add_widget(zones_box)

    def build_log(self):
        log_box = MDBoxLayout(orientation='vertical', size_hint_x=0.35)

        log_box.add_widget(Label(
            text='실시간 로그',
            font_size='28sp',
            font_name=FONT_NAME,
            bold=True,
            size_hint_y=None,
            height=60,
            color=(1, 1, 1, 1),
            halign='center',
            valign='middle',
        ))

        scroll = ScrollView(bar_width=4, do_scroll_x=False)
        self.log_label = Label(
            text='',
            font_size='22sp',
            font_name=FONT_NAME,
            size_hint_y=None,
            halign='left',
            valign='top',
            padding=(8, 4),
            color=(0.9, 0.9, 0.9, 1),
        )
        self.log_label.bind(
            width=lambda *x: setattr(self.log_label, 'text_size', (self.log_label.width, None))
        )
        self.log_label.bind(texture_size=self.log_label.setter('size'))
        scroll.add_widget(self.log_label)
        log_box.add_widget(scroll)

        self.main_content.add_widget(log_box)

    def reset_stock(self, instance):
        """재고만 50으로 되돌림 (진행 상태는 건드리지 않음)"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("UPDATE inventory SET stock = 50, reserved = 0")
            conn.commit()
            conn.close()
            self.add_log(f"[{datetime.now().strftime('%H:%M:%S')}] 재고 초기화 완료 (전 품목 50개)")
            self.update_inventory_grid()
        except Exception as e:
            print(f"재고 초기화 실패: {e}")

    def reset_progress(self, instance):
        """피킹 진행 상태(완료 기록/재개 지점)만 삭제 (재고는 건드리지 않음)"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            try:
                cursor.execute("DELETE FROM picking_progress")
                cursor.execute("DELETE FROM user_state")
            except sqlite3.OperationalError:
                pass
            conn.commit()
            conn.close()
            self.add_log(f"[{datetime.now().strftime('%H:%M:%S')}] 진행 상태 초기화 완료 (완료 기록/재개 지점 삭제)")
        except Exception as e:
            print(f"진행 상태 초기화 실패: {e}")

    def on_refresh_btn(self, instance):
        self.log_messages = []
        self.log_label.text = ''
        self.add_log(f"[{datetime.now().strftime('%H:%M:%S')}] 로그 새로고침")
        self.refresh_data(0)

    def refresh_data(self, dt):
        try:
            self.time_label.text = datetime.now().strftime('%Y-%m-%d\n%H:%M:%S')
            self.update_inventory_grid()
        except Exception as e:
            print(f"❌ 데이터 로드 실패: {e}")

    def _seed_sample_logs(self):
        """데모용 샘플 로그를 미리 채워 로그창이 비어 보이지 않게 한다.
        (실제 작업 로그가 들어오면 오래된 항목부터 자연히 밀려난다)"""
        samples = [
            "[09:00:01] 사용자1 주문1 시작",
            "[09:00:05] 사용자1 선반1-1 도착",
            "(드롭스 2개 차감)",
            "[09:00:12] 사용자1 선반1-1 완료",
            "[09:00:16] 사용자1 선반2-3 도착",
            "(롤리팝 3개 차감)",
            "[09:00:23] 사용자1 선반2-3 완료",
            "[09:00:25] 사용자1 주문1 완료",
            "[09:00:31] 사용자2 주문1 시작",
            "[09:00:35] 사용자2 선반1-4 도착",
            "(마시멜로 4개 차감)",
            "[09:00:42] 사용자2 선반1-4 완료",
            "[09:00:47] 사용자1 주문2 시작",
            "[09:00:51] 사용자1 선반2-1 도착",
            "(캐러멜 5개 차감)",
            "[09:00:58] 사용자1 선반2-1 완료",
        ]
        for line in samples:
            self.add_log(line)

    def add_log(self, message):
        self.log_messages.append(message)
        if len(self.log_messages) > 26:
            self.log_messages = self.log_messages[-26:]
        self.log_label.text = '\n'.join(reversed(self.log_messages))


class AdminApp(MDApp):
    def build(self):
        self.title = '창고 관리자 - 재고 모니터링'
        self.theme_cls.primary_palette = 'Green'
        self.theme_cls.theme_style = 'Dark'
        return AdminGUI()

    def on_stop(self):
        gui = self.root
        if gui.mqtt_client:
            gui.mqtt_client.loop_stop()
            gui.mqtt_client.disconnect()


if __name__ == '__main__':
    AdminApp().run()

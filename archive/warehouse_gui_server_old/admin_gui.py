#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
창고 관리자 GUI - 재고 모니터링
- 실시간 재고 현황
- 구역별 재고 통계
- 재고 부족 경고
- MQTT 로그 모니터링
"""
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.core.text import LabelBase
from kivy.graphics import Color, Rectangle
from kivy.clock import Clock
import requests
import json
import os
from datetime import datetime

# MQTT 라이브러리
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("⚠️  paho-mqtt 라이브러리가 필요합니다: pip install paho-mqtt")
    mqtt = None

# 설정
SERVER_IP = 'localhost'  # 로컬 서버
HTTP_PORT = 5000
MQTT_PORT = 1883

# 윈도우 크기
Window.size = (1200, 700)

# 한글 폰트 등록
FONT_NAME = 'NanumGothic'

FONT_PATHS = [
    'C:/Windows/Fonts/malgun.ttf',  # Windows
    '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',  # Linux
    '/System/Library/Fonts/AppleGothic.ttf',  # Mac
]

font_path = None
for path in FONT_PATHS:
    if os.path.exists(path):
        font_path = path
        break

if font_path:
    LabelBase.register(name=FONT_NAME, fn_regular=font_path)
else:
    print("경고: 한글 폰트를 찾을 수 없습니다. 기본 폰트를 사용합니다.")
    FONT_NAME = 'Roboto'


class InventoryRow(BoxLayout):
    """재고 항목 행"""
    def __init__(self, shelf, item, stock, **kwargs):
        super(InventoryRow, self).__init__(**kwargs)
        self.orientation = 'horizontal'
        self.size_hint_y = None
        self.height = 40
        self.spacing = 5
        
        # 재고 수량에 따른 색상
        if stock <= 3:
            bg_color = (1, 0.3, 0.3, 1)  # 빨간색 (위험)
        elif stock <= 5:
            bg_color = (1, 0.8, 0, 1)  # 노란색 (경고)
        else:
            bg_color = (0.9, 0.9, 0.9, 1)  # 회색 (정상)
        
        # 선반번호
        shelf_label = Label(
            text=shelf,
            size_hint_x=0.2,
            color=(0, 0, 0, 1),
            font_size='14sp',
            font_name=FONT_NAME
        )
        self.add_widget(shelf_label)
        
        # 물건명
        item_label = Label(
            text=item,
            size_hint_x=0.5,
            color=(0, 0, 0, 1),
            font_size='14sp',
            font_name=FONT_NAME,
            halign='left',
            valign='middle'
        )
        item_label.bind(size=item_label.setter('text_size'))
        self.add_widget(item_label)
        
        # 재고
        stock_box = BoxLayout(size_hint_x=0.3)
        with stock_box.canvas.before:
            Color(*bg_color)
            stock_box.rect = Rectangle(size=stock_box.size, pos=stock_box.pos)
        stock_box.bind(size=lambda i, v: setattr(stock_box.rect, 'size', v),
                       pos=lambda i, v: setattr(stock_box.rect, 'pos', v))
        
        stock_label = Label(
            text=f'{stock}개',
            color=(0, 0, 0, 1) if stock > 5 else (1, 1, 1, 1),
            font_size='16sp',
            font_name=FONT_NAME,
            bold=True
        )
        stock_box.add_widget(stock_label)
        self.add_widget(stock_box)


class AdminGUI(BoxLayout):
    def __init__(self, **kwargs):
        super(AdminGUI, self).__init__(**kwargs)
        
        # 배경색
        with self.canvas.before:
            Color(0.95, 0.95, 0.95, 1)
            self.rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self.on_size_change, pos=self.on_size_change)
        
        self.orientation = 'vertical'
        self.padding = 10
        self.spacing = 10
        
        # MQTT 클라이언트
        self.mqtt_client = None
        self.init_mqtt()
        
        # UI 구성
        self.build_header()
        self.build_stats()
        self.build_inventory_list()
        self.build_log()
        
        # 자동 새로고침 (5초마다)
        Clock.schedule_interval(self.refresh_data, 5)
        
        # 초기 데이터 로드
        self.refresh_data(0)
    
    def on_size_change(self, instance, value):
        if hasattr(self, 'rect'):
            self.rect.size = self.size
            self.rect.pos = self.pos
    
    def init_mqtt(self):
        """MQTT 클라이언트 초기화"""
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
        """MQTT 메시지 수신"""
        try:
            payload = json.loads(msg.payload.decode())
            msg_type = payload.get('type', 'unknown')
            timestamp = datetime.now().strftime('%H:%M:%S')
            
            if msg_type == 'start_order':
                log_text = f"[{timestamp}] 🚀 사용자{payload['사용자ID']} 주문{payload['주문번호']} 시작"
            elif msg_type == 'shelf_complete':
                log_text = f"[{timestamp}] 📦 사용자{payload['사용자ID']} 선반{payload['선반번호']} 완료"
            elif msg_type == 'order_complete':
                log_text = f"[{timestamp}] 🎉 사용자{payload['사용자ID']} 주문{payload['주문번호']} 완료"
            else:
                log_text = f"[{timestamp}] {msg.topic}: {payload}"
            
            self.add_log(log_text)
            
            # 주문 시작/완료 시 재고 새로고침
            if msg_type in ['start_order', 'order_complete']:
                Clock.schedule_once(lambda dt: self.refresh_data(0), 1)
        except:
            pass
    
    def build_header(self):
        """헤더 섹션"""
        header = BoxLayout(orientation='horizontal', size_hint_y=0.1, spacing=10)
        
        # 타이틀
        title = Label(
            text='📊 창고 재고 관리 시스템',
            font_size='28sp',
            font_name=FONT_NAME,
            bold=True,
            size_hint_x=0.6,
            color=(0.2, 0.2, 0.2, 1)
        )
        header.add_widget(title)
        
        # 새로고침 버튼
        refresh_btn = Button(
            text='🔄 새로고침',
            font_size='18sp',
            font_name=FONT_NAME,
            size_hint_x=0.2,
            background_color=(0.3, 0.6, 1, 1),
            color=(1, 1, 1, 1)
        )
        refresh_btn.bind(on_press=self.refresh_data)
        header.add_widget(refresh_btn)
        
        # 시간 표시
        self.time_label = Label(
            text='',
            font_size='16sp',
            font_name=FONT_NAME,
            size_hint_x=0.2,
            color=(0.2, 0.2, 0.2, 1)
        )
        header.add_widget(self.time_label)
        
        self.add_widget(header)
    
    def build_stats(self):
        """통계 섹션"""
        stats_box = BoxLayout(orientation='horizontal', size_hint_y=0.15, spacing=10)
        
        # 구역별 통계 카드
        self.zone_labels = []
        for i in range(3):
            card = BoxLayout(orientation='vertical', padding=10)
            with card.canvas.before:
                Color(1, 1, 1, 1)
                card.rect = Rectangle(size=card.size, pos=card.pos)
            card.bind(size=lambda i, v, c=card: setattr(c.rect, 'size', v),
                     pos=lambda i, v, c=card: setattr(c.rect, 'pos', v))
            
            zone_title = Label(
                text=f'구역 {i+1}',
                font_size='20sp',
                font_name=FONT_NAME,
                bold=True,
                size_hint_y=0.3,
                color=(0, 0, 0, 1)
            )
            card.add_widget(zone_title)
            
            zone_stat = Label(
                text='0개 품목\n총 0개',
                font_size='18sp',
                font_name=FONT_NAME,
                size_hint_y=0.7,
                color=(0.2, 0.2, 0.2, 1)
            )
            self.zone_labels.append(zone_stat)
            card.add_widget(zone_stat)
            
            stats_box.add_widget(card)
        
        # 재고 부족 경고
        warning_card = BoxLayout(orientation='vertical', padding=10)
        with warning_card.canvas.before:
            Color(1, 0.95, 0.8, 1)
            warning_card.rect = Rectangle(size=warning_card.size, pos=warning_card.pos)
        warning_card.bind(size=lambda i, v: setattr(warning_card.rect, 'size', v),
                         pos=lambda i, v: setattr(warning_card.rect, 'pos', v))
        
        warning_title = Label(
            text='⚠️ 재고 부족',
            font_size='20sp',
            font_name=FONT_NAME,
            bold=True,
            size_hint_y=0.3,
            color=(0.8, 0.3, 0, 1)
        )
        warning_card.add_widget(warning_title)
        
        self.warning_label = Label(
            text='없음',
            font_size='16sp',
            font_name=FONT_NAME,
            size_hint_y=0.7,
            color=(0.6, 0.2, 0, 1)
        )
        warning_card.add_widget(self.warning_label)
        
        stats_box.add_widget(warning_card)
        
        self.add_widget(stats_box)
    
    def build_inventory_list(self):
        """재고 목록"""
        inventory_box = BoxLayout(orientation='vertical', size_hint_y=0.5)
        
        # 헤더
        list_header = BoxLayout(orientation='horizontal', size_hint_y=None, height=40, spacing=5)
        list_header.add_widget(Label(text='선반번호', size_hint_x=0.2, font_size='16sp', font_name=FONT_NAME, bold=True, color=(0,0,0,1)))
        list_header.add_widget(Label(text='물건', size_hint_x=0.5, font_size='16sp', font_name=FONT_NAME, bold=True, color=(0,0,0,1)))
        list_header.add_widget(Label(text='재고', size_hint_x=0.3, font_size='16sp', font_name=FONT_NAME, bold=True, color=(0,0,0,1)))
        inventory_box.add_widget(list_header)
        
        # 스크롤 가능한 목록
        scroll = ScrollView()
        self.inventory_grid = GridLayout(cols=1, spacing=2, size_hint_y=None)
        self.inventory_grid.bind(minimum_height=self.inventory_grid.setter('height'))
        scroll.add_widget(self.inventory_grid)
        inventory_box.add_widget(scroll)
        
        self.add_widget(inventory_box)
    
    def build_log(self):
        """로그 섹션"""
        log_box = BoxLayout(orientation='vertical', size_hint_y=0.25)
        
        log_header = Label(
            text='📋 실시간 로그',
            font_size='18sp',
            font_name=FONT_NAME,
            bold=True,
            size_hint_y=0.15,
            color=(0.2, 0.2, 0.2, 1)
        )
        log_box.add_widget(log_header)
        
        # 스크롤 가능한 로그
        scroll = ScrollView()
        self.log_label = Label(
            text='',
            font_size='14sp',
            font_name=FONT_NAME,
            size_hint_y=None,
            halign='left',
            valign='top',
            color=(0, 0, 0, 1)
        )
        self.log_label.bind(texture_size=self.log_label.setter('size'))
        scroll.add_widget(self.log_label)
        log_box.add_widget(scroll)
        
        self.add_widget(log_box)
        
        self.log_messages = []
    
    def refresh_data(self, dt):
        """데이터 새로고침"""
        try:
            # 시간 업데이트
            self.time_label.text = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # API 호출
            response = requests.get(f'http://{SERVER_IP}:{HTTP_PORT}/api/inventory/status', timeout=2)
            data = response.json()
            
            # 구역별 통계 업데이트
            for i, stat in enumerate(data['zone_stats']):
                self.zone_labels[i].text = f"{stat['items']}개 품목\n총 {stat['total_stock']}개"
            
            # 재고 부족 경고
            if data['low_stock']:
                warning_text = '\n'.join([f"{item['item']}: {item['stock']}개" 
                                         for item in data['low_stock'][:5]])
                self.warning_label.text = warning_text
            else:
                self.warning_label.text = '없음'
            
            # 전체 재고 목록
            self.update_inventory_list()
            
        except Exception as e:
            print(f"❌ 데이터 로드 실패: {e}")
    
    def update_inventory_list(self):
        """재고 목록 업데이트"""
        try:
            # 기존 목록 제거
            self.inventory_grid.clear_widgets()
            
            # SQLite에서 전체 재고 가져오기
            import sqlite3
            conn = sqlite3.connect('warehouse.db')
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT shelf_number, item, stock 
                FROM inventory 
                ORDER BY zone, row, tier
            """)
            
            for shelf, item, stock in cursor.fetchall():
                row = InventoryRow(shelf, item, stock)
                self.inventory_grid.add_widget(row)
            
            conn.close()
            
        except Exception as e:
            print(f"❌ 재고 목록 로드 실패: {e}")
    
    def add_log(self, message):
        """로그 추가"""
        self.log_messages.append(message)
        
        # 최근 20개만 유지
        if len(self.log_messages) > 20:
            self.log_messages = self.log_messages[-20:]
        
        self.log_label.text = '\n'.join(reversed(self.log_messages))


class AdminApp(App):
    def build(self):
        self.title = '창고 관리자 - 재고 모니터링'
        return AdminGUI()
    
    def on_stop(self):
        """앱 종료 시 MQTT 연결 해제"""
        gui = self.root
        if gui.mqtt_client:
            gui.mqtt_client.loop_stop()
            gui.mqtt_client.disconnect()


if __name__ == '__main__':
    AdminApp().run()
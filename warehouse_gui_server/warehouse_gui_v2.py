#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
창고 피킹 GUI - v2 (라즈베리파이)
- MQTT 통신: 주문 시작/선반 완료/주문 완료
- HTTP API: 피킹 리스트 받기
- [추가] shelf_arrived 수신 시 해당 선반 셀만 활성화
"""
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.core.window import Window
from kivy.core.text import LabelBase
from kivy.graphics import Color, Rectangle
from kivy.clock import Clock
import os
import json
import requests

# MQTT 라이브러리
try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("⚠️  paho-mqtt 라이브러리가 필요합니다: pip install paho-mqtt")
    mqtt = None

# 설정
SERVER_IP = '172.30.1.72'
MQTT_PORT = 1883
HTTP_PORT = 5000

# 윈도우 크기
Window.size = (800, 450)
Window.borderless = True
Window.fullscreen = False
Window.top = 40

# 한글 폰트 등록
FONT_NAME = 'NanumGothic'
FONT_PATHS = [
    'C:/Windows/Fonts/malgun.ttf',
    '/usr/share/fonts/truetype/nanum/NanumGothic.ttf',
    '/System/Library/Fonts/AppleGothic.ttf',
]

font_path = None
for path in FONT_PATHS:
    if os.path.exists(path):
        font_path = path
        break

if font_path:
    LabelBase.register(name=FONT_NAME, fn_regular=font_path)
else:
    print("경고: 한글 폰트를 찾을 수 없습니다.")
    FONT_NAME = 'Roboto'

# 셀 상태별 색상
COLOR_DISABLED  = (0.5, 0.5, 0.5, 1)   # 회색 - 비활성 (선반 미도착)
COLOR_ACTIVE    = (0.93, 0.93, 0.93, 1) # 밝은 회색 - 활성 (터치 가능)
COLOR_COMPLETED = (1, 0.2, 0.2, 0.85)  # 빨간색 - 완료
COLOR_EMPTY     = (0.4, 0.4, 0.4, 1)   # 어두운 회색 - 빈 셀


class WorkCell(Button):
    """작업 셀 (2x4 그리드의 각 칸)"""
    
    def __init__(self, on_complete_callback=None, **kwargs):
        super(WorkCell, self).__init__(**kwargs)
        self.shelf_number = ''
        self.item = ''
        self.quantity = 0
        self.on_complete_callback = on_complete_callback
        
        self.color = (0, 0, 0, 1)
        self.font_size = '16sp'
        self.font_name = FONT_NAME
        
        # 초기 상태: 비어있음
        self.is_completed = False
        self.is_active = False   # 선반 도착으로 활성화 여부
        self.is_empty = True     # 피킹 항목 없음
        
        self._apply_color()
    
    def set_item(self, shelf_number, item, quantity):
        """피킹 항목 설정 - 기본적으로 비활성(대기) 상태"""
        self.shelf_number = shelf_number
        self.item = item
        self.quantity = quantity
        self.is_completed = False
        self.is_active = False
        self.is_empty = False
        self.text = f"{shelf_number}\n{item}\n({quantity}개)"
        self._apply_color()
    
    def clear(self):
        """셀 초기화"""
        self.shelf_number = ''
        self.item = ''
        self.quantity = 0
        self.text = ''
        self.is_completed = False
        self.is_active = False
        self.is_empty = True
        self._apply_color()
    
    def activate(self):
        """선반 도착 → 셀 활성화"""
        if self.is_empty or self.is_completed:
            return
        self.is_active = True
        self._apply_color()
    
    def _apply_color(self):
        """상태에 따라 배경색 적용"""
        if self.is_empty:
            self.background_color = COLOR_EMPTY
            self.color = (0.6, 0.6, 0.6, 1)
        elif self.is_completed:
            self.background_color = COLOR_COMPLETED
            self.color = (1, 1, 1, 1)
        elif self.is_active:
            self.background_color = COLOR_ACTIVE
            self.color = (0, 0, 0, 1)
        else:
            # 비활성 (선반 미도착)
            self.background_color = COLOR_DISABLED
            self.color = (0.85, 0.85, 0.85, 1)
    
    def on_press(self):
        """셀 터치"""
        if self.is_empty:
            return
        
        if not self.is_active:
            # 선반이 아직 도착하지 않음
            print(f"⚠️  선반 {self.shelf_number} 아직 미도착")
            return
        
        if not self.is_completed:
            self.is_completed = True
            self._apply_color()
            
            if self.on_complete_callback:
                self.on_complete_callback(self.shelf_number)
        else:
            # 다시 터치하면 완료 취소
            self.is_completed = False
            self._apply_color()


class WarehouseGUI(BoxLayout):
    def __init__(self, **kwargs):
        super(WarehouseGUI, self).__init__(**kwargs)
        
        with self.canvas.before:
            Color(0.5, 0.2, 0.05, 1)
            self.rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self.on_size_change, pos=self.on_size_change)
        
        self.orientation = 'vertical'
        self.padding = 10
        self.spacing = 10
        
        # 상태 변수
        self.selected_user_id = None
        self.current_order_number = 1
        self.total_orders = 0        # 총 주문 수 (서버에서 받아옴)
        self.work_cells = []
        self.picking_list = []
        
        # MQTT 클라이언트
        self.mqtt_client = None
        self.init_mqtt()
        
        # UI 구성
        self.build_top_section()
        self.build_work_grid()
        self.build_bottom_section()
    
    def on_size_change(self, instance, value):
        if hasattr(self, 'rect'):
            self.rect.size = self.size
            self.rect.pos = self.pos
    
    def init_mqtt(self):
        """MQTT 클라이언트 초기화"""
        if mqtt is None:
            print("⚠️  MQTT 없이 실행 (테스트 모드)")
            return
        
        try:
            import random
            client_id = f"raspberrypi_gui_{random.randint(1000, 9999)}"
            self.mqtt_client = mqtt.Client(client_id=client_id)
            self.mqtt_client.on_message = self.on_mqtt_message
            self.mqtt_client.connect(SERVER_IP, MQTT_PORT, 60)
            
            # shelf_arrived 토픽 구독 추가
            self.mqtt_client.subscribe("warehouse/shelf/arrived")
            
            self.mqtt_client.loop_start()
            print(f"✅ MQTT 연결: {SERVER_IP}:{MQTT_PORT} (ID: {client_id})")
            print(f"📡 구독: warehouse/shelf/arrived")
        except Exception as e:
            print(f"❌ MQTT 연결 실패: {e}")
            self.mqtt_client = None
    
    def on_mqtt_message(self, client, userdata, msg):
        """MQTT 메시지 수신"""
        try:
            payload = json.loads(msg.payload.decode())
            topic = msg.topic
            
            print(f"\n📩 MQTT 수신: {topic}")
            print(f"   데이터: {payload}")
            
            if topic == "warehouse/shelf/arrived":
                # 자기 사용자ID 메시지만 처리
                if payload.get('사용자ID') == self.selected_user_id:
                    shelf_group = payload.get('선반번호')
                    # Kivy UI 업데이트는 메인 스레드에서
                    Clock.schedule_once(
                        lambda dt: self.activate_shelf_cells(shelf_group), 0
                    )
        except Exception as e:
            print(f"❌ MQTT 메시지 처리 오류: {e}")
    
    def activate_shelf_cells(self, shelf_group):
        """
        도착한 선반(구역-열)에 해당하는 셀만 활성화
        예: shelf_group = "1-1" → 선반번호가 "1-1-*"인 셀 활성화
        """
        activated = 0
        for cell in self.work_cells:
            if cell.is_empty:
                continue
            cell_group = '-'.join(cell.shelf_number.split('-')[:2])
            if cell_group == shelf_group:
                cell.activate()
                activated += 1
        
        if activated > 0:
            print(f"✅ 선반 {shelf_group} 도착 → {activated}개 셀 활성화")
        else:
            print(f"⚠️  선반 {shelf_group}: 해당하는 셀 없음")
    
    def build_top_section(self):
        """상단: 사용자 선택, 주문번호 표시, 작업시작 버튼"""
        top_layout = BoxLayout(orientation='horizontal', size_hint_y=0.2, spacing=10)
        
        # 사용자 선택
        user_box = BoxLayout(orientation='vertical', size_hint_x=0.3)
        user_label = Label(
            text='사용자 선택',
            size_hint_y=0.3,
            font_size='16sp',
            font_name=FONT_NAME,
            color=(0, 0, 0, 1)
        )
        user_box.add_widget(user_label)
        
        user_buttons = BoxLayout(orientation='horizontal', size_hint_y=0.7, spacing=5)
        for user_id in [1, 2]:
            btn = Button(
                text=f'사용자{user_id}',
                font_size='20sp',
                font_name=FONT_NAME,
                background_color=(0.3, 0.6, 1, 1),
                color=(1, 1, 1, 1)
            )
            btn.bind(on_press=lambda x, uid=user_id: self.on_user_select(uid))
            user_buttons.add_widget(btn)
        user_box.add_widget(user_buttons)
        top_layout.add_widget(user_box)
        
        # 상태 표시
        self.status_label = Label(
            text='사용자를 선택하세요',
            font_size='24sp',
            font_name=FONT_NAME,
            bold=True,
            size_hint_x=0.4,
            color=(0, 0, 0, 1)
        )
        top_layout.add_widget(self.status_label)
        
        # 작업시작 버튼
        self.start_work_btn = Button(
            text='작업시작',
            font_size='24sp',
            font_name=FONT_NAME,
            background_color=(0.5, 0.5, 0.5, 1),
            color=(1, 1, 1, 1),
            size_hint_x=0.3,
            disabled=True
        )
        self.start_work_btn.bind(on_press=self.start_work)
        top_layout.add_widget(self.start_work_btn)
        
        self.add_widget(top_layout)
    
    def build_work_grid(self):
        """중앙: 작업 그리드 (2행 x 4열)"""
        grid_container = BoxLayout(orientation='vertical', size_hint_y=0.65)
        
        # 안내 레이블 (선반 대기 상태 표시)
        self.grid_label = Label(
            text='피킹 작업 | 선반 도착 대기 중...',
            size_hint_y=0.1,
            font_size='18sp',
            font_name=FONT_NAME,
            bold=True,
            color=(0, 0, 0, 1)
        )
        grid_container.add_widget(self.grid_label)
        
        # 2x4 그리드
        self.work_grid = GridLayout(cols=4, rows=2, spacing=5, size_hint_y=0.9)
        
        for i in range(8):
            cell = WorkCell(on_complete_callback=self.on_shelf_complete)
            self.work_cells.append(cell)
            self.work_grid.add_widget(cell)
        
        grid_container.add_widget(self.work_grid)
        self.add_widget(grid_container)
    
    def build_bottom_section(self):
        """하단: 완료 버튼"""
        bottom_layout = BoxLayout(orientation='horizontal', size_hint_y=0.15, padding=10)
        
        self.complete_btn = Button(
            text='주문 완료',
            font_size='24sp',
            font_name=FONT_NAME,
            background_color=(0.5, 0.5, 0.5, 1),
            color=(1, 1, 1, 1),
            disabled=True
        )
        self.complete_btn.bind(on_press=self.complete_order)
        bottom_layout.add_widget(self.complete_btn)
        
        self.add_widget(bottom_layout)
    
    def on_user_select(self, user_id):
        """사용자 선택"""
        self.selected_user_id = user_id
        self.current_order_number = 1
        
        self.status_label.text = f'사용자{user_id}\n주문 {self.current_order_number}번'
        self.start_work_btn.disabled = False
        self.start_work_btn.background_color = (0.2, 0.8, 0.2, 1)
        
        self.clear_grid()
        self.complete_btn.disabled = True
        self.complete_btn.background_color = (0.5, 0.5, 0.5, 1)
        self.grid_label.text = '피킹 작업 | 작업시작 버튼을 누르세요'
    
    def start_work(self, instance):
        """작업 시작 버튼 - MQTT 전송 + HTTP 요청"""
        if not self.selected_user_id:
            return
        
        print(f"\n🚀 작업 시작: 사용자{self.selected_user_id}, 주문{self.current_order_number}")
        
        # 1. MQTT: start_order 전송
        self.send_mqtt_start_order()
        
        # 2. HTTP: 피킹 리스트 요청
        self.fetch_picking_list()
    
    def send_mqtt_start_order(self):
        """MQTT: start_order 전송"""
        if not self.mqtt_client:
            print("⚠️  MQTT 미연결 - 메시지 전송 생략")
            return
        
        msg = {
            "type": "start_order",
            "사용자ID": self.selected_user_id,
            "주문번호": self.current_order_number
        }
        try:
            self.mqtt_client.publish("warehouse/order/start", json.dumps(msg, ensure_ascii=False))
            print(f"✅ MQTT 전송: start_order")
        except Exception as e:
            print(f"❌ MQTT 전송 실패: {e}")
    
    def fetch_picking_list(self):
        """HTTP: 피킹 리스트 요청"""
        url = f"http://{SERVER_IP}:{HTTP_PORT}/api/picking/user/{self.selected_user_id}/order/{self.current_order_number}"
        
        try:
            response = requests.get(url, timeout=5)
            data = response.json()
            
            if data['status'] == 'success':
                self.picking_list = data['피킹리스트']
                self.total_orders = data.get('총주문수', 0)
                print(f"✅ 피킹 리스트 수신: {len(self.picking_list)}개 항목 (총 주문 {self.total_orders}건 중 {self.current_order_number}번째)")
                
                # 그리드에 표시 (모든 셀 비활성 상태로)
                self.load_picking_list_to_grid()
                
                # 완료 버튼 활성화
                self.complete_btn.disabled = False
                self.complete_btn.background_color = (0, 0.7, 1, 1)
                
                self.grid_label.text = '피킹 작업 | 선반 도착 대기 중...'
            else:
                self.show_error_popup(f"주문을 찾을 수 없습니다:\n{data.get('message', '')}")
        
        except requests.exceptions.ConnectionError:
            self.show_error_popup(f"서버 연결 실패:\n{SERVER_IP}:{HTTP_PORT}")
        except requests.exceptions.Timeout:
            self.show_error_popup("서버 응답 시간 초과")
        except Exception as e:
            self.show_error_popup(f"오류 발생:\n{e}")
    
    def load_picking_list_to_grid(self):
        """피킹 리스트를 그리드에 로드 (모두 비활성 상태)"""
        # 전체 초기화
        for cell in self.work_cells:
            cell.clear()
        
        # 피킹 항목 설정 (최대 8개, 기본 비활성)
        for i, pick_item in enumerate(self.picking_list[:8]):
            cell = self.work_cells[i]
            cell.set_item(
                shelf_number=pick_item['선반번호'],
                item=pick_item['물건'],
                quantity=pick_item['개수']
            )
    
    def on_shelf_complete(self, shelf_number):
        """셀 터치 완료 콜백 - 같은 선반 그룹의 모든 셀 완료 시 MQTT 전송"""
        shelf_group = '-'.join(shelf_number.split('-')[:2])
        
        # 같은 선반 그룹의 미완료 셀 확인
        all_completed = True
        for cell in self.work_cells:
            if cell.is_empty:
                continue
            cell_group = '-'.join(cell.shelf_number.split('-')[:2])
            if cell_group == shelf_group and not cell.is_completed:
                all_completed = False
                break
        
        if all_completed:
            print(f"✅ 선반 {shelf_group} 전체 완료!")
            self.send_mqtt_shelf_complete(shelf_group)
        else:
            print(f"   → 선반 {shelf_group} 아직 미완료 셀 있음")
    
    def send_mqtt_shelf_complete(self, shelf_group):
        """MQTT: shelf_complete 전송"""
        if not self.mqtt_client:
            print("⚠️  MQTT 미연결 - 메시지 전송 생략")
            return
        
        msg = {
            "type": "shelf_complete",
            "사용자ID": self.selected_user_id,
            "선반번호": shelf_group
        }
        try:
            self.mqtt_client.publish("warehouse/shelf/complete", json.dumps(msg, ensure_ascii=False))
            print(f"✅ MQTT 전송: shelf_complete - {shelf_group}")
        except Exception as e:
            print(f"❌ MQTT 전송 실패: {e}")
    
    def complete_order(self, instance):
        """주문 완료 버튼 - 미완료 셀 확인 + MQTT 전송"""
        # 미완료 셀 찾기
        incomplete_cells = [
            cell for cell in self.work_cells
            if not cell.is_empty and not cell.is_completed
        ]
        
        if incomplete_cells:
            self.blink_cells(incomplete_cells, 5)
        else:
            self.send_mqtt_order_complete()
            
            # 마지막 주문인지 확인
            if self.total_orders > 0 and self.current_order_number >= self.total_orders:
                self.send_mqtt_all_orders_complete()
                self.show_all_done_popup()
            else:
                self.move_to_next_order()
    
    def send_mqtt_order_complete(self):
        """MQTT: order_complete 전송"""
        if not self.mqtt_client:
            print("⚠️  MQTT 미연결 - 메시지 전송 생략")
            return
        
        msg = {
            "type": "order_complete",
            "사용자ID": self.selected_user_id,
            "주문번호": self.current_order_number
        }
        try:
            self.mqtt_client.publish("warehouse/order/complete", json.dumps(msg, ensure_ascii=False))
            print(f"✅ MQTT 전송: order_complete - 주문{self.current_order_number}")
        except Exception as e:
            print(f"❌ MQTT 전송 실패: {e}")
    
    def send_mqtt_all_orders_complete(self):
        """MQTT: all_orders_complete 전송 - 모든 주문 완료"""
        if not self.mqtt_client:
            print("⚠️  MQTT 미연결 - 메시지 전송 생략")
            return
        
        msg = {
            "type": "all_orders_complete",
            "사용자ID": self.selected_user_id,
            "총주문수": self.total_orders
        }
        try:
            self.mqtt_client.publish("warehouse/order/all_complete", json.dumps(msg, ensure_ascii=False))
            print(f"✅ MQTT 전송: all_orders_complete - 총 {self.total_orders}건 완료")
        except Exception as e:
            print(f"❌ MQTT 전송 실패: {e}")
    
    def show_all_done_popup(self):
        """전체 주문 완료 팝업"""
        # 그리드 및 버튼 초기화
        self.clear_grid()
        self.complete_btn.disabled = True
        self.complete_btn.background_color = (0.5, 0.5, 0.5, 1)
        self.start_work_btn.disabled = True
        self.start_work_btn.background_color = (0.5, 0.5, 0.5, 1)
        self.grid_label.text = '모든 주문 완료!'
        self.status_label.text = f'사용자{self.selected_user_id}\n작업 완료 🎉'
        
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        msg = Label(
            text=f'모든 주문 완료!\n(총 {self.total_orders}건)',
            font_size='22sp',
            font_name=FONT_NAME,
            halign='center',
            color=(0, 0, 0, 1)
        )
        content.add_widget(msg)
        
        close_btn = Button(
            text='확인',
            size_hint_y=0.3,
            font_size='18sp',
            font_name=FONT_NAME,
            background_color=(0.2, 0.6, 1, 1),
            color=(1, 1, 1, 1)
        )
        content.add_widget(close_btn)
        
        popup = Popup(title='작업 완료', content=content, size_hint=(0.6, 0.4))
        close_btn.bind(on_press=popup.dismiss)
        popup.open()

    def move_to_next_order(self):
        """다음 주문으로 이동"""
        prev_order = self.current_order_number
        self.current_order_number += 1
        self.status_label.text = f'사용자{self.selected_user_id}\n주문 {self.current_order_number}번'
        
        self.clear_grid()
        
        self.complete_btn.disabled = True
        self.complete_btn.background_color = (0.5, 0.5, 0.5, 1)
        self.grid_label.text = '피킹 작업 | 작업시작 버튼을 누르세요'
        
        self.show_success_popup(f"주문 {prev_order}번 완료!")
    
    def clear_grid(self):
        """그리드 전체 초기화"""
        for cell in self.work_cells:
            cell.clear()
    
    def blink_cells(self, cells, count):
        """미완료 셀 깜빡임 (노란색)"""
        blink_state = {'count': 0, 'max_count': count * 2, 'is_yellow': False}
        
        def toggle_color(dt):
            if blink_state['count'] >= blink_state['max_count']:
                for cell in cells:
                    cell._apply_color()
                return False
            
            if blink_state['is_yellow']:
                for cell in cells:
                    cell._apply_color()
                blink_state['is_yellow'] = False
            else:
                for cell in cells:
                    cell.background_color = (1, 1, 0, 1)
                blink_state['is_yellow'] = True
            
            blink_state['count'] += 1
            return True
        
        Clock.schedule_interval(toggle_color, 0.3)
    
    def show_success_popup(self, message):
        """성공 팝업"""
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        msg = Label(
            text=message, font_size='20sp', font_name=FONT_NAME,
            halign='center', color=(0, 0, 0, 1)
        )
        content.add_widget(msg)
        
        close_btn = Button(
            text='확인', size_hint_y=0.3, font_size='18sp', font_name=FONT_NAME,
            background_color=(0.2, 0.8, 0.2, 1), color=(1, 1, 1, 1)
        )
        content.add_widget(close_btn)
        
        popup = Popup(title='완료', content=content, size_hint=(0.6, 0.4))
        close_btn.bind(on_press=popup.dismiss)
        popup.open()
    
    def show_error_popup(self, message):
        """에러 팝업"""
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        msg = Label(
            text=message, font_size='18sp', font_name=FONT_NAME,
            halign='center', color=(1, 0, 0, 1)
        )
        content.add_widget(msg)
        
        close_btn = Button(
            text='확인', size_hint_y=0.3, font_size='18sp', font_name=FONT_NAME,
            background_color=(0.7, 0.7, 0.7, 1), color=(0, 0, 0, 1)
        )
        content.add_widget(close_btn)
        
        popup = Popup(title='오류', content=content, size_hint=(0.7, 0.5))
        close_btn.bind(on_press=popup.dismiss)
        popup.open()


class WarehouseApp(App):
    def build(self):
        self.title = '창고 피킹 시스템'
        return WarehouseGUI()
    
    def on_stop(self):
        gui = self.root
        if gui.mqtt_client:
            gui.mqtt_client.loop_stop()
            gui.mqtt_client.disconnect()


if __name__ == '__main__':
    WarehouseApp().run()

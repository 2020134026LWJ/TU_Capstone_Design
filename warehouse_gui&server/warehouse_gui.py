#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
창고 피킹 GUI - v2 (라즈베리파이)
- MQTT 통신: 주문 시작/선반 완료/주문 완료
- HTTP API: 피킹 리스트 받기
"""
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
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
MQTT_PORT = 1883
HTTP_PORT = 5000
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')

def load_server_ip():
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f).get('server_ip', '172.30.1.72')
    except:
        return '172.30.1.72'

def save_server_ip(ip):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump({'server_ip': ip}, f)
    except:
        pass

SERVER_IP = load_server_ip()

# 윈도우 크기 설정 (라즈베리파이 5인치 터치스크린)
Window.size = (800, 450)  # 높이를 450으로 유지
Window.borderless = True  # 테두리 제거
Window.fullscreen = False  # 전체화면은 False (크기 고정)
Window.top = 40  # 화면 상단에서 40픽셀 아래에 배치

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


class WorkCell(Button):
    """작업 셀 (2x4 그리드의 각 칸)"""
    def __init__(self, shelf_number='', item='', quantity=0, on_complete_callback=None, **kwargs):
        super(WorkCell, self).__init__(**kwargs)
        self.shelf_number = shelf_number
        self.item = item
        self.quantity = quantity
        self.on_complete_callback = on_complete_callback
        
        self.background_color = (0.9, 0.9, 0.9, 1)
        self.color = (0, 0, 0, 1)
        self.font_size = '16sp'
        self.font_name = FONT_NAME
        self.is_completed = False
        
        # 텍스트 설정
        if shelf_number and item:
            self.text = f"{shelf_number}\n{item}\n({quantity}개)"
        else:
            self.text = ''
    
    def on_press(self):
        """셀 클릭시 빨간색으로 변경 + MQTT 전송"""
        if not self.text:  # 빈 셀이면 무시
            return
            
        if not self.is_completed:
            self.background_color = (1, 0, 0, 0.7)  # 빨간색
            self.is_completed = True
            
            # 선반 완료 콜백 호출
            if self.on_complete_callback:
                self.on_complete_callback(self.shelf_number)
        else:
            # 다시 클릭하면 취소
            self.background_color = (0.9, 0.9, 0.9, 1)
            self.is_completed = False


class WarehouseGUI(BoxLayout):
    def __init__(self, **kwargs):
        super(WarehouseGUI, self).__init__(**kwargs)
        
        # 배경색
        with self.canvas.before:
            Color(0.5, 0.2, 0.05, 1)
            self.rect = Rectangle(size=self.size, pos=self.pos)
        self.bind(size=self.on_size_change, pos=self.on_size_change)
        
        self.orientation = 'vertical'
        self.padding = 10
        self.spacing = 10
        
        # 상태 변수
        self.selected_user_id = None
        self.current_order_number = 1  # 현재 주문 번호
        self.work_cells = []
        self.picking_list = []
        
        # MQTT 클라이언트
        self.mqtt_client = None
        Clock.schedule_once(lambda dt: self.show_ip_popup(), 0.3)
        
        # UI 구성
        self.build_top_section()
        self.build_work_grid()
        self.build_bottom_section()
    
    def on_size_change(self, instance, value):
        """창 크기 변경 시 배경 업데이트"""
        if hasattr(self, 'rect'):
            self.rect.size = self.size
            self.rect.pos = self.pos
    
    def show_ip_popup(self):
        """서버 IP 입력 팝업"""
        global SERVER_IP

        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        content.add_widget(Label(
            text=f'서버 IP 주소를 입력하세요',
            font_name=FONT_NAME, font_size='16sp', size_hint_y=None, height=40
        ))
        ip_input = TextInput(
            text=SERVER_IP,
            multiline=False,
            font_size='18sp',
            size_hint_y=None, height=44
        )
        content.add_widget(ip_input)
        btn = Button(
            text='확인',
            font_name=FONT_NAME,
            size_hint_y=None, height=50,
            background_color=(0.2, 0.6, 0.2, 1)
        )
        content.add_widget(btn)

        popup = Popup(
            title='서버 IP 설정',
            content=content,
            size_hint=(0.6, 0.45),
            auto_dismiss=False
        )

        def on_confirm(instance):
            global SERVER_IP
            SERVER_IP = ip_input.text.strip()
            save_server_ip(SERVER_IP)
            popup.dismiss()
            self.init_mqtt()

        btn.bind(on_press=on_confirm)
        popup.open()

    def init_mqtt(self):
        """MQTT 클라이언트 초기화"""
        if mqtt is None:
            print("⚠️  MQTT 없이 실행 (테스트 모드)")
            return
        
        try:
            import random
            client_id = f"raspberrypi_gui_{random.randint(1000, 9999)}"
            self.mqtt_client = mqtt.Client(client_id=client_id)
            self.mqtt_client.connect(SERVER_IP, MQTT_PORT, 60)
            self.mqtt_client.loop_start()
            print(f"✅ MQTT 연결: {SERVER_IP}:{MQTT_PORT} (ID: {client_id})")
        except Exception as e:
            print(f"❌ MQTT 연결 실패: {e}")
            self.mqtt_client = None
    
    def build_top_section(self):
        """상단: 사용자 선택, 주문번호 표시, 작업시작 버튼"""
        top_layout = BoxLayout(orientation='horizontal', size_hint_y=0.2, spacing=10)
        
        # 왼쪽: 사용자 선택
        user_box = BoxLayout(orientation='vertical', size_hint_x=0.3)
        user_label = Label(
            text='사용자 선택',
            size_hint_y=0.3,
            font_size='16sp',
            font_name=FONT_NAME,
            color=(0, 0, 0, 1)
        )
        user_box.add_widget(user_label)
        
        # 사용자 버튼
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
        
        # 중앙: 선택된 사용자 & 주문번호 표시
        self.status_label = Label(
            text='사용자를 선택하세요',
            font_size='24sp',  # 32sp에서 24sp로 축소
            font_name=FONT_NAME,
            bold=True,
            size_hint_x=0.4,
            color=(0, 0, 0, 1)
        )
        top_layout.add_widget(self.status_label)
        
        # 오른쪽: 작업시작 버튼
        self.start_work_btn = Button(
            text='작업시작',
            font_size='24sp',
            font_name=FONT_NAME,
            background_color=(0.2, 0.8, 0.2, 1),
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
        
        grid_label = Label(
            text='피킹 작업',
            size_hint_y=0.1,
            font_size='18sp',
            font_name=FONT_NAME,
            bold=True,
            color=(0, 0, 0, 1)
        )
        grid_container.add_widget(grid_label)
        
        # 2x4 그리드
        self.work_grid = GridLayout(cols=4, rows=2, spacing=5, size_hint_y=0.9)
        
        # 8개의 셀 생성
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
            background_color=(0, 0.5, 1, 1),
            color=(1, 1, 1, 1),
            disabled=True
        )
        self.complete_btn.bind(on_press=self.complete_order)
        bottom_layout.add_widget(self.complete_btn)
        
        self.add_widget(bottom_layout)
    
    def on_user_select(self, user_id):
        """사용자 선택"""
        self.selected_user_id = user_id
        self.current_order_number = 1  # 주문번호 초기화
        
        self.status_label.text = f'사용자{user_id}\n주문 {self.current_order_number}번'
        self.start_work_btn.disabled = False
        self.start_work_btn.background_color = (0.2, 0.8, 0.2, 1)
        
        # 그리드 초기화
        self.clear_grid()
        self.complete_btn.disabled = True
    
    def start_work(self, instance):
        """작업 시작 버튼 - MQTT 전송 + HTTP 요청"""
        if not self.selected_user_id:
            return
        
        print(f"\n🚀 작업 시작: 사용자{self.selected_user_id}, 주문{self.current_order_number}")
        
        # 1. MQTT로 주문 시작 알림
        self.send_mqtt_start_order()
        
        # 2. HTTP로 피킹 리스트 받기
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
                print(f"✅ 피킹 리스트 수신: {len(self.picking_list)}개 항목")
                
                # 그리드에 표시
                self.load_picking_list_to_grid()
                
                # 완료 버튼 활성화
                self.complete_btn.disabled = False
                self.complete_btn.background_color = (0, 0.7, 1, 1)
            else:
                self.show_error_popup(f"주문을 찾을 수 없습니다:\n{data.get('message', '')}")
        
        except requests.exceptions.ConnectionError:
            self.show_error_popup(f"서버 연결 실패:\n{SERVER_IP}:{HTTP_PORT}")
        except requests.exceptions.Timeout:
            self.show_error_popup("서버 응답 시간 초과")
        except Exception as e:
            self.show_error_popup(f"오류 발생:\n{e}")
    
    def load_picking_list_to_grid(self):
        """피킹 리스트를 그리드에 로드"""
        # 그리드 초기화
        for cell in self.work_cells:
            cell.shelf_number = ''
            cell.item = ''
            cell.quantity = 0
            cell.text = ''
            cell.background_color = (0.9, 0.9, 0.9, 1)
            cell.is_completed = False
        
        # 피킹 리스트 표시 (최대 8개)
        for i, pick_item in enumerate(self.picking_list[:8]):
            cell = self.work_cells[i]
            cell.shelf_number = pick_item['선반번호']
            cell.item = pick_item['물건']
            cell.quantity = pick_item['개수']
            cell.text = f"{cell.shelf_number}\n{cell.item}\n({cell.quantity}개)"
            cell.background_color = (0.93, 0.93, 0.93, 1)
    
    def on_shelf_complete(self, shelf_number):
        """선반 완료 콜백 - 같은 선반(구역-열)의 모든 층 완료 시 MQTT 전송"""
        print(f"📦 셀 완료: {shelf_number}")
        
        # 선반 그룹 추출 (1-1-1 → 1-1)
        shelf_group = '-'.join(shelf_number.split('-')[:2])
        
        # 같은 선반 그룹의 모든 셀 확인
        all_completed = True
        for cell in self.work_cells:
            if cell.text:  # 셀에 내용이 있으면
                cell_shelf_group = '-'.join(cell.shelf_number.split('-')[:2])
                if cell_shelf_group == shelf_group and not cell.is_completed:
                    all_completed = False
                    break
        
        # 같은 선반의 모든 층이 완료되었으면 MQTT 전송
        if all_completed:
            print(f"✅ 선반 {shelf_group} 전체 완료!")
            
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
        else:
            print(f"   → 선반 {shelf_group} 아직 미완료 셀 있음")
    
    def complete_order(self, instance):
        """주문 완료 버튼 - 모든 셀 완료 확인 + MQTT 전송"""
        # 미완료 셀 찾기
        incomplete_cells = []
        for cell in self.work_cells:
            if cell.text and not cell.is_completed:
                incomplete_cells.append(cell)
        
        if incomplete_cells:
            # 미완료 셀 깜빡임
            self.blink_cells(incomplete_cells, 5)
        else:
            # 모든 셀 완료 → MQTT 전송
            self.send_mqtt_order_complete()
            
            # 다음 주문으로
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
    
    def move_to_next_order(self):
        """다음 주문으로 이동"""
        self.current_order_number += 1
        self.status_label.text = f'사용자{self.selected_user_id}\n주문 {self.current_order_number}번'
        
        # 그리드 초기화
        self.clear_grid()
        
        # 완료 버튼 비활성화
        self.complete_btn.disabled = True
        self.complete_btn.background_color = (0.5, 0.5, 0.5, 1)
        
        # 완료 팝업
        self.show_success_popup(f"주문 {self.current_order_number - 1}번 완료!")
    
    def clear_grid(self):
        """그리드 초기화"""
        for cell in self.work_cells:
            cell.shelf_number = ''
            cell.item = ''
            cell.quantity = 0
            cell.text = ''
            cell.background_color = (0.9, 0.9, 0.9, 1)
            cell.is_completed = False
    
    def blink_cells(self, cells, count):
        """셀 깜빡임 (노란색)"""
        blink_state = {'count': 0, 'max_count': count * 2, 'is_yellow': False}
        
        def toggle_color(dt):
            if blink_state['count'] >= blink_state['max_count']:
                for cell in cells:
                    cell.background_color = (0.9, 0.9, 0.9, 1)
                return False
            
            if blink_state['is_yellow']:
                for cell in cells:
                    cell.background_color = (0.9, 0.9, 0.9, 1)
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
            text=message,
            font_size='20sp',
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
            background_color=(0.2, 0.8, 0.2, 1),
            color=(1, 1, 1, 1)
        )
        content.add_widget(close_btn)
        
        popup = Popup(
            title='완료',
            content=content,
            size_hint=(0.6, 0.4)
        )
        close_btn.bind(on_press=popup.dismiss)
        popup.open()
    
    def show_error_popup(self, message):
        """에러 팝업"""
        content = BoxLayout(orientation='vertical', padding=10, spacing=10)
        
        msg = Label(
            text=message,
            font_size='18sp',
            font_name=FONT_NAME,
            halign='center',
            color=(1, 0, 0, 1)
        )
        content.add_widget(msg)
        
        close_btn = Button(
            text='확인',
            size_hint_y=0.3,
            font_size='18sp',
            font_name=FONT_NAME,
            background_color=(0.7, 0.7, 0.7, 1),
            color=(0, 0, 0, 1)
        )
        content.add_widget(close_btn)
        
        popup = Popup(
            title='오류',
            content=content,
            size_hint=(0.7, 0.5)
        )
        close_btn.bind(on_press=popup.dismiss)
        popup.open()


class WarehouseApp(App):
    def build(self):
        self.title = '창고 피킹 시스템'
        return WarehouseGUI()
    
    def on_stop(self):
        """앱 종료 시 MQTT 연결 해제"""
        gui = self.root
        if gui.mqtt_client:
            gui.mqtt_client.loop_stop()
            gui.mqtt_client.disconnect()


if __name__ == '__main__':
    WarehouseApp().run()
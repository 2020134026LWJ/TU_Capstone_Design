#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
창고 피킹 GUI - KivyMD 버전
"""
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDRaisedButton, MDFlatButton
from kivymd.uix.card import MDCard
from kivymd.uix.dialog import MDDialog
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.core.window import Window
from kivy.core.text import LabelBase
from kivy.clock import Clock
import os
import json
import threading
import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("paho-mqtt 필요: pip install paho-mqtt")
    mqtt = None

# 설정
SERVER_IP = 'UB-Region5.local'   # mDNS 이름 — 같은 네트워크면 IP가 바뀌어도 그대로 동작 (IP 하드코딩 제거)
MQTT_PORT = 1883
HTTP_PORT = 5000

# 이 파이가 설치된 작업대 번호 (작업대-파이 고정: 파이1=1, 파이2=2)
# 사용자는 파이에서 1·2 모두 선택 가능하지만 작업대는 파이에 고정된다.
# AGV가 선반을 어느 작업대로 보낼지 알 수 있도록 모든 발행 메시지에 실어 보낸다.
WORKSTATION_ID = 2

Window.size = (800, 448)
Window.borderless = True
Window.fullscreen = False
Window.top = 32

# 한글 폰트
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
    FONT_NAME = 'Roboto'

# 색상
COLOR_BG            = (0.95, 0.95, 0.95, 1)
COLOR_CELL_EMPTY    = (0.88, 0.88, 0.88, 1)
COLOR_CELL_INACTIVE = (0.78, 0.78, 0.78, 1)
COLOR_CELL_ACTIVE   = (0.13, 0.59, 0.95, 1)   # 파란색
COLOR_CELL_DONE     = (0.90, 0.22, 0.21, 1)   # 빨간색
COLOR_CELL_BLINK    = (1.00, 0.87, 0.09, 1)   # 노란색 (경고)
COLOR_USER_DEFAULT  = (0.55, 0.55, 0.55, 1)
COLOR_USER_SELECTED = (0.13, 0.59, 0.95, 1)
COLOR_BTN_START     = (0.13, 0.59, 0.95, 1)
COLOR_BTN_NEXT      = (0.22, 0.66, 0.29, 1)


class WorkCell(MDCard):
    """작업 셀 (2x4 그리드의 각 칸)"""

    def __init__(self, on_complete_callback=None, **kwargs):
        super().__init__(**kwargs)
        self.on_complete_callback = on_complete_callback
        self.orientation = 'vertical'
        self.padding = [10, 6, 10, 6]
        self.spacing = 2
        self.radius = [12]
        self.elevation = 2

        self.shelf_number = ''
        self.item = ''
        self.quantity = 0
        self.is_completed = False
        self.is_active = False
        self.is_empty = True

        self.shelf_label = Label(
            text='',
            font_size='13sp',
            font_name=FONT_NAME,
            halign='left',
            valign='middle',
            size_hint_y=0.25,
            color=(0.55, 0.55, 0.55, 1),
        )
        self.item_label = Label(
            text='',
            font_size='21sp',
            font_name=FONT_NAME,
            halign='center',
            valign='middle',
            bold=True,
            size_hint_y=0.5,
            color=(0.15, 0.15, 0.15, 1),
        )
        self.qty_label = Label(
            text='',
            font_size='15sp',
            font_name=FONT_NAME,
            halign='center',
            valign='middle',
            size_hint_y=0.25,
            color=(0.35, 0.35, 0.35, 1),
        )
        self.add_widget(self.shelf_label)
        self.add_widget(self.item_label)
        self.add_widget(self.qty_label)
        self._apply_color()

    def set_item(self, shelf_number, item, quantity, completed=False):
        self.shelf_number = shelf_number
        self.item = item
        self.quantity = quantity
        self.is_completed = completed   # 자동 복원: 이미 완료한 품목은 빨강 표시
        self.is_active = False
        self.is_empty = False
        self.shelf_label.text = shelf_number
        self.item_label.text = item
        self.qty_label.text = f'{quantity}개'
        self._apply_color()

    def clear(self):
        self.shelf_number = ''
        self.item = ''
        self.quantity = 0
        self.shelf_label.text = ''
        self.item_label.text = ''
        self.qty_label.text = ''
        self.is_completed = False
        self.is_active = False
        self.is_empty = True
        self._apply_color()

    def activate(self):
        if self.is_empty or self.is_completed:
            return
        self.is_active = True
        self._apply_color()

    def _apply_color(self):
        if self.is_empty:
            self.md_bg_color = COLOR_CELL_EMPTY
            self._set_text_colors((0.7, 0.7, 0.7, 1), (0.7, 0.7, 0.7, 1), (0.7, 0.7, 0.7, 1))
        elif self.is_completed:
            self.md_bg_color = COLOR_CELL_DONE
            self._set_text_colors((1, 1, 1, 0.85), (1, 1, 1, 1), (1, 1, 1, 0.85))
        elif self.is_active:
            self.md_bg_color = COLOR_CELL_ACTIVE
            self._set_text_colors((1, 1, 1, 0.85), (1, 1, 1, 1), (1, 1, 1, 0.85))
        else:
            self.md_bg_color = COLOR_CELL_INACTIVE
            self._set_text_colors((0.4, 0.4, 0.4, 1), (0.2, 0.2, 0.2, 1), (0.4, 0.4, 0.4, 1))

    def _set_text_colors(self, shelf_c, item_c, qty_c):
        self.shelf_label.color = shelf_c
        self.item_label.color = item_c
        self.qty_label.color = qty_c

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            if not self.is_empty and self.is_active and not self.is_completed:
                self.is_completed = True
                self._apply_color()
                if self.on_complete_callback:
                    self.on_complete_callback(self)
        return super().on_touch_down(touch)


class WarehouseGUI(MDBoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.orientation = 'vertical'
        self.padding = [8, 8, 8, 8]
        self.spacing = 6
        self.md_bg_color = COLOR_BG

        self.selected_user_id = None
        self.current_order_number = 1
        self.total_orders = 0
        self.work_cells = []
        self.picking_list = []
        self.user_buttons = {}
        self._order_advancing = False   # 주문 자동 진행 중복 방지 가드
        self._pending_active_shelf = None  # 사용자 전환 시 복원할 활성 선반 그룹

        self.mqtt_client = None
        self.init_mqtt()

        self.build_top_section()
        self.build_work_grid()

    def init_mqtt(self):
        if mqtt is None:
            print("MQTT 없이 실행")
            return
        try:
            import random
            client_id = f"raspberrypi_gui_{random.randint(1000, 9999)}"
            self.mqtt_client = mqtt.Client(client_id=client_id)
            self.mqtt_client.on_message = self.on_mqtt_message
            self.mqtt_client.connect(SERVER_IP, MQTT_PORT, 60)
            self.mqtt_client.subscribe("warehouse/shelf/arrived")
            self.mqtt_client.loop_start()
            print(f"MQTT 연결: {SERVER_IP}:{MQTT_PORT}")
        except Exception as e:
            print(f"MQTT 연결 실패: {e}")
            self.mqtt_client = None

    def on_mqtt_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
            if msg.topic == "warehouse/shelf/arrived":
                # 작업대-파이 고정 → 내 작업대로 온 선반만 처리.
                # (작업대 필드가 없는 옛 메시지는 사용자ID로 폴백)
                ws = payload.get('작업대')
                for_me = (ws == WORKSTATION_ID) if ws is not None \
                    else (payload.get('사용자ID') == self.selected_user_id)
                if for_me:
                    shelf_group = payload.get('선반번호')
                    Clock.schedule_once(
                        lambda dt: self.activate_shelf_cells(shelf_group), 0
                    )
        except Exception as e:
            print(f"MQTT 메시지 오류: {e}")

    def activate_shelf_cells(self, shelf_group):
        for cell in self.work_cells:
            if cell.is_empty:
                continue
            if '-'.join(cell.shelf_number.split('-')[:2]) == shelf_group:
                cell.activate()

    # ── UI 구성 ──────────────────────────────────────────────────────

    def build_top_section(self):
        top = MDBoxLayout(orientation='horizontal', size_hint_y=0.20, spacing=10)

        # 좌: 사용자 버튼
        user_box = MDBoxLayout(orientation='vertical', size_hint_x=0.32, spacing=4)
        user_box.add_widget(Label(size_hint_y=0.3))

        user_btns = MDBoxLayout(orientation='horizontal', size_hint_y=0.7, spacing=6)
        for user_id in [1, 2]:
            btn = MDRaisedButton(
                text=f'사용자{user_id}',
                font_size='17sp',
                font_name=FONT_NAME,
                md_bg_color=COLOR_USER_DEFAULT,
                size_hint=(1, 1),
            )
            btn.bind(on_press=lambda x, uid=user_id: self.on_user_select(uid))
            self.user_buttons[user_id] = btn
            user_btns.add_widget(btn)
        user_box.add_widget(user_btns)
        top.add_widget(user_box)

        # 중앙: 상태 표시 (시작 버튼 제거 - 사용자 선택 시 자동 로드)
        self.status_label = Label(
            text='사용자를 선택하세요',
            font_size='19sp',
            font_name=FONT_NAME,
            bold=True,
            halign='center',
            valign='middle',
            size_hint_x=0.48,
            color=(0.2, 0.2, 0.2, 1),
        )
        top.add_widget(self.status_label)

        # 우측 끝: '다음 작업' 버튼 (주문의 모든 품목 완료 시에만 노출)
        self.next_button = MDRaisedButton(
            text='다음 작업',
            font_size='18sp',
            font_name=FONT_NAME,
            md_bg_color=COLOR_BTN_NEXT,
            size_hint=(0.20, 1),
            disabled=True,
            opacity=0,
        )
        self.next_button.bind(on_press=self.on_next_button)
        top.add_widget(self.next_button)

        self.add_widget(top)

    def build_work_grid(self):
        container = MDBoxLayout(orientation='vertical', size_hint_y=0.80)

        self.grid_label = Label(
            text='피킹 작업 | 선반 도착 대기 중...',
            size_hint_y=None,
            height=26,
            font_size='15sp',
            font_name=FONT_NAME,
            bold=True,
            halign='center',
            valign='middle',
            color=(0.4, 0.4, 0.4, 1),
        )
        container.add_widget(self.grid_label)

        grid = GridLayout(cols=4, rows=2, spacing=6)
        for _ in range(8):
            cell = WorkCell(on_complete_callback=self.on_shelf_complete)
            self.work_cells.append(cell)
            grid.add_widget(cell)
        container.add_widget(grid)
        self.add_widget(container)

    # ── 이벤트 처리 ──────────────────────────────────────────────────

    def on_user_select(self, user_id):
        self.selected_user_id = user_id
        self._order_advancing = False

        for uid, btn in self.user_buttons.items():
            btn.md_bg_color = COLOR_USER_SELECTED if uid == user_id else COLOR_USER_DEFAULT

        self.clear_grid()
        self.status_label.text = f'사용자{user_id}  |  진행 상태 불러오는 중...'
        self.grid_label.text = '피킹 작업 | 진행 상태 불러오는 중...'

        # 서버에서 마지막 작업 주문번호 조회(자동 복원). UI 멈춤 방지를 위해 스레드 사용
        threading.Thread(target=self._load_user_state, args=(user_id,), daemon=True).start()

    def _load_user_state(self, user_id):
        order_number = 1
        active_shelf = None
        busy_ws = None
        try:
            url = f"http://{SERVER_IP}:{HTTP_PORT}/api/user/{user_id}/state"
            resp = requests.get(url, timeout=5)
            data = resp.json()
            if data.get('status') == 'success':
                order_number = data.get('현재주문번호', 1)
                active_shelf = data.get('활성선반')
                busy_ws = data.get('점유작업대')
        except Exception as e:
            print(f"재개 상태 조회 실패(기본값 1): {e}")
        Clock.schedule_once(
            lambda dt: self._apply_user_state(user_id, order_number, active_shelf, busy_ws), 0
        )

    def _apply_user_state(self, user_id, order_number, active_shelf=None, busy_ws=None):
        # 조회 중 사용자가 다른 버튼을 눌렀다면 무시
        if self.selected_user_id != user_id:
            return
        # 다른 작업대가 점유 중인 사용자면 선택 거부
        if busy_ws is not None and busy_ws != WORKSTATION_ID:
            self.status_label.text = f'사용자{user_id}는 작업대{busy_ws}에서 사용 중'
            self.grid_label.text = '다른 작업대 사용 중 — 선택할 수 없습니다'
            self.selected_user_id = None
            for btn in self.user_buttons.values():
                btn.md_bg_color = COLOR_USER_DEFAULT
            self.clear_grid()
            return
        self.current_order_number = order_number
        # 전환 시 놓친 shelf_arrived 보완용: 서버가 기억하는 활성 선반을 그리드 로드 후 복원
        self._pending_active_shelf = active_shelf
        self.status_label.text = f'사용자{user_id}  |  주문 {order_number}번'
        # 자동 복원: 마지막 작업 주문을 즉시 불러와 완료 품목을 빨강으로 표시
        self.start_work(None)

    def start_work(self, instance):
        if not self.selected_user_id:
            return
        print(f"작업 시작: 사용자{self.selected_user_id}, 주문{self.current_order_number}")
        self.fetch_picking_list()

    # ── MQTT ─────────────────────────────────────────────────────────

    def send_mqtt_start_order(self):
        if not self.mqtt_client:
            return
        msg = {
            "type": "start_order",
            "사용자ID": self.selected_user_id,
            "주문번호": self.current_order_number,
            "작업대": WORKSTATION_ID,
        }
        try:
            self.mqtt_client.publish("warehouse/order/start", json.dumps(msg, ensure_ascii=False))
        except Exception as e:
            print(f"MQTT 전송 실패: {e}")

    def send_mqtt_shelf_complete(self, cells):
        # 같은 선반의 셀들을 묶어 '선반 완료'로 1번만 발행 (셀당 발행 금지)
        if not self.mqtt_client or not cells:
            return
        shelf_group = '-'.join(cells[0].shelf_number.split('-')[:2])
        items = [
            {"물건": c.item, "개수": c.quantity, "선반번호": c.shelf_number}
            for c in cells
        ]
        msg = {
            "type": "shelf_complete",
            "사용자ID": self.selected_user_id,
            "주문번호": self.current_order_number,
            "선반번호": shelf_group,
            "품목목록": items,
            "작업대": WORKSTATION_ID,
        }
        try:
            self.mqtt_client.publish("warehouse/shelf/complete", json.dumps(msg, ensure_ascii=False))
        except Exception as e:
            print(f"MQTT 전송 실패: {e}")

    def send_mqtt_order_complete(self):
        if not self.mqtt_client:
            return
        msg = {
            "type": "order_complete",
            "사용자ID": self.selected_user_id,
            "주문번호": self.current_order_number,
            "작업대": WORKSTATION_ID,
        }
        try:
            self.mqtt_client.publish("warehouse/order/complete", json.dumps(msg, ensure_ascii=False))
        except Exception as e:
            print(f"MQTT 전송 실패: {e}")

    def send_mqtt_release_hold(self):
        """그리드 빨강(주문 품목 전부 완료) 시 점유만 해제 요청.
        '다음 작업'을 누르면 order/start로 다시 점유된다."""
        if not self.mqtt_client:
            return
        msg = {
            "type": "release_hold",
            "사용자ID": self.selected_user_id,
            "주문번호": self.current_order_number,
            "작업대": WORKSTATION_ID,
        }
        try:
            self.mqtt_client.publish("warehouse/order/release_hold", json.dumps(msg, ensure_ascii=False))
        except Exception as e:
            print(f"MQTT 전송 실패: {e}")

    def send_mqtt_all_orders_complete(self):
        if not self.mqtt_client:
            return
        msg = {
            "type": "all_orders_complete",
            "사용자ID": self.selected_user_id,
            "총주문수": self.total_orders,
            "작업대": WORKSTATION_ID,
        }
        try:
            self.mqtt_client.publish("warehouse/order/all_complete", json.dumps(msg, ensure_ascii=False))
        except Exception as e:
            print(f"MQTT 전송 실패: {e}")

    # ── 피킹 로직 ────────────────────────────────────────────────────

    def fetch_picking_list(self):
        url = (f"http://{SERVER_IP}:{HTTP_PORT}/api/picking"
               f"/user/{self.selected_user_id}/order/{self.current_order_number}")
        try:
            response = requests.get(url, timeout=5)
            data = response.json()
            if data['status'] == 'success':
                self.picking_list = data['피킹리스트']
                self.total_orders = data.get('총주문수', 0)
                if not self.picking_list:
                    print(f"주문 {self.current_order_number}: 재고 없음, 다음 주문으로 자동 이동")
                    self._advance_or_complete()
                    return
                # 재진입: 모든 품목이 이미 완료된 주문이면 자동 진행하지 않고
                # 완료 화면 + '다음 작업' 버튼을 띄워 사용자가 직접 넘어가게 한다.
                if all(p.get('완료') for p in self.picking_list):
                    print(f"주문 {self.current_order_number}: 이미 완료됨 → '다음 작업' 대기")
                    self.load_picking_list_to_grid()
                    self.grid_label.text = '주문 완료 | "다음 작업" 버튼을 누르세요'
                    self._show_next_button()
                    self._unlock_all_users()
                    # 서버 점유도 해제 → 같은 작업대에서 사용자 전환 시 유령 점유 방지
                    self.send_mqtt_release_hold()
                    return
                # 피킹리스트 확인 후 MQTT 전송
                self.send_mqtt_start_order()
                self.load_picking_list_to_grid()
                self.grid_label.text = '피킹 작업 | 선반 도착 대기 중...'
                # 작업 진행 중 → 다른 사용자로 전환 잠금
                self._lock_other_users(self.selected_user_id)
            else:
                msg = data.get('message', '')
                if any(k in msg for k in ['재고', '없', '품절']):
                    print(f"주문 {self.current_order_number}: {msg}, 다음 주문으로 자동 이동")
                    self._advance_or_complete()
                else:
                    self.show_error_dialog(f"주문을 찾을 수 없습니다:\n{msg}")
        except requests.exceptions.ConnectionError:
            self.show_error_dialog(f"서버 연결 실패:\n{SERVER_IP}:{HTTP_PORT}")
        except requests.exceptions.Timeout:
            self.show_error_dialog("서버 응답 시간 초과")
        except Exception as e:
            self.show_error_dialog(f"오류 발생:\n{e}")

    def _advance_or_complete(self):
        if self.total_orders > 0 and self.current_order_number >= self.total_orders:
            self.send_mqtt_all_orders_complete()
            self.show_all_done_dialog()
        else:
            self.move_to_next_order()

    def load_picking_list_to_grid(self):
        self._hide_next_button()
        for cell in self.work_cells:
            cell.clear()
        for i, pick_item in enumerate(self.picking_list[:8]):
            self.work_cells[i].set_item(
                shelf_number=pick_item['선반번호'],
                item=pick_item['물건'],
                quantity=pick_item['개수'],
                completed=pick_item.get('완료', False),
            )
        # 사용자 전환으로 놓친 선반 도착 신호 복원 → 작업 중이던 선반 셀 재활성화
        if self._pending_active_shelf:
            self.activate_shelf_cells(self._pending_active_shelf)
            self.grid_label.text = f'피킹 작업 | 선반 {self._pending_active_shelf} 작업 중'
        self._pending_active_shelf = None

    def on_shelf_complete(self, cell):
        # 같은 선반의 셀이 전부 완료됐을 때만 '선반 완료'를 1번 발행한다.
        # (1-1-1, 1-1-2가 한 주문에 있으면 둘 다 빨강이 된 뒤에만 서버로 전송)
        group = '-'.join(cell.shelf_number.split('-')[:2])
        group_cells = [
            c for c in self.work_cells
            if not c.is_empty and '-'.join(c.shelf_number.split('-')[:2]) == group
        ]
        if all(c.is_completed for c in group_cells):
            self.send_mqtt_shelf_complete(group_cells)
            print(f"선반 완료 전송: {group} ({len(group_cells)}품목)")
        else:
            done = sum(1 for c in group_cells if c.is_completed)
            print(f"품목 완료(대기): {cell.item} ({cell.shelf_number}) "
                  f"- 선반 {group} {done}/{len(group_cells)} 완료")
        # 현재 주문의 모든 셀을 완료하면 '다음 작업' 버튼을 노출 (수동 진행)
        remaining = [c for c in self.work_cells if not c.is_empty and not c.is_completed]
        if not remaining and not self._order_advancing:
            self.grid_label.text = '주문 완료 | "다음 작업" 버튼을 누르세요'
            self._show_next_button()
            # 주문 1건 완료 → 잠금 해제 (다른 사용자가 작업할 수 있음)
            # '다음 작업'을 누르면 다음 주문이 로드되며 fetch_picking_list에서 다시 잠금
            self._unlock_all_users()
            # 서버 점유도 해제 → 같은 작업대에서 사용자 전환 시 유령 점유 방지
            self.send_mqtt_release_hold()

    def on_next_button(self, instance):
        """'다음 작업' 버튼: 서버 점유 확인 후 다음 주문으로 진행.
        다른 작업대가 이 사용자를 선점(작업 중)하고 있으면 차단하고 팝업을 띄운다."""
        self._hide_next_button()
        threading.Thread(
            target=self._check_hold_and_advance,
            args=(self.selected_user_id,), daemon=True
        ).start()

    def _check_hold_and_advance(self, user_id):
        busy_ws = None
        try:
            url = f"http://{SERVER_IP}:{HTTP_PORT}/api/user/{user_id}/state"
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                busy_ws = resp.json().get('점유작업대')
        except Exception as e:
            print(f"점유 확인 실패: {e}")
        Clock.schedule_once(
            lambda dt: self._after_hold_check(user_id, busy_ws), 0
        )

    def _after_hold_check(self, user_id, busy_ws):
        # 확인 도중 사용자가 바뀌었으면 무시
        if self.selected_user_id != user_id:
            return
        # 다른 작업대가 이 사용자를 선점 중이면 진행 차단
        if busy_ws is not None and busy_ws != WORKSTATION_ID:
            self.show_error_dialog(f'작업대{busy_ws}에서 선점 중입니다', title='알림')
            self._show_next_button()  # 버튼 다시 노출 → 해제 후 재시도 가능
            return
        self._finish_order_and_advance()

    def _show_next_button(self):
        self.next_button.disabled = False
        self.next_button.opacity = 1

    def _hide_next_button(self):
        self.next_button.disabled = True
        self.next_button.opacity = 0

    def _lock_other_users(self, active_user_id):
        """작업 진행 중 → 선택된 사용자 외 버튼을 잠가 전환을 막는다."""
        for uid, btn in self.user_buttons.items():
            btn.disabled = (uid != active_user_id)

    def _unlock_all_users(self):
        """전체 주문 완료 → 모든 사용자 버튼 잠금 해제."""
        for btn in self.user_buttons.values():
            btn.disabled = False

    def _finish_order_and_advance(self):
        """현재 주문 완료 처리 후 다음 주문으로 자동 진행"""
        self._order_advancing = True
        self.send_mqtt_order_complete()
        if self.total_orders > 0 and self.current_order_number >= self.total_orders:
            self.send_mqtt_all_orders_complete()
            self.show_all_done_dialog()
        else:
            self.move_to_next_order()

    def move_to_next_order(self):
        self._order_advancing = False
        self.current_order_number += 1
        self.status_label.text = f'사용자{self.selected_user_id}  |  주문 {self.current_order_number}번'
        self.clear_grid()
        self.start_work(None)

    def clear_grid(self):
        for cell in self.work_cells:
            cell.clear()

    # ── 다이얼로그 ───────────────────────────────────────────────────

    def _fix_dialog_fonts(self, dialog):
        try:
            for widget in dialog.walk():
                if hasattr(widget, 'font_name'):
                    widget.font_name = FONT_NAME
        except Exception:
            pass

    def show_all_done_dialog(self):
        self._hide_next_button()
        self._unlock_all_users()
        self.clear_grid()
        self.grid_label.text = '모든 주문 완료!'
        self.status_label.text = f'사용자{self.selected_user_id}  |  작업 완료'

        def open_dialog():
            d = MDDialog(
                title='작업 완료',
                text=f'모든 주문이 완료되었습니다!\n(총 {self.total_orders}건)',
                buttons=[MDFlatButton(text='확인', font_name=FONT_NAME)],
            )
            d.buttons[0].bind(on_release=lambda *a: d.dismiss())
            d.open()
            Clock.schedule_once(lambda dt: self._fix_dialog_fonts(d), 0.1)

        open_dialog()

    def show_error_dialog(self, message, title='오류'):
        d = MDDialog(
            title=title,
            text=message,
            buttons=[MDFlatButton(text='확인', font_name=FONT_NAME)],
        )
        d.buttons[0].bind(on_release=lambda *a: d.dismiss())
        d.open()
        Clock.schedule_once(lambda dt: self._fix_dialog_fonts(d), 0.1)


class WarehouseApp(MDApp):
    def build(self):
        self.title = '물류 창고 관리 시스템'
        self.theme_cls.primary_palette = 'Blue'
        self.theme_cls.theme_style = 'Light'
        return WarehouseGUI()

    def on_stop(self):
        gui = self.root
        if gui.mqtt_client:
            gui.mqtt_client.loop_stop()
            gui.mqtt_client.disconnect()


if __name__ == '__main__':
    WarehouseApp().run()

# 📱 창고 피킹 GUI - 라즈베리파이 최종 가이드

## 🎯 프로젝트 개요

라즈베리파이 터치스크린을 사용한 창고 피킹 작업 관리 시스템

---

## 📦 1. 시스템 구성

```
┌─────────────────────┐         ┌─────────────────────┐
│ 라즈베리파이 user1   │         │ 라즈베리파이 user2   │
│  - 800x480 터치     │         │  - 800x480 터치     │
│  - Kivy GUI         │         │  - Kivy GUI         │
└──────────┬──────────┘         └──────────┬──────────┘
           │                               │
           │         WiFi 네트워크          │
           │                               │
           └───────────┬───────────────────┘
                       │
            ┌──────────▼──────────┐
            │   노트북 서버        │
            │  - MQTT 브로커      │
            │  - HTTP API         │
            │  - SQLite DB        │
            │  - 재고 관리         │
            └─────────────────────┘
```

---

## 🔧 2. 초기 설치 (한 번만)

### Step 1: 필수 라이브러리 설치

**라즈베리파이가 인터넷에 연결된 상태에서:**

```bash
# 시스템 업데이트
sudo apt update

# Kivy 및 필요한 라이브러리
sudo apt install python3-kivy python3-paho-mqtt python3-requests -y
```

**설치 시간: 약 3-5분**

### Step 2: 프로젝트 폴더 생성

```bash
# 홈 디렉토리에 폴더 생성
mkdir -p ~/agv_warehouse_system
cd ~/agv_warehouse_system
```

### Step 3: GUI 파일 복사

**USB 또는 네트워크로 `warehouse_gui_v2.py` 파일을 라즈베리파이에 복사**

```bash
# USB에서 복사하는 경우
cp /media/user1/USB이름/warehouse_gui_v2.py ~/agv_warehouse_system/

# 또는 scp로 복사 (노트북에서 실행)
scp warehouse_gui_v2.py user1@라즈베리파이IP:~/agv_warehouse_system/
```

---

## ⚙️ 3. WiFi 변경 시 설정

### 📶 WiFi가 바뀌었을 때 해야 할 일

#### 1) 노트북 IP 확인

**노트북에서:**
```bash
hostname -I
```

**예시 결과:**
```
172.30.1.72
```

#### 2) 라즈베리파이 GUI 파일 수정

**라즈베리파이에서:**
```bash
nano ~/agv_warehouse_system/warehouse_gui_v2.py
```

**21번째 줄 수정:**
```python
# 설정
SERVER_IP = '172.30.1.72'  # ← 노트북 IP로 변경!
MQTT_PORT = 1883
HTTP_PORT = 5000
```

**저장: Ctrl+O, Enter, Ctrl+X**

#### 3) 자동 수정 명령어 (선택사항)

```bash
# 기존 IP를 새 IP로 자동 변경
sed -i "s/SERVER_IP = '.*'/SERVER_IP = '172.30.1.72'/" ~/agv_warehouse_system/warehouse_gui_v2.py
```

#### 4) 연결 테스트

```bash
# 노트북 서버에 ping 테스트
ping -c 3 172.30.1.72

# HTTP API 테스트
curl http://172.30.1.72:5000/health
```

**성공 시:**
```json
{"status":"ok","service":"warehouse_server"}
```

---

## 🚀 4. 실행 방법

### 방법 1: 터미널에서 직접 실행

**라즈베리파이 터치 디스플레이에서 터미널 열고:**

```bash
cd ~/agv_warehouse_system
python3 warehouse_gui_v2.py
```

**또는 SSH로 실행:**
```bash
# 노트북에서 SSH 접속
ssh user1@라즈베리파이IP

# 환경변수 설정하고 실행
export DISPLAY=:0
python3 ~/agv_warehouse_system/warehouse_gui_v2.py
```

### 방법 2: 바탕화면 아이콘으로 실행

**실행 스크립트 생성:**
```bash
cat > ~/Desktop/warehouse.sh << 'EOF'
#!/bin/bash
cd ~/agv_warehouse_system
python3 warehouse_gui_v2.py
EOF

chmod +x ~/Desktop/warehouse.sh
```

**바탕화면에 아이콘이 생기고, 더블클릭하면 실행됩니다!**

### 방법 3: 자동 실행 (부팅 시)

```bash
# autostart 폴더 생성
mkdir -p ~/.config/autostart

# 자동 실행 파일 생성
cat > ~/.config/autostart/warehouse.desktop << 'EOF'
[Desktop Entry]
Type=Application
Name=Warehouse GUI
Exec=python3 /home/user1/agv_warehouse_system/warehouse_gui_v2.py
X-GNOME-Autostart-enabled=true
EOF
```

**다음 부팅부터 자동으로 GUI가 실행됩니다!**

---

## 📱 5. 사용 방법

### 5.1 사용자 선택

1. 화면 상단 왼쪽 **"사용자1"** 또는 **"사용자2"** 버튼 터치
2. 중앙에 선택된 사용자 & 주문번호 표시
3. **"작업시작"** 버튼 활성화

### 5.2 작업 시작

1. **"작업시작"** 버튼 터치
2. MQTT로 서버에 `start_order` 전송 (재고 즉시 차감)
3. HTTP로 피킹 리스트 자동 요청
4. 2x4 그리드에 피킹 위치 표시

**그리드 표시 예시:**
```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│  1-1-1       │  2-1-2       │  2-3-2       │  3-1-2       │
│  드롭스      │  퍼지        │  구미        │  무설탕캔디  │
│  (3개)       │  (2개)       │  (4개)       │  (5개)       │
└──────────────┴──────────────┴──────────────┴──────────────┘
│  (비어있음)  │  (비어있음)  │  (비어있음)  │  (비어있음)  │
└──────────────┴──────────────┴──────────────┴──────────────┘
```

### 5.3 피킹 작업

1. **첫 번째 셀의 선반으로 이동** (예: 1-1-1)
2. 물건과 수량 확인
3. **셀 터치** → 빨간색으로 변경
4. **같은 선반의 다른 층도 완료하면 MQTT 전송**
   - 예: 1-1-1, 1-1-2 모두 터치 → "1-1 선반 완료" 전송
5. 다음 선반으로 이동

### 5.4 주문 완료

1. 모든 셀이 빨간색이 되면
2. **"주문 완료"** 버튼 터치
3. MQTT로 서버에 `order_complete` 전송
4. **자동으로 다음 주문번호로 이동** (주문2번)
5. 반복...

**미완료 셀이 있으면:**
- 노란색으로 5번 깜빡임 (경고)
- 주문 완료 불가

---

## 🔄 6. 작업 흐름

```
[1] 사용자 선택
     ↓
[2] 작업시작 버튼
     ↓
[3] MQTT: start_order 전송
     서버: 재고 즉시 차감 ⭐
     ↓
[4] HTTP: 피킹 리스트 요청
     서버: JSON 응답
     ↓
[5] 그리드에 표시
     ↓
[6] 선반 이동 & 셀 터치 (빨간색)
     같은 선반 전체 완료 시
     MQTT: shelf_complete 전송
     ↓
[7] 모든 선반 완료
     ↓
[8] 주문 완료 버튼
     MQTT: order_complete 전송
     ↓
[9] 다음 주문으로 자동 이동
     ↓
[반복]
```

---

## 📡 7. 통신 규격

### 7.1 MQTT 메시지 (라즈베리파이 → 서버)

#### 주문 시작
```json
토픽: warehouse/order/start
{
  "type": "start_order",
  "사용자ID": 1,
  "주문번호": 1
}
```

#### 선반 완료 (같은 선반의 모든 층 완료 시)
```json
토픽: warehouse/shelf/complete
{
  "type": "shelf_complete",
  "사용자ID": 1,
  "선반번호": "1-1"
}
```
**주의:** `1-1-1`, `1-1-2`, `1-1-3` 모두 완료해야 `1-1` 전송!

#### 주문 완료
```json
토픽: warehouse/order/complete
{
  "type": "order_complete",
  "사용자ID": 1,
  "주문번호": 1
}
```

### 7.2 HTTP API (라즈베리파이 → 서버)

#### 피킹 리스트 요청
```
GET http://서버IP:5000/api/picking/user/1/order/1
```

**응답:**
```json
{
  "status": "success",
  "사용자ID": 1,
  "주문번호": 1,
  "피킹리스트": [
    {
      "선반번호": "1-1-1",
      "물건": "드롭스",
      "개수": 3
    }
  ]
}
```

---

## 🐛 8. 발견된 버그 및 수정 이력 (2026-03-06)

협업자에게 전달할 수정 내용:

| # | 문제 | 원인 | 수정 |
|---|------|------|------|
| 1 | 폴더 이름 `warehouse_gui&server` | `&`가 쉘 특수문자라 터미널에서 매번 따옴표 필요 | `warehouse_gui_server`로 변경 (git mv) |
| 2 | 엑셀 파일 경로 불일치 | 파일은 `webots_simulation/Database/`에 있는데 코드는 같은 폴더에서 찾음 | `excel_to_sqlite.py`, `warehouse_server.py` 경로 수정 |
| 3 | 파일명 불일치 | `데이터 베이스.xlsx`(띄어쓰기) vs 코드에서 `데이터_베이스.xlsx`(언더스코어) | 경로 수정 시 실제 파일명으로 맞춤 |
| 4 | `SERVER_IP` 하드코딩 | 핫스팟 연결마다 IP가 바뀌는데 코드에 고정값 | 시작 시 IP 입력 팝업 추가, `config.json`에 저장 ✅ |
| 5 | 작업시작 버튼 중복 클릭 방지 없음 | 빠르게 두 번 누르면 `start_order` 두 번 전송 → AGV 2대 동시 배정 | 버튼 누르면 즉시 비활성화 ✅ |
| 6 | AGV 도착 전 셀 클릭 가능 | 피킹 리스트 뜨자마자 셀 클릭 가능 → 서버 WAIT_PICKING 에러 | AGV가 WS 도착 후 `warehouse/agv/at_ws` 수신 시 셀 활성화 ✅ |
| 7 | 도착하지 않은 선반 셀 클릭 가능 | AGV가 선반A를 가져왔을 때 선반B 셀도 클릭 가능 → 잘못된 shelf_complete 전송 | 미수정 🔲 — 아래 수정 방법 참고 |
| 8 | 아무 셀이나 눌러도 shelf_complete 처리됨 | `shelf_complete` 메시지에 선반ID 없이 사용자ID만 전송 → 서버가 WS의 AT_WORKSTATION 선반을 자동 탐색하여 무조건 처리 | 미수정 🔲 — 버그 7번과 동일 원인, 아래 수정 방법 참고 |

### 추가된 MQTT 토픽
| 토픽 | 방향 | 설명 |
|------|------|------|
| `warehouse/agv/at_ws` | 서버 → 라파 | AGV가 작업대 도착, 피킹 가능 알림 (`{"사용자ID": 1, "선반번호": "1-1"}`) |

### 🔲 버그 7·8 수정 방법 (미완료)

**문제 요약**: 어떤 셀을 눌러도 해당 WS의 AT_WORKSTATION 선반이 자동으로 완료 처리됨

**수정 내용**:

1. **`warehouse_gui.py`**: `enable_cells(shelf_label)` — `warehouse/agv/at_ws` 수신 시 `선반번호`에 해당하는 셀만 활성화, 나머지는 계속 비활성 유지
   - 현재: AGV 도착 시 모든 셀 활성화 → 잘못된 선반 셀 클릭 가능
   - 수정: `shelf_label`로 필터링해서 해당 선반 셀만 활성화

2. **`warehouse/shelf/complete` 메시지에 선반번호 포함** (선택적 강화):
   ```json
   {
     "type": "shelf_complete",
     "사용자ID": 1,
     "선반번호": "1-1"
   }
   ```
   서버(`request_handler.py` `_handle_shelf_complete`)에서 선반번호 검증 추가

---

## ⚠️ 9. 문제 해결

### 문제 1: MQTT 연결 실패

**증상:**
```
❌ MQTT 연결 실패: timed out
```

**해결:**
1. 노트북 서버가 실행 중인지 확인
2. SERVER_IP가 올바른지 확인 (21번째 줄)
3. ping 테스트: `ping 서버IP`
4. 방화벽 확인

### 문제 2: 피킹 리스트를 받지 못함

**증상:**
```
서버 연결 실패: 172.30.1.72:5000
```

**해결:**
1. 노트북 서버 실행 확인
2. HTTP API 테스트: `curl http://서버IP:5000/health`
3. SERVER_IP 확인

### 문제 3: 한글이 깨짐

**증상:**
- 한글이 네모 박스로 표시

**해결:**
```bash
# 한글 폰트 설치
sudo apt install fonts-nanum fonts-nanum-coding
```

### 문제 4: 터치가 안 됨

**해결:**
```bash
# 터치스크린 보정
sudo apt install xinput-calibrator
xinput_calibrator
```

### 문제 5: 화면이 짤림

**해결:**
- `warehouse_gui_v2.py` 파일에서 화면 크기 조정
- 17-18번째 줄:
```python
Window.size = (800, 450)  # 높이 조정
Window.top = 30  # 위치 조정
```

---

## 🔍 9. 로그 확인

### 실행 로그 보기

**터미널에 다음과 같은 로그가 출력됩니다:**

```
[INFO] Kivy v2.3.1
[INFO] Python v3.13.5
✅ MQTT 연결: 172.30.1.72:1883 (ID: raspberrypi_gui_1234)
[INFO] Base application main loop

🚀 작업 시작: 사용자1, 주문1
✅ MQTT 전송: start_order
✅ 피킹 리스트 수신: 4개 항목
📦 셀 완료: 1-1-1
   → 선반 1-1 아직 미완료 셀 있음
📦 셀 완료: 1-1-2
✅ 선반 1-1 전체 완료!
✅ MQTT 전송: shelf_complete - 1-1
```

### Kivy 로그 파일 위치

```bash
# 로그 파일 보기
cat ~/.kivy/logs/kivy_*.txt
```

---

## 🎨 10. 화면 레이아웃

```
┌────────────────────────────────────────────────────────┐
│ [사용자1] [사용자2]  │ 사용자1 주문1번 │  [작업시작] │  ← 상단 (20%)
├────────────────────────────────────────────────────────┤
│                    작업 그리드                          │
│  ┌──────┬──────┬──────┬──────┐                        │
│  │ 1-1-1│ 2-1-2│ ...  │ ...  │                        │  ← 중앙 (65%)
│  │드롭스│ 퍼지 │      │      │                        │
│  │(3개) │(2개) │      │      │                        │
│  ├──────┼──────┼──────┼──────┤                        │
│  │      │      │      │      │                        │
│  └──────┴──────┴──────┴──────┘                        │
├────────────────────────────────────────────────────────┤
│              [주문 완료]                                │  ← 하단 (15%)
└────────────────────────────────────────────────────────┘
```

---

## 📋 11. WiFi 변경 체크리스트

WiFi 변경 시 순서대로 진행:

- [ ] **Step 1**: 노트북 새 IP 확인 (`hostname -I`)
- [ ] **Step 2**: 노트북 서버 재시작
- [ ] **Step 3**: 라즈베리파이 user1 - `warehouse_gui_v2.py` 21번째 줄 수정
- [ ] **Step 4**: 라즈베리파이 user2 - `warehouse_gui_v2.py` 21번째 줄 수정
- [ ] **Step 5**: ping 테스트 (`ping 새IP`)
- [ ] **Step 6**: HTTP 테스트 (`curl http://새IP:5000/health`)
- [ ] **Step 7**: GUI 재시작 (user1, user2)
- [ ] **Step 8**: 작업시작 테스트

---

## 💡 12. 팁 & 주의사항

### ✅ 운영 팁

1. **서버 먼저 실행**
   - 라즈베리파이 GUI 실행 전에 노트북 서버가 실행되어 있어야 함

2. **WiFi 안정성**
   - 같은 WiFi 네트워크에 연결
   - 신호 강도 확인

3. **재고 확인**
   - 주문 시작 전에 관리자 GUI에서 재고 확인
   - 재고 부족 시 초기화 버튼 사용

4. **동시 작업**
   - user1과 user2가 동시에 작업 가능
   - 재고는 서버에서 실시간 차감

### ⚠️ 주의사항

1. **IP 주소 변경**
   - WiFi 바뀌면 반드시 SERVER_IP 수정!

2. **client_id 충돌**
   - 같은 파일 사용해도 자동으로 고유 ID 생성됨
   - `raspberrypi_gui_1234`, `raspberrypi_gui_5678` 등

3. **선반 완료 조건**
   - 같은 선반(구역-열)의 모든 층 완료해야 MQTT 전송
   - 예: 1-1-1, 1-1-2, 1-1-3 모두 완료 → 1-1 전송

4. **터치 정확도**
   - 셀을 정확히 터치해야 함
   - 잘못 터치하면 다시 터치해서 취소 가능

---

## 🔧 13. 고급 설정

### 화면 크기 조정

```python
# warehouse_gui_v2.py 17-20번째 줄
Window.size = (800, 465)  # 폭, 높이
Window.borderless = True  # 테두리 제거
Window.fullscreen = False
Window.top = 15  # 상단 여백
```

### 자동 숨김 작업표시줄

```bash
nano ~/.config/lxpanel/LXDE-pi/panels/panel
```

**수정:**
```ini
autohide=1  # 0을 1로 변경
```

### 화면 절전 비활성화

```bash
sudo nano /etc/lightdm/lightdm.conf
```

**[Seat:*] 섹션에 추가:**
```ini
xserver-command=X -s 0 -dpms
```

---

## 📞 14. 지원

### 문제 발생 시:

1. **로그 확인**: 터미널 출력 메시지
2. **서버 로그**: 노트북 서버 터미널
3. **네트워크**: ping, curl 테스트
4. **재시작**: GUI, 서버, 라즈베리파이

### 자주 묻는 질문

**Q: 주문이 없다고 나옵니다.**
A: 노트북 서버에 `사용자1주문.xlsx` 파일이 있는지 확인

**Q: 재고가 부족하다고 나옵니다.**
A: 관리자 GUI에서 "재고 초기화(20개)" 버튼 클릭

**Q: 두 라즈베리파이가 서로 끊어집니다.**
A: 최신 `warehouse_gui_v2.py` 파일 사용 (고유 client_id 자동 생성)

**Q: 작업 완료 후 다음 주문이 자동으로 안 넘어갑니다.**
A: 주문번호 2, 3이 엑셀 파일에 있는지 확인

---

## 🎉 완료!

**이제 라즈베리파이 피킹 시스템을 사용할 준비가 되었습니다!**

**관련 문서:**
- `README_SERVER_v2.md` - 노트북 서버 가이드
- `README.md` - 전체 시스템 가이드

---

**버전:** v2.1
**최종 업데이트:** 2026-03-06
**작성자:** AGV Warehouse System Team
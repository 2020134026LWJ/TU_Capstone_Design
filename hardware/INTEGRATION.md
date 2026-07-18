# AGV 실물 통합 (HIL bring-up) — 명령어

위에서부터 순서대로. 각 명령 앞의 **[기기]** = 어디서 실행하는지. 프로토콜/환경 상세는 [`README.md`](README.md).

## 기기 & SSH 접속

| 기기 | 정체 | 접속 (PC에서) |
|---|---|---|
| **[PC]** | 이 노트북 — 서버·트윈·주문 발행 | (그냥 PC 터미널. 여러 개 띄움) |
| **[AGV 라파]** | 실물 AGV의 라즈베리파이 (현재 1대) | `ssh agv1@agv1-RPi.local` |
| **[GUI 라파]** | 작업대 터치스크린 | `ssh user1@raspberrypi.local` |

- SSH 키 등록돼서 **비번 없음**. `.local`이 안 풀리면 핫스팟(SSID `LWJ`) 접속기기 목록에서 IP 확인.
- 라파에서 서버 주소는 `hardware/config.py: MQTT_HOST="UB-Region5.local"` (mDNS, IP 바뀌어도 됨).

---

## 0. 시작 전 (실물로만 확인 — 3단계에서)

- `turn_left` → 실제 좌회전인지 (반대면 STM 매핑 `5`(left)↔`6`(right) 뒤집기)
- AGV를 홈에 **북향(heading 0)**으로: AGV-1 = **8**(W2), AGV-2 = **32**(W1)
- ⛔ 카메라 왜곡보정 수식 건드리지 말 것 (STM이 그 값 기준 튜닝됨)

---

## 1. 라파 준비 (AGV 라파에서)

```bash
# [PC] AGV 라파 접속
ssh agv1@agv1-RPi.local
```
```bash
# [AGV 라파] 자기 번호 박기 (라파마다 다르게 — 없으면 rpi_main 에러)
echo 'export AGV_ID=1' >> ~/.bashrc   # AGV_ID = 이 라파의 로봇 번호 (2번 라파는 =2)
source ~/.bashrc

# [AGV 라파] UART 켜기 — 한 번만. STM은 40핀 헤더 핀 8/10에 연결됨 → /dev/ttyAMA0 필요.
#   ⚠️ Ubuntu 라파는 기본으로 헤더 UART가 꺼져 있어, config.txt에 enable_uart=1 을 넣어야
#   /dev/ttyAMA0 이 생긴다. (안 넣으면 ttyAMA10=전용 디버그 커넥터만 있어 STM과 통신 안 됨)
grep -q "enable_uart=1" /boot/firmware/config.txt || echo 'enable_uart=1' | sudo tee -a /boot/firmware/config.txt
sudo usermod -aG dialout $USER     # 시리얼 포트 열기 권한 (없으면 Permission denied)
sudo reboot                         # 재부팅해야 /dev/ttyAMA0 생기고 그룹 적용 → 재접속 후 계속
#   [확인] 재접속 후:  ls /dev/ttyAMA*  → /dev/ttyAMA0 보여야 함 (config.py UART_PORT=/dev/ttyAMA0 와 일치)

# [AGV 라파] 카메라 살아있나
python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"
#   → [{'Model':'ov5647',...}] 나오면 OK

# [AGV 라파] 초점 맞추기 (ov5647 수동초점 — 반드시)
python3 -m hardware.camera_preview     # PC 브라우저 http://<라파IP>:8000 보며 sharpness 최대로 렌즈 돌림
#   [주의] 카메라 독점 → rpi_main과 동시 실행 불가
```

---

## 2. STM 단독 — turn 방향 (서버 없이 제일 먼저)

```bash
# [AGV 라파] bridge+camera 기동
python3 -m hardware.rpi_main 1         # 인자 1 = AGV 번호 (생략 시 env AGV_ID). --preview = PC 브라우저(:8000)로 화면 관찰
```
```bash
# [PC] 명령 직접 발행
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"turn_left"}'
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"forward","target_node":9}'  # target_node = 도착 예정 노드
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"lift_up"}'

# [PC] AGV가 주고받는 모든 메시지 보기
mosquitto_sub -h UB-Region5.local -t '/agv/#' -v      # -v = 토픽명도 표시
```
성공: `turn_left`→좌회전(반시계) / 90°·1칸 정확 / turn·lift 후 `/agv/cmd_ack` 도착 (forward 완료는 마커 보고로 대신, cmd_ack 안 옴이 정상)

---

## 3. 전체 기동 (기기별로, 각각 다른 터미널)

```bash
# [PC] 터미널 1 — MQTT 브로커 (보통 부팅 시 자동, 이미 떠 있으면 통과)
sudo systemctl start mosquitto

# [PC] 터미널 2 — DB 초기화 (이어할 땐 생략)
./warehouse_gui_server/reset_progress.sh

# [PC] 터미널 3 — 협업자 서버 (반드시 이 폴더 안에서)
cd warehouse_gui_server && python3 warehouse_server_v2.py

# [PC] 터미널 4 — AGV 서버 (터미널3과 순서 무관)
python3 -m server.main
```
```bash
# [AGV 라파] — AGV 기동 (ssh 세션에서)
python3 -m hardware.rpi_main           # env AGV_ID 사용. --preview 옵션 가능
```
```bash
# [GUI 라파] — 작업대 GUI (ssh 세션에서)
ssh user1@raspberrypi.local
cd ~/Desktop/TU_Capstone_Design/warehouse_gui_server && python3 warehouse_gui_ws1.py
#   1번 라파=ws1 / 2번 라파=ws2 (warehouse_gui_v2.py는 작업대2 사본=함정, 열지 말 것)
```
```bash
# [PC] 터미널 5 — Isaac 트윈 (4단계 옵션 참고)
./isaac_simulation/run_twin.sh 3.0
```
- **순서 규칙**: 브로커 먼저. 협업자 서버(터미널3)·AGV 서버(터미널4) 둘 다 **주문 GUI보다 먼저**.
- `DEMO_MODE=False` 확인(`server/core/request_handler.py:43`). 끌 땐 **역순** (AGV 라파 먼저 내림).

---

## 4. Isaac 트윈 (실물과 병행, 필수)

```bash
# [PC]
./isaac_simulation/run_twin.sh 3.0
#   3.0 = 1칸 주행 추정 초 (생략하면 3.0, 실측 1회면 자동 대체). run_twin.sh가 TWIN=1 을 자동 설정
#   ⚠️ TWIN=1 없이 step7 직접 실행 = Isaac이 '트윈'이 아니라 'AGV 본인'이 되어 마커 직접 발행 → 실물과 동시에 켜면 상태 꼬임

# [PC] 트윈 회전이 이상할 때만: 옛 델타 방식 (기본=절대값 실시간 추종)
TWIN_ABS_HEADING=0 ./isaac_simulation/run_twin.sh 3.0
```

---

## 5. 1대 주행 (지금 단계)

3단계에서 라파 **1대만** 켠다.

```bash
# [PC] 서버 로그에서 확인: presence online rid=1  (안 뜨면 태스크 배정 안 됨)

# [PC] GUI 없이 빠르게 주문 한 건 넣기 (또는 GUI 라파에서 터치)
mosquitto_pub -h localhost -t warehouse/order/start -m '{"사용자ID":1,"주문번호":1,"작업대":2}'
```
- 한 바퀴: 주문 → 배달 → 셀 파란불 → 피킹완료 터치 → 반납. 여기까지 = **통합 성공**

---

## 6. 로그 병합 (통신 타이밍 디버깅)

```bash
# [PC] 두 콘솔을 화면+파일 동시 기록 후 시각순 병합
python3 -m server.main                    2>&1 | tee server.log
TWIN=1 ./isaac_simulation/run_twin.sh 3.0 2>&1 | tee twin.log
cat server.log twin.log | grep -E '^\[[0-9]{2}:[0-9]{2}:' | sort   # 타임스탬프 줄만 시각순 정렬
```
- 서버 발행→트윈 실행 지연 수십~수백ms=정상, 초 단위=브로커/페이싱 의심. **한 PC에서 돌릴 때만** 시각 정확(시계 공유).

---

## 7. HEADING_OFFSET (마지막, 밸브)

```
# 측정: AGV 홈에 북향으로 → [PC] 서버 로그의 yaw 읽기 → HEADING_OFFSET = (0 - yaw) % 360
#       → hardware/config.py 의 HEADING_OFFSET 에 기입 (현재 0)
# 밸브: 주행 중 로그가 계속 '[heading] ... 차이 0°' 면 → server/config.py: TRUST_CAMERA_HEADING = True
```
- 밸브 열면 0단계 "북향" 제약 사라짐. (yaw 규약: 시계방향=yaw 증가, 서버 heading과 같은 부호)

---

## 8. 2대 동시 (1대 완주 후, AGV 라파 2대일 때)

```bash
# [AGV 라파 각각] 자기 번호 확인 (둘 다 1이면 명령 뺏김)
echo $AGV_ID
```
- 홈: AGV-1=**8**, AGV-2=**32** (교차 배치, 바꾸지 말 것)
- **정상 동작**: 선반 내렸다 돌고 다시 들기(수정 82/85) / 잠깐 멈춰 상대 대기(수정 84) = 충돌 회피, 고장 아님
- ⚠️ **미해결 [③] 회랑 정면 교착**: 왼쪽 1차선(0-8-16-24-32-40) 마주침 시 간헐적 교착 (2대 본격 운용 전 수정 예정, 1대엔 무관). 상세 `FLOWCHART.md` "알려진 이슈"

---

## 증상별 원인 (빠른 참조)

| 증상 | 먼저 의심 |
|---|---|
| 순간이동 / 엉뚱한 노드 | 마커 낱장 아님 → 초점 |
| `turn_left`인데 우회전 | handedness → STM 매핑 5↔6 |
| 서버가 태스크 안 줌 | presence online 미발행 → MQTT 연결 |
| GUI 주문 반응 없음 | `server.main` 미기동 |
| GUI 재고·진행 안 뜸 | `warehouse_server_v2.py` 미기동 |
| turn/lift 후 다음 명령 안 나감 | `cmd_ack` 미도착 → UART `0x81` |
| forward 후 멈춤 | 마커 못 봄 → 초점/마커 위치 |
| `.local` 접속 안 됨 | 핫스팟(LWJ) 미접속 → 기기목록서 IP 직접 |

---

## 실물 없이 미리 (전부 [PC])

```bash
pytest                                                              # 알고리즘 회귀 100
python3 -m virtual_test.software_in_the_loop.run_sil               # SIL (가짜 STM + pty 가상 UART)
python3 -m virtual_test.bench_camera.run_bench 1                   # 벤치: 카메라+손 마커. 인자 1=AGV 번호
python3 -m virtual_test.bench_camera.run_bench 1 --no-camera --auto-walk 8
#   --no-camera = 카메라 없이 PC 단독 / --auto-walk N = 가짜 로봇이 노드 N(8=AGV-1 홈)에서 시작해 명령대로 자동 주행
```
⚠️ 어느 것도 turn handedness는 검증 못 함 — 실물 전용.

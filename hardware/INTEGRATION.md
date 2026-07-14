# AGV 실물 통합 가이드 (HIL bring-up)

**실물 AGV를 처음 붙이는 날, 위에서부터 순서대로 따라가는 절차서.**
프로토콜·환경구축 상세는 [`README.md`](README.md). 여기는 **순서와 성공 판정**만.

> 순서를 지킬 것. 초점이 안 맞으면 마커가 헛것을 보고, 마커가 틀리면 heading 장부가 틀어지고,
> 그러면 **회전 방향이 틀린 건지 장부가 틀린 건지 구분이 안 된다.**

**빠른 참조**
| 단계 | 내용 |
|---|---|
| 0 | 시작 전 주의 3가지 |
| 1~2 | 바닥 마커 배치 / 라파 준비 + **카메라 초점** |
| 3 | STM 단독 — **turn 방향** 확인 (서버 없이) |
| **4** | **전체 기동 순서** (브로커 → GUI 백엔드 → AGV 서버 → 라파 → GUI) |
| 5~6 | 1대 단독 주행 + GUI 한 바퀴 / **Isaac 트윈(필수)** |
| 7~8 | HEADING_OFFSET 실측 → 밸브 / 2대 동시 |

---

## 0. 시작 전 — 알고 있어야 할 3가지

### ① turn 좌우 방향(handedness)은 벤치로 검증이 **구조적으로 불가능**하다
벤치의 가짜 로봇은 서버와 **같은 회전 규약을 공유**한다. 즉 서버가 `turn_left`를 내면
가짜 로봇은 정의상 왼쪽으로 돈다 — 실물이 어느 쪽으로 도는지는 **아무것도 말해주지 않는다.**
그래서 이건 실물에서만 확인된다. 차 띄워놓고 한 번 쏴보면 5분이고, 반대면 부호 하나 뒤집으면 된다.
→ **3단계**에서 제일 먼저 한다.

### ② AGV는 홈 노드에 **북향(heading 0)으로** 놓아야 한다
서버와 트윈 **둘 다** 초기 heading을 0으로 가정한다. 홈: **AGV-1 = 8번(W2), AGV-2 = 32번(W1)**.
(7단계에서 `TRUST_CAMERA_HEADING` 밸브를 켜면 이 제약이 사라진다.)

### ③ [금지] 카메라 왜곡보정 수식은 **절대 고치지 말 것**
`camera.py`의 `cv2.undistort` 경로는 이중보정처럼 보이지만, **주원이 STM이 그 값을 기준으로
직진·회전을 이미 튜닝**해놨다. 고치면 튜닝이 전부 깨진다.
속도만 올리고 싶으면 `initUndistortRectifyMap` + `remap`으로 바꿔라 — **출력값은 동일**하고
15fps → 26fps가 된다. 수식 자체는 그대로 두는 것이 요점이다.

---

## 1. 바닥 마커 깔기

- `marker_id == node_id` — ArUco ID를 **노드 번호와 1:1**로 인쇄해 해당 칸에 붙인다 (노드 **0~47**).
- **[주의] 전부 같은 방향으로 깔 것.** 한 장이라도 돌아가면 그 노드에서만 heading이 틀어진다
  (7단계 `HEADING_OFFSET`이 전역 상수 하나라는 전제가 깨진다).
- **[주의] 마커 시트는 낱장으로 잘라서** 붙인다. 한 장에 15~20개가 인쇄돼 있으면 카메라가
  9번을 보는데 옆칸 10번까지 시야에 들어와 **로봇이 순간이동**한다. (이게 "유령 마커"의 진짜
  원인이었다 — 오검출이 아니라 시트 한 장에 여러 개였던 것.)
- 마커는 **이미 0~47로 인쇄돼 있다** (소프트웨어를 이 실물 마커에 맞춰 0-based로 바꿨다 — 수정 76).
  낱장 재출력이 필요할 때만: `python3 -m hardware.make_marker_sheet 8 9 16 17 18`
  (검은 사각형 **25mm** = `camera.py`의 `marker_size`. 크기가 다르면 offset(mm)이 전부 스케일 어긋난다.)

> **여유가 되면 `DICT_6X6_50`으로 재인쇄**를 권한다. 현재 `DICT_4X4_250`은 4×4=16비트라 ID 간
> 패턴 차이가 작아 오검출이 잦은데, 우리는 ID가 48개만 필요하다. 250개짜리를 쓸 이유가 없다.
> (바꾸려면 `camera.py`의 `getPredefinedDictionary` + `make_marker_sheet.py` 양쪽을 같이.)

---

## 2. 라파 준비 (AGV 라파 2대 각각)

```bash
# a) 자기가 몇 번 AGV인지 — 라파마다 다르게
echo 'export AGV_ID=1' >> ~/.bashrc      # 2번 라파는 =2
# 없으면 rpi_main이 에러로 멈춘다 (기본 1 금지 — '둘 다 1' 충돌 방지)

# b) 서버 주소 확인: hardware/config.py 의 MQTT_HOST
#    현재 "UB-Region5.local" (mDNS — 핫스팟에서 IP 바뀌어도 자동 해석)
#    mDNS가 안 잡히면 IP 직접 기입

# c) 카메라 살아있나 (Pi5+Ubuntu는 libpisp 소스빌드 선행 — README 참조)
python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"
#   → [{'Model': 'ov5647', ...}]  나오면 OK.  빈 리스트면 PiSP 미탑재
```

### 카메라 초점 — 여기서 반드시 맞추고 간다
`ov5647`은 **수동 초점**이다. 흐린 영상 + ArUco는 **없는 마커를 지어낸다.**

```bash
python3 -m hardware.camera_preview      # 라파에서
# PC 브라우저: http://<라파IP>:8000
```

- **`sharpness`(라플라시안 분산)가 최대가 되는 지점**이 초점이다. 눈대중보다 정확하다.
  절대값이 아니라 "렌즈 경통을 돌렸을 때 최대가 되는 지점"을 찾는 것.
- **카메라 앞에 아무것도 없는데 ID가 뜨면 그게 오검출이다.** 초점을 더 맞춰라.
- `camera_preview`는 카메라를 독점한다 → `rpi_main`/`run_bench`와 **동시 실행 불가**.

---

## 3. STM 단독 확인 — turn 방향과 직진 거리 (제일 먼저)

**서버 없이** 명령 하나씩 쏴서 차가 어떻게 움직이는지 눈으로 본다.
여기서 규약이 맞는 걸 확인하기 전에 전체를 돌리면, 뒤에서 뭐가 틀렸는지 절대 못 가린다.

```bash
python3 -m hardware.rpi_main 1          # 라파에서 (bridge + camera 기동)

# PC에서 명령 직접 발행
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"turn_left"}'
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"forward","target_node":9}'
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"lift_up"}'
```

**확인 항목**

| 항목 | 성공 판정 | 실패 시 |
|---|---|---|
| **turn 방향** | `turn_left` → 차가 **실제로 좌회전** (반시계) | 반대로 돌면 부호/코드 매핑 하나 뒤집기 (STM: 5=left, 6=right) |
| **turn 각도** | 90° 돌고 멈춤 (180°는 `turn_180`) | STM 튜닝 |
| **직진 1칸** | 다음 노드 마커 위에 정지 | STM 튜닝 |
| **cmd_ack** | turn/lift 완료 시 `/agv/cmd_ack` 도착 | UART 수신 스레드 / `0x81` 확인 |
| **forward 완료** | **마커 보고로 대신함** — `cmd_ack` 안 옴이 **정상** | — |

```bash
# PC에서 AGV가 뭘 말하는지 다 들여다보기
mosquitto_sub -h UB-Region5.local -t '/agv/#' -v
```

---

## 4. 전체 기동 순서

**순서를 지킬 것.** 브로커가 먼저 떠야 나머지가 붙고, AGV 서버가 GUI보다 먼저 떠야 주문이 유실되지 않는다.

| # | 어디서 | 명령 | 확인 |
|---|---|---|---|
| 1 | **PC** | `sudo systemctl status mosquitto` (안 떠 있으면 `sudo systemctl start mosquitto`) | MQTT 브로커. 외부 접속 허용 = `/etc/mosquitto/conf.d/external.conf` |
| 2 | **PC** | `./warehouse_gui_server/reset_progress.sh` | 시연/테스트 시작 전 **DB 초기화** (xlsx에서 warehouse.db 재생성). 이어서 할 땐 생략 |
| 3 | **PC** | `cd warehouse_gui_server && python3 warehouse_server_v2.py` | GUI 백엔드 (HTTP :5000 + MQTT). GUI가 재고/진행상황을 여기서 읽는다. **[주의] 반드시 그 폴더 안에서** — `db_path='warehouse.db'`가 상대경로라 루트에서 띄우면 빈 DB를 새로 만든다 |
| 4 | **PC** | `cd TU_Capstone_Design && python3 -m server.main` | **AGV 서버** (경로계획·충돌회피) |
| 5 | **AGV 라파** ×2 | `python3 -m hardware.rpi_main` (AGV_ID는 .bashrc) | AGV 서버 로그에 `presence online rid=N` |
| 6 | **GUI 라파** ×2 | 1번 라파 `python3 warehouse_gui_ws1.py` / 2번 라파 `python3 warehouse_gui_ws2.py` | 터치스크린 (사용자는 화면에서 고른다 — 인자 없음). `.bashrc`에 `export DISPLAY=:0` 필요. **[주의] `warehouse_gui_v2.py`는 ws2와 동일한 사본**(`WORKSTATION_ID=2`) — 1번 라파에서 열면 작업대 2로 발행된다 |
| 7 | **PC** | `TWIN=1 ~/isaacsim/_build/linux-x86_64/release/python.sh /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py` | **Isaac 트윈** — 6단계. **절대경로 필수**. 실물과 **같이** 띄운다 |

- **AGV는 홈 노드에 북향으로 놓고** 켠다 (AGV-1=**8**(W2), AGV-2=**32**(W1)). 0-② 참조.
- **`DEMO_MODE` 확인** — `server/core/request_handler.py:43`이 `False`인지 (정상 동작).
  `True`면 WS 전담 + 스테이징 비활성이라 통합 검증이 안 된다.
- **[주의] step7은 `TWIN=1` 없이 열지 말 것.** 빼면 Isaac이 "자기가 AGV"라고 여기고 가짜 마커·cmd_ack를
  발행해 실물 AGV와 충돌한다. `TWIN=1`이어야 관찰자로 붙는다.
- 모든 머신의 서버 주소는 **`UB-Region5.local`** (mDNS). AGV 라파 = `hardware/config.py: MQTT_HOST`,
  GUI 라파 = `warehouse_gui_ws{1,2}.py: SERVER_IP`. 네트워크가 바뀌어도 그대로 동작한다.
- GUI 라파 SSH: `pi@172.30.1.44` / `user1@172.30.1.98`, AGV 라파: `ssh agv1@agv1-RPi.local`
- **끌 때는 역순.** AGV 라파를 먼저 내리면 브로커가 LWT로 `offline`을 대신 발행한다.

---

## 5. 1대 단독 주행 확인

2대를 동시에 붙이기 전에 **1대만** 켜서 끝까지 돌린다 (기동 순서 5단계에서 라파 하나만).

**서버 로그에서 볼 것**
- `presence online rid=1` — 수정 75. 이게 안 뜨면 서버는 그 로봇을 **없는 것으로 치고 태스크를 안 준다.**
  (반대로 로봇이 죽으면 브로커가 LWT로 `offline`을 대신 보낸다. 단, **A\*는 여전히 그 칸을 장애물로
  본다** — 끊긴 로봇의 몸은 그 자리에 그대로 있기 때문이다.)
- 마커 보고마다 `current_node` 갱신이 **인접 노드로만** 일어나는지. 순간이동하면 → 1단계 마커 배치
  (시트 낱장) 또는 초점 문제. (수정 62/64의 인접성 가드가 대부분 잡아서 로그로 경고를 띄운다.)
- `[heading] ... 차이 0°` — 7단계에서 쓸 값. 지금은 **로그만** 나오고 제어엔 안 쓰인다.

**GUI까지 한 바퀴**: GUI에서 주문 시작 → AGV가 선반 싣고 작업대 도착 → **GUI 셀에 파란불**
(`warehouse/shelf/arrived`) → 피킹 완료 터치 → AGV 선반 반납. 여기까지 돌면 통합 성공이다.

---

## 6. Isaac 트윈 병행 (필수)

실물이 움직일 때 시뮬 AGV가 따라 움직인다. **실물 주행은 트윈을 띄운 채로 한다.**
로그만 봐서는 안 보이는 오동작(heading 어긋남, 순간이동)을 화면에서 바로 잡아낸다 —
실제로 "유령 마커" 사고도 트윈이 `heading 방향 노드 없음`을 신고해서 발각됐다.

- 트윈은 `/agv/pose`(수정 68)를 구독한다 — **서버는 이 토픽을 안 본다** (연속 위치가 경로계획을
  흔들면 안 되므로). 트윈 전용 스트림이다.
- **회전은 실시간 추종, 직진은 보간.** 제자리 회전 중엔 발밑 마커가 계속 보여 yaw를 측정할 수
  있지만, 직진 중엔 마커가 시야를 벗어난다. 이 비대칭이 정상이다.
- 트윈이 `heading 방향에 노드 없음`을 신고하면 **실물 heading 장부가 어긋난 것** (뿌리는 대개 마커/초점).
- **페이싱**: 트윈은 1칸 소요시간을 실측해 따라간다(EMA, 기본 4.0초에서 시작). 실물 1칸 시간을
  이미 알면 넘겨라 — `TWIN=1 TWIN_EDGE_SECS=3.2 python.sh ...`

---

## 7. HEADING_OFFSET 실측 → 밸브 열기 (마지막)

지금 서버는 카메라 heading을 **믿지 않는다** (`server/config.py: TRUST_CAMERA_HEADING = False`).
경로 기반으로 스스로 계산하고, 카메라 값은 **비교해서 로그만** 찍는다. 배관은 이미 다 돼 있고
**남은 건 로그를 읽는 것뿐이다.**

**yaw 규약 (2026-07-12 실측 확정)**: 로봇이 **시계방향으로 돌면 yaw 증가**. 서버 heading
(0=N/90=E/180=S/270=W)도 시계방향이 + → **부호가 같다.** 그래서 덧셈이면 된다:
`heading = (yaw + HEADING_OFFSET) % 360`

**측정법**
1. AGV를 홈 노드에 **북쪽 보게** 놓는다.
2. 그때의 yaw를 읽는다 → `HEADING_OFFSET = (0 - yaw) % 360`
3. `hardware/config.py`의 `HEADING_OFFSET`에 기입.
   (현재 **0** — 주원이가 항상 0으로 맞추겠다고 한 **합의값**. 대개 그대로 맞을 것이다.)

**밸브 열기**: 실물 주행에서 서버 로그가 계속 `[heading] ... 차이 0°`로 나오면(=경고가 안 뜨면)
확인된 것이다 → `server/config.py`의 `TRUST_CAMERA_HEADING = True`.
그 순간부터 **"북향으로 놓아야 한다"는 제약(0-②)이 사라진다.**

---

## 8. 2대 동시

1대가 끝까지 돌고 나서 붙인다.

- 라파별 `AGV_ID`가 **다른지** 확인 (`echo $AGV_ID`). 둘 다 1이면 서로 명령을 뺏는다.
- 홈: AGV-1 = **8**(W2), AGV-2 = **32**(W1). **교착 회피를 위해 일부러 교차 배치**한 것이니 바꾸지 말 것.
- 볼 것: 회랑 진입 순서(STG), 트리거 마커 통과 시 대기 로봇 해제(TRG), 정면 교착 해소.
- 간헐적 명령 씹힘 → MQTT client_id 충돌 의심 (수정 63에서 `bridge_{rid}_{pid}_{uuid}`로 막았지만
  같은 증상이면 여기부터).

---

## 증상별 원인 지도

| 증상 | 먼저 의심할 것 |
|---|---|
| 로봇이 순간이동 / 엉뚱한 노드로 점프 | **마커 시트가 낱장이 아님** (옆칸 동시 검출) → 초점 → DICT |
| 아무것도 없는데 마커 검출 | **초점** (`camera_preview`의 sharpness 최대점) |
| `turn_left`인데 우회전 | handedness (3단계). STM 코드 매핑 5↔6 |
| 서버가 태스크를 안 줌 | presence online 미발행 (수정 75) — MQTT 연결 확인 |
| GUI에서 주문했는데 아무 일도 안 일어남 | AGV 서버(`server.main`) 미기동 / presence offline |
| GUI 재고·진행상황이 안 뜸 | GUI 백엔드(`warehouse_server_v2.py`, HTTP :5000) 미기동 |
| AGV는 도착했는데 GUI 셀에 파란불이 안 켜짐 | 중복 주문이면 **협업자 GUI 버그**(`activate_shelf_cells`가 현재 그리드만 켬). AGV 서버는 정상 |
| 명령이 간헐적으로 씹힘 | client_id 충돌 (수정 63) / UART DONE 유실 (STM이 3회 반복 송신하므로 거의 닫힘) |
| turn/lift 후 다음 명령 안 나감 | `cmd_ack` 미도착 → UART 수신(`0x81`) 확인 |
| forward 후 멈춰 있음 | 마커를 못 봄 (forward의 완료 신호 = 마커 보고). 초점/마커 위치 |
| 트윈이 `heading 방향 노드 없음` 신고 | heading 장부 어긋남 → 뿌리는 대개 마커/초점 |

---

## 실물 없이 미리 해볼 수 있는 것

| 하네스 | 무엇을 검증하나 | 실행 |
|---|---|---|
| **알고리즘** (pytest) | 서버 로직 회귀 (100 tests) | `pytest` (repo 루트) |
| **SIL** | 가짜 STM + 가상 UART(pty)로 bridge 명령-응답 | `python3 -m virtual_test.software_in_the_loop.run_sil` |
| **벤치(카메라)** | 실물 카메라 + 손으로 마커 카드 → 서버 알고리즘 전체 | `python3 -m virtual_test.bench_camera.run_bench 1` |
| **벤치(PC 단독)** | 카메라도 없이 가짜 로봇 자동 주행 | `... run_bench 1 --no-camera --auto-walk 8` |

> [주의] 이 중 **어느 것도 turn handedness를 검증하지 못한다** (0-① 참조). 그건 실물 전용이다.

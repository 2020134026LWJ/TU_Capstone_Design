# AGV 실물 통합 (HIL bring-up)

**위에서부터 순서대로.** 프로토콜/환경 상세는 [`README.md`](README.md). 여기는 순서·명령·성공 판정만.

| 단계 | 내용 |
|---|---|
| 0 | 시작 전 3가지 (읽고 시작) |
| 1 | 바닥 마커 배치 |
| 2 | 라파 준비 + 카메라 초점 |
| 3 | STM 단독 — **turn 방향** 확인 |
| 4 | 전체 기동 순서 |
| 5~6 | 1대 주행 + GUI / 트윈 |
| 7~8 | HEADING_OFFSET 밸브 / 2대 동시 |

---

## 0. 시작 전 3가지

1. **turn 좌우(handedness)는 벤치로 검증 불가** → 3단계에서 실물로 제일 먼저 확인. 반대면 STM 매핑(5=left/6=right) 부호 하나 뒤집기.
2. **AGV는 홈에 북향(heading 0)으로** 놓는다. 홈: AGV-1=**8**(W2), AGV-2=**32**(W1). (7단계 밸브 열면 제약 사라짐)
3. ⛔ **카메라 왜곡보정 수식 고치지 말 것** — 주원이 STM이 그 값 기준으로 튜닝됨. (속도만 올리려면 `undistort→remap`, 출력 동일 — 이미 적용됨)

---

## 1. 바닥 마커

- `marker_id == node_id`, 노드 **0~47**. **전부 같은 방향(북향)으로**, **낱장으로 잘라서** 붙인다.
- 검은 사각형 **15mm** (= `camera.py`의 `marker_size`). 크기 다르면 offset(mm) 스케일 어긋남.
- 낱장 재출력: `python3 -m hardware.make_marker_sheet 8 9 16 17 18`

---

## 2. 라파 준비 (AGV 라파 각각)

```bash
# a) AGV 번호 (라파마다 다르게 — 없으면 rpi_main 에러)
echo 'export AGV_ID=1' >> ~/.bashrc      # 2번 라파는 =2

# b) 카메라 살아있나 (Pi5+Ubuntu는 libpisp 소스빌드 선행 — README)
python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"
#   → [{'Model': 'ov5647', ...}] 나오면 OK

# c) 카메라 초점 (ov5647 수동초점 — 반드시 맞추고 간다)
python3 -m hardware.camera_preview        # PC 브라우저: http://<라파IP>:8000
#   sharpness 최대가 되도록 렌즈를 돌린다. 아무것도 없는데 ID 뜨면 초점 불량.
#   [주의] 카메라 독점 → rpi_main과 동시 실행 불가 (구동 중 화면은 5단계 --preview)
```

- 서버 주소: `hardware/config.py: MQTT_HOST = "UB-Region5.local"`, UART: `UART_PORT = "/dev/ttyAMA0"`.

---

## 3. STM 단독 — turn 방향 (서버 없이, 제일 먼저)

```bash
python3 -m hardware.rpi_main 1                       # 라파 (bridge + camera)
#   원격으로 화면 보며 하려면:  python3 -m hardware.rpi_main 1 --preview

# PC에서 명령 직접 발행
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"turn_left"}'
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"forward","target_node":9}'
mosquitto_pub -h UB-Region5.local -t /agv/cmd -m '{"rid":1,"cmd":"lift_up"}'

# AGV가 뭘 말하는지 다 보기
mosquitto_sub -h UB-Region5.local -t '/agv/#' -v
```

| 항목 | 성공 | 실패 시 |
|---|---|---|
| **turn 방향** | `turn_left` → 실제 좌회전(반시계) | STM 매핑 5↔6 뒤집기 |
| turn 각도 | 90° 돌고 멈춤 | STM 튜닝 |
| 직진 1칸 | 다음 노드 마커 위 정지 | STM 튜닝 |
| cmd_ack | turn/lift 완료 시 `/agv/cmd_ack` 도착 | UART `0x81` 확인 |
| forward 완료 | **마커 보고로 대신** — cmd_ack 안 옴이 정상 | — |

---

## 4. 전체 기동 순서 (순서 지킬 것)

| # | 어디서 | 명령 |
|---|---|---|
| 1 | PC | `sudo systemctl start mosquitto` (MQTT 브로커=메시지 버스. 보통 부팅 시 자동 — 이미 떠 있으면 통과) |
| 2 | PC | `./warehouse_gui_server/reset_progress.sh` (테스트 시작 전 DB 초기화, 이어할 땐 생략) |
| 3 | PC | `cd warehouse_gui_server && python3 warehouse_server_v2.py` (**반드시 그 폴더 안에서**) |
| 4 | PC | `cd TU_Capstone_Design && python3 -m server.main` |
| 5 | AGV 라파 ×2 | `python3 -m hardware.rpi_main` (헤드리스면 `--preview` 추가) |
| 6 | GUI 라파 ×2 | 1번 `python3 warehouse_gui_ws1.py` / 2번 `python3 warehouse_gui_ws2.py` |
| 7 | PC | `./isaac_simulation/run_twin.sh 3.0` (Isaac 트윈, `TWIN=1` 래퍼) |

- **순서 규칙**: #1 브로커가 맨 먼저. #2 다음 **#3·#4는 서로 독립**(아무 순서나 OK — server.main은 주문 DB를 직접 읽음). 진짜 제약은 **AGV 서버(#4)·GUI 백엔드(#3) 둘 다 주문 GUI(#6)보다 먼저** — 사람이 주문 누를 때 떠 있어야 유실 없음.
- **AGV는 홈에 북향** (AGV-1=**8**, AGV-2=**32**).
- **`DEMO_MODE` = `False` 확인** (`server/core/request_handler.py:43`).
- ⚠️ **트윈은 `run_twin.sh`(=`TWIN=1`)로만.** 맨 step7을 `TWIN=1` 없이 켜면 Isaac이 가짜 마커를 발행해 실물과 충돌.
- GUI ws2 사본 함정: `warehouse_gui_v2.py`는 작업대 2로 발행됨(1번 라파에서 열지 말 것).
- **끌 때 역순** (AGV 라파 먼저 내리면 브로커가 LWT로 offline 발행).

---

## 5. 1대 단독 주행

기동 순서에서 라파 하나만 켜고 끝까지 돌린다. 서버 로그에서:

- `presence online rid=1` — 안 뜨면 태스크 배정 안 됨.
- 마커 보고 시 `current_node`가 **인접 노드로만** 갱신 (순간이동하면 → 마커 낱장/초점).
- `[heading] ... 차이 0°` — 7단계에서 쓸 값 (지금은 로그만).

**GUI 한 바퀴**: 주문 → 선반 싣고 작업대 도착 → GUI 셀 파란불 → 피킹완료 터치 → 선반 반납. 여기까지 = 통합 성공.

---

## 6. Isaac 트윈 (필수, 실물과 병행)

```bash
./isaac_simulation/run_twin.sh 3.0        # 3.0 = 실물 1칸 소요초 (모르면 생략, EMA로 맞춰감)
```

- 실물 주행을 **트윈 띄운 채로** 한다 — 로그로 안 보이는 heading 어긋남/순간이동을 화면에서 잡는다.
- **회전은 절대값으로 실시간 추종**(수정 77, 기본값), 직진은 시간 보간. `/agv/pose` 전용(서버 안 봄).
- 트윈이 이상하게 돌면 옛 델타 방식으로: `TWIN_ABS_HEADING=0 ./isaac_simulation/run_twin.sh 3.0`

---

## 7. HEADING_OFFSET 밸브 (마지막)

지금 서버는 카메라 heading을 안 믿고(`server/config.py: TRUST_CAMERA_HEADING=False`) 로그만 찍는다. **남은 건 로그 읽기.**

```
측정: AGV를 홈에 북쪽 보게 → yaw 읽기 → HEADING_OFFSET = (0 - yaw) % 360
      → hardware/config.py 의 HEADING_OFFSET 에 기입 (현재 0 = 주원 합의값, 대개 그대로 맞음)
밸브: 주행 중 서버 로그가 계속 '[heading] ... 차이 0°' → server/config.py: TRUST_CAMERA_HEADING = True
```

밸브 열면 0-② "북향" 제약이 사라진다. (yaw 규약: 시계방향=yaw 증가, 서버 heading과 같은 부호 → 덧셈)

---

## 8. 2대 동시

1대 완주 후 붙인다.

- 라파별 `echo $AGV_ID`가 **다른지** 확인 (둘 다 1이면 명령 뺏김).
- 홈: AGV-1=**8**(W2), AGV-2=**32**(W1) — 교착 회피 교차 배치, 바꾸지 말 것.
- 볼 것: 회랑 진입 순서(STG) / 트리거 통과 시 대기 해제(TRG) / 정면 교착 해소.

---

## 증상별 원인

| 증상 | 먼저 의심 |
|---|---|
| 순간이동 / 엉뚱한 노드 | 마커 시트 낱장 아님 → 초점 |
| 아무것도 없는데 마커 검출 | 초점 (`camera_preview` sharpness) |
| `turn_left`인데 우회전 | handedness (3단계), STM 매핑 5↔6 |
| 서버가 태스크 안 줌 | presence online 미발행 → MQTT 연결 |
| GUI 주문했는데 반응 없음 | `server.main` 미기동 / presence offline |
| GUI 재고·진행 안 뜸 | `warehouse_server_v2.py` (HTTP :5000) 미기동 |
| 도착했는데 GUI 파란불 안 켜짐 | 중복주문이면 협업자 GUI 버그 (AGV 서버는 정상) |
| turn/lift 후 다음 명령 안 나감 | `cmd_ack` 미도착 → UART `0x81` |
| forward 후 멈춤 | 마커 못 봄 → 초점/마커 위치 |
| 트윈 `heading 방향 노드 없음` | heading 장부 어긋남 → 마커/초점 |

---

## 실물 없이 미리

| 하네스 | 실행 |
|---|---|
| 알고리즘 회귀 (100) | `pytest` (repo 루트) |
| SIL (가짜 STM + pty UART) | `python3 -m virtual_test.software_in_the_loop.run_sil` |
| 벤치 (카메라 + 손 마커) | `python3 -m virtual_test.bench_camera.run_bench 1` |
| 벤치 (PC 단독 자동주행) | `python3 -m virtual_test.bench_camera.run_bench 1 --no-camera --auto-walk 8` |

> ⚠️ 어느 것도 turn handedness는 검증 못 함 (0-①) — 실물 전용.

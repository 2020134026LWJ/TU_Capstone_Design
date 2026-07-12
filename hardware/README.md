# hardware/ — AGV 실물(RPi) 코드

실물 AGV(STM32 + RPi) 코드. **Isaac Sim 전용 코드는 `isaac_simulation/`으로 분리**됨.
역할 분담: **비전(카메라) = 주원이 / UART 다리(bridge) = 사용자**. 같은 라파 안에서 함수 호출로 엮고, MQTT는 라파↔서버(다른 머신)에만 쓴다.

---

## 빠른 실행 (clone & run)

```bash
# 1) 라파마다 한 번: 자기가 몇 번 AGV인지 ~/.bashrc 에 박기
echo 'export AGV_ID=1' >> ~/.bashrc   # 2번 라파는 =2

# 2) 배포 설정은 hardware/config.py 한 곳만 (서버 IP / UART / 카메라)

# 3) 실행 (repo 루트에서)
python3 -m hardware.rpi_main          # AGV_ID 환경변수로 자동
# 또는 인자로:  python3 -m hardware.rpi_main 1
```

> `AGV_ID`도 인자도 없으면 **에러로 멈춤**(기본 1 금지 — 두 라파가 같은 코드라 '둘 다 1' 충돌 방지).

---

## 구조

```
hardware/
├── rpi_main.py       ★ RPi 진입점 (camera + bridge 엮기, AGV_ID 해석)
├── camera.py         ★ 실물 카메라 — 비전 전용 (주원이 opencv 비전 그대로, detect→id/x/y/yaw)
├── bridge_rpi.py     ★ 실물 bridge — MQTT ↔ UART (주원이 STM ASCII 프로토콜 + 카메라 offset)
├── config.py         ★ 배포 설정 한 곳 (MQTT_HOST / UART_* / CALIB_FILE / SHOW_PREVIEW)
├── camera_preview.py ★ 초점 맞추기용 웹 프리뷰 (라파에 모니터 없어도 PC 브라우저로 봄)
├── stm32/
│   └── rpi_uart.c                      (주원이) STM UART 송수신 — 신호 3회 반복 반영본 (660a43e)
├── AGV_Control.zip                     (주원이) 실제 STM32F7 CubeIDE 펌웨어 (전체 프로젝트)
├── opencv_arucomarker_detection_v4.py  (주원이) 카메라 원본 — camera.py가 이 비전 로직을 그대로 옮김
└── camera_calibration.pkl              (주원이) 카메라 캘리브레이션
```

> ★ = 사용자 코드. Isaac 전용(`bridge_isaac.py`·`isaac_hw.py`·`IsaacCamera`)은 `isaac_simulation/`,
> stm32 임시 스켈레톤은 `archive/hardware_stm32_skeleton/`에.
> `stm32/rpi_uart.c`는 주원이가 별도로 올린 최신 STM 소스(전체 프로젝트는 `AGV_Control.zip`).

---

## 흐름 (실물)

```
서버 ──MQTT /agv/cmd──→ bridge_rpi ──UART <cmd,x,y,yaw>──→ STM32
camera.detect() ──(id,x,y,yaw)──→ rpi_main 루프
        ├→ bridge.set_marker_offset(x,y,yaw)  : 매 프레임 offset을 UART 패킷에 실어 스트리밍
        └→ bridge.publish_marker(id)          : 새 마커일 때만 위치 보고 (서버)
STM32 ──0x81(DONE)/0xFF(ACK)──→ bridge_rpi ──/agv/cmd_ack──→ 서버
```

- `camera.py` = 비전(ArUco offset/id)만, UART/명령은 `bridge_rpi`가 담당 (역할 분리)
- 같은 라파 안 camera↔bridge는 **메모리 공유**(함수 호출), MQTT는 라파↔서버만
- 서버 발행은 **새 마커일 때만**(`prev_marker` 가드) — 같은 마커 중복 트리거 방지. offset은 매 프레임 STM으로 계속 흐름.

---

## UART 프로토콜 (주원이 STM 기준 — 3자 일치 검증됨)

bridge_rpi 송신 / 주원이 카메라 원본 / STM 파서, 셋이 byte 단위로 동일함을 확인.

**송신** (bridge_rpi → STM): ASCII `<command,±xxxx,±yyyy,±wwww>` (21바이트)
- command 1자리(1~7, **0 = carrier/무명령**), x/y/yaw = (mm·deg)×10 정수 (STM이 /10 복원)
- offset = camera가 `set_marker_offset()`으로 공급한 최신값
- STM 파서 위치: cmd=buf[1], x=buf[3], y=buf[9], yaw=buf[15] / 프레임 `[0]=='<'`, `[20]=='>'`
- ⚠️ offset 절댓값은 **±999.9(=±9999) 이내** 가정 (넘으면 6자리 → 21바이트 깨짐)

| forward | stop | lift_up | lift_down | turn_left | turn_right | turn_180 |
|---|---|---|---|---|---|---|
| 1 | 2 | 3 | 4 | 5 | 6 | 7 |

**수신** (STM → bridge_rpi): 단일 바이트 `0x81`=DONE / `0xFF`=ACK (각 **3회 반복 송신** — 아래 통신유실 참조)

**전송 흐름**: command는 평소 0(carrier, offset만 스트리밍), MQTT 명령 오면 `_pending_code`에 실림 → `EVT_ACK` 오면 0 복귀. STM은 `STATE_READY`일 때만 command를 읽으므로(이동 중엔 무시) 다음 명령을 미리 보내도 안전.

**완료 신호 처리**:
- **forward 완료 = 카메라 마커**(서버가 forward의 ACK로 사용). → bridge는 **forward DONE을 서버로 안 보냄**(중복/오ack 방지).
- **turn/lift 완료 = cmd_ack(DONE)**.
- **heading** = 서버가 경로 기반 계산. 카메라 yaw는 STM offset에만 쓰고 서버엔 heading 미전송. (절대방위가 필요하면 마커 부착 방향 + 보정상수 K로 변환하거나 STM IMU 보고 — 미정)

---

## 통신 유실 대비 ✅

raw UART는 ACK/DONE이 1회 송신이라, **DONE 유실 → 멈춤 / ACK 유실 → 이중 실행** 위험.
(MQTT는 TCP 위라 재전송 보장되어 무관 — UART만 안전망 없음)

- **해결**(660a43e): STM `Send_Event`가 **ACK·DONE을 3회 반복 송신**(`HAL_UART_Transmit` ×3, 사이 `HAL_Delay(10)`). 한 함수만 고쳐 호출 4군데(forward/회전/리프트 DONE + ACK) 전부 커버.
- **우리 bridge는 이미 중복 안전**: DONE 3개 → 첫 번째만 cmd_ack(`_last_cmd` None으로 나머지 skip) / ACK 3개 → `_pending_code=0` 멱등. **우리 수정 0.**
- HAL_Delay 20ms는 ACK=이동 시작 전 / DONE=모터 멈춘 후라 무해, RX는 DMA라 그 동안에도 수신.

---

## 라파 카메라 환경 구축 — Pi 5 + Ubuntu 24.04 (2026-07-11, 검증됨)

> **왜 어려운가**: Raspberry Pi OS면 `sudo apt install python3-picamera2` 한 방이다.
> 그런데 **Ubuntu 24.04에는 그 패키지가 아예 없고**, 우분투가 배포한 libcamera(0.2.0)에는
> **Pi 5용 ISP 파이프라인(PiSP)이 빠져 있다** → 카메라가 **0대**로 잡힌다.
> 커널은 센서(ov5647)를 정상 인식하는데도 그렇다. apt로는 절대 해결 안 됨(백포트에도 없음).
> → **libpisp / libcamera / kmsxx를 소스 빌드**해야 한다. (Pi OS를 쓰면 이 절 전체가 불필요)

**증상 판별**: `cam -l` → `Available cameras:` 뒤가 비어 있음 / `ls /usr/lib/aarch64-linux-gnu/libcamera/`에
`ipa_rpi_vc4.so`(Pi 4 이하용)만 있고 **`ipa_rpi_pisp.so`가 없음**.

```bash
# 0) 의존성
sudo apt install -y libcamera-dev libepoxy-dev libjpeg-dev libtiff5-dev libpng-dev \
  qtbase5-dev libavcodec-dev libavdevice-dev libavformat-dev libswresample-dev \
  libboost-dev libboost-program-options-dev libgnutls28-dev openssl pybind11-dev \
  meson ninja-build cmake python3-yaml python3-ply python3-jinja2 \
  libglib2.0-dev libgstreamer-plugins-base1.0-dev libdrm-dev libexif-dev libfmt-dev \
  libyaml-dev libssl-dev libevent-dev python3-pyqt5 python3-prctl python3-pip

# 1) libpisp — Pi 5 ISP 저수준 라이브러리 (우분투에 없음)
git clone --depth 1 https://github.com/raspberrypi/libpisp.git && cd libpisp
meson setup build && ninja -C build && sudo ninja -C build install && sudo ldconfig && cd ..

# 2) libcamera — PiSP 파이프라인 켜서 빌드 (핵심)
git clone --depth 1 https://github.com/raspberrypi/libcamera.git && cd libcamera
meson setup build --buildtype=release \
  -Dpipelines=rpi/vc4,rpi/pisp -Dipas=rpi/vc4,rpi/pisp \
  -Dv4l2=true -Dgstreamer=enabled -Dtest=false -Dlc-compliance=disabled \
  -Dcam=disabled -Dqcam=disabled -Ddocumentation=disabled -Dpycamera=enabled
ninja -C build && sudo ninja -C build install && sudo ldconfig && cd ..

# 3) kmsxx — picamera2가 import 시점에 요구하는 pykms
git clone https://github.com/tomba/kmsxx.git && cd kmsxx && git submodule update --init
meson setup build -Dpykms=enabled && ninja -C build && sudo ninja -C build install && cd ..

# 4) 파이썬 패키지
pip3 install picamera2 opencv-contrib-python --break-system-packages
#   ※ apt python3-opencv(4.6)에는 cv2.aruco.ArucoDetector가 없다 → pip 최신판 필수

# 5) 장치 권한
sudo usermod -aG video,render $USER      # 재로그인 필요
sudo tee /etc/udev/rules.d/99-camera.rules > /dev/null <<'EOF'
SUBSYSTEM=="video4linux", KERNEL=="video*", MODE="0666"
SUBSYSTEM=="media", KERNEL=="media*", MODE="0666"
SUBSYSTEM=="video4linux", KERNEL=="v4l-subdev*", MODE="0666"
SUBSYSTEM=="dma_heap", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger

# 6) ★ 파이썬 경로 연결 — 우분투 함정. 이거 안 하면 위를 다 해도 import 실패
mkdir -p ~/.local/lib/python3.12/site-packages
echo "/usr/local/lib/python3/dist-packages"                     >  ~/.local/lib/python3.12/site-packages/libcamera-local.pth
echo "/usr/local/lib/aarch64-linux-gnu/python3.12/site-packages" >  ~/.local/lib/python3.12/site-packages/pykms-local.pth
```

**확인**: `python3 -c "from picamera2 import Picamera2; print(Picamera2.global_camera_info())"`
→ `[{'Model': 'ov5647', ...}]` 가 나오면 성공 (빈 리스트면 PiSP가 안 붙은 것).

**함정 요약** (하나라도 빠지면 조용히 실패):
1. `libcamera-dev`(apt) 위에 rpicam-apps만 빌드 → **API가 낡아 컴파일 에러**. libcamera부터 소스로.
2. 우분투는 `/usr/local/lib/python3/dist-packages`와 `.../python3.12/site-packages`를 **sys.path에 안 넣는다** → `.pth` 필수.
3. `video` 그룹 미가입 → `/dev/media*` **Permission denied** → 카메라 0대.
4. OpenCV 5.0은 `detectMarkers`의 `ids` 모양이 4.x와 다르다(`[[9]]` → `[9]`). `camera.py`는 양쪽 호환(`np.ravel`).

---

## ★ 카메라 초점 맞추기 — 제일 먼저 할 것 (2026-07-12)

**초점이 안 맞으면 ArUco가 없는 마커를 지어낸다.** 흐린 영상 + `DICT_4X4_250`(4×4=16비트라
ID 간 패턴 차이가 작음) 조합은 오검출의 온상이다. 실제로 겪은 사고:

> 카메라 앞에 **아무것도 없는데** 마커 37 → 3 → 4가 차례로 검출됨 → 서버가 로봇을 그 노드로
> 순간이동시킴 → heading 장부가 실제와 어긋남 → `turn_left`를 냈는데 차가 엉뚱한 방향을 봄.
> 트윈이 `heading 방향 노드 없음`으로 신고해서 발각. **뿌리는 전부 초점이었다.**

`ov5647`은 **수동 초점**이라 렌즈 경통을 손으로 돌려야 한다. 라파에 모니터가 없어도 된다:

```bash
# 라파에서
python3 -m hardware.camera_preview

# PC 브라우저에서 (주소는 실행 시 콘솔에 찍힌다)
http://<라파IP>:8000
```

- **`sharpness`(라플라시안 분산)가 최대가 되는 지점이 초점이다.** 눈대중보다 정확하다.
  절대값이 아니라 "돌렸을 때 최대가 되는 지점"을 보는 것
- 검출된 마커는 초록 테두리 + ID로 표시. **아무것도 안 댔는데 ID가 뜨면 그게 오검출**
- 카메라를 독점하므로 `run_bench` / `rpi_main`과 **동시 실행 불가**

> [주의] 수정 59의 "맵 밖 마커 무시" 필터는 오검출의 **약 80%만** 막는다.
> `DICT_4X4_250`의 오검출은 ID 0~249에 흩어지는데 우리 유효 노드는 1~48이라,
> **5번 중 1번은 유효 노드로 위장해 필터를 통과한다.** 위 사고의 37/3/4가 그 경우.
> → 근본 처방은 `DICT_6X6_50`(ID 48개만 필요한데 250개짜리를 쓸 이유가 없다).
>   마커 재인쇄가 필요하므로 바닥에 마커 깔 때 같이 할 것.

---

## 벤치 테스트 — 카메라만으로 서버↔라파 통신 검증 (STM·모터 없이)

로봇(STM32+모터)이 없어도 **서버 알고리즘 전체**를 실물 카메라로 굴려볼 수 있다.
서버는 AGV 위치를 오직 **마커 ID**로만 알기 때문에, **마커 카드를 손으로 보여주면** 된다
(공간 정확도는 STM 몫이라 이 테스트와 무관).

```bash
# 라파 (repo 루트)
python3 -m virtual_test.bench_camera.run_bench 1              # 미리보기 창 (ssh -X 로 노트북에 표시)
python3 -m virtual_test.bench_camera.run_bench 1 --no-preview # 헤드리스
python3 -m virtual_test.bench_camera.run_bench 1 --no-camera --auto-walk 9   # PC 예행연습(가짜 로봇)

# PC: 마커 시트 인쇄 (검은 사각형 25mm = camera.py marker_size)
python3 -m hardware.make_marker_sheet 9 17 25 26 27
```

- 실물 `Bridge`를 **상속만** 하므로 MQTT 경로(토픽·페이로드·발행 시점)는 진짜와 동일.
  STM이 없어 안 오는 **turn/lift 완료 신호만 가짜 타이머**가 채운다(`forward`는 마커 보고가 완료 신호).
- 배포 파일(`hardware/`)은 건드리지 않는다 — SIL과 같이 `br.UART_ENABLED` monkeypatch.
- **Isaac 트윈과 함께**: PC에서 `TWIN=1 python.sh isaac_simulation/step7_kinematic.py`
  → 라파에 마커를 보여줄 때마다 **시뮬 AGV가 따라 움직인다**.

---

## SIL — 통신 검증 (실물 없이)

SIL 하니스는 **`virtual_test/software_in_the_loop/`** 로 이동됨 (알고리즘 테스트와 함께 `virtual_test/`에서 관리). 가짜 STM + 가상 UART(pty)로 `bridge_rpi`의 명령-응답을 실물 없이 검증한다 (상세는 `run_sil.py` docstring).

```bash
python3 -m virtual_test.software_in_the_loop.run_sil   # repo 루트에서
```

---

## 설정 (`config.py`)

| 항목 | 값 | 비고 |
|---|---|---|
| `MQTT_HOST` | `172.30.1.26` | PC 서버 IP (핫스팟). 네트워크 바뀌면 여기만 |
| `UART_PORT` / `UART_BAUD` | `/dev/ttyAMA10` / `115200` | 주원이 카메라와 동일 |
| `UART_ENABLED` | `True` | 실물 전용 (SIL은 monkeypatch로 덮음) |
| `CALIB_FILE` | (자동) | `hardware/camera_calibration.pkl`, cwd 무관 |
| `SHOW_PREVIEW` | `True` | 헤드리스(디스플레이 없는) 라파면 `False` |
| **AGV_ID** | — | config 아님 → **라파별 `.bashrc` `export AGV_ID=1/2`** |

---

## 남은 일 (HIL — 실물 붙여서)

- **heading**: 마커 부착 방향 기준 보정상수 K 측정 (또는 IMU 출처 결정)
- **turn 방향**: 서버 "turn_left" = AGV 실제 좌회전인지(handedness) 확인
- **marker_id == node_id**: 바닥 ArUco를 노드 번호와 1:1 인쇄/배치
- 카메라 실동작(주원이 하드 + calibration) / 2대 동시(rid별) / picamera2·opencv 설치
- 통신유실 3회 반복 라이브 확인(거의 닫혔지만 실측)

---

## 의존성
```
paho-mqtt >= 1.6   pyserial >= 3.5   picamera2   opencv-python   numpy
```

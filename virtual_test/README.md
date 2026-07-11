# virtual_test — 실물 없이 돌려보기

실물 AGV(STM32 + 모터 + 로봇 몸체)가 없어도 **서버 → 명령 → AGV → 마커 보고 → 서버**의
전체 루프를 책상 위에서 돌릴 수 있다. 여기 있는 것들이 그 "빠진 부품 대역"이다.

## 용어

- **하네스(harness)**: 시험 대상을 대신 굴려주는 코드. 입력을 넣어주고, 없는 부품을 가짜로
  채우고, 결과를 보여준다. 시험 대상 자체는 아니다.
- **벤치(bench)**: 작업대. 실물에 붙이기 전에 책상 위에서 돌려보는 시험.
- 따라서 **벤치 하네스** = 실물 로봇 없이 책상에서 라파 코드를 굴려보는 장치.

| | 진짜인 것 | 가짜인 것 |
|---|---|---|
| **SIL** (`software_in_the_loop/`) | 라파 브릿지, UART 프로토콜 | STM(가짜 응답), 시리얼 포트(pty) |
| **벤치** (`bench_camera/`) | 라파, 카메라, MQTT, 서버, 트윈 | STM, 모터, 로봇 몸체 |
| **HIL** (실물, 아직) | 전부 | 없음 |
| **algorithm/** (pytest) | 서버 알고리즘 | 로봇·통신 전부 |

핵심: `BenchBridge`는 실물 `hardware/bridge_rpi.Bridge`를 **상속만** 한다. MQTT 경로
(토픽·페이로드·발행 시점)가 진짜와 100% 동일하고, STM이 없어 안 오는 turn/lift 완료 신호
(`0x81`)만 타이머가 대신 채운다. forward의 완료 신호는 원래도 **마커 보고**라서 대역이 필요 없다
— 사람이 마커 카드를 카메라에 보여주면 그게 곧 "그 노드에 도착했다"는 뜻이다.

---

## 모드 1 — PC 혼자서 (카메라도 로봇도 없음) ★ 가장 먼저 이걸로

가짜 로봇이 서버 명령대로 "주행"한 뒤 마커를 자동 발행한다. 사람이 할 일이 없다.
**트윈 페이싱(수정 60) 검증에 가장 정확하다** — 한 칸 주행 시간을 숫자로 지정할 수 있으니까.

```bash
# 터미널 1 — 서버
cd TU_Capstone_Design && python3 -m server.main

# 터미널 2 — Isaac 트윈 (첫 칸 추정도 실물 속도에 맞춰 3초로)
TWIN=1 TWIN_EDGE_SECS=3.0 ~/isaacsim/_build/linux-x86_64/release/python.sh \
  /home/won-ububtu/Desktop/Projects/TU_Capstone_Design/isaac_simulation/step7_kinematic.py

# 터미널 3 — 가짜 실물 AGV-1 (홈 9에서 시작, 한 칸 3초)
cd TU_Capstone_Design && python3 -m virtual_test.bench_camera.run_bench 1 \
  --no-camera --auto-walk 9 --ack-delay 3.0

# 터미널 4 — 주문 (라파 GUI 또는 직접 발행)
mosquitto_pub -h localhost -t warehouse/order/start -m '{"사용자ID":1,"주문번호":1,"작업대":2}'
```

**정상이면 이렇게 보인다** (터미널 3):

```
[Bridge-1] <- cmd: turn_180
    → (가짜 STM) turn_180 실행 중... 3.0s 후 완료 보고
[Bridge-1] -> /agv/cmd_ack  cmd=turn_180
[Bridge-1] <- cmd: forward
    → (가짜 로봇) 9 → 17 주행 중... 3.0s 후 마커 17 감지
[Bridge-1] -> /agv/marker  id=17
```

Isaac 쪽엔 `(트윈) 실측 1칸 3.0x초 → 평균 ... (n=3)`이 찍히고, AGV가 **순간이동 없이**
엣지 끝(99% 지점)에서 잠깐 멈췄다 노드에 안착하면 페이싱이 제대로 도는 것이다.
`--ack-delay 1.0`으로 바꿔 트윈도 같이 빨라지는지 보면 확실하다.

AGV 2대: 터미널을 하나 더 열고 `run_bench 2 --no-camera --auto-walk 33`.

## 모드 2 — 라파 + 진짜 카메라 (사람이 마커 카드를 보여준다)

로봇 몸체 없이, 사람 손이 "주행"을 대신한다. 바닥에 격자를 깔 필요 없다 — 서버는
'AGV가 어떤 마커를 봤는가'로만 위치를 알기 때문이다.

```bash
# 마커 카드 인쇄용 PDF (PC에서)
python3 -m hardware.make_marker_sheet

# 라파에서 (repo 루트)
python3 -m virtual_test.bench_camera.run_bench 1 --no-preview   # 모니터 없으면 --no-preview
```

서버가 `forward`를 내리면 터미널에 **"다음 노드의 마커를 카메라에 보여주세요"** 라고 뜬다.
그 카드를 카메라에 보여주는 순간이 도착 신호다. turn/lift는 가짜 STM 타이머가 처리한다.

> [주의] 이 모드에서 트윈이 재는 "1칸 소요시간"은 사실상 **사람 반응시간**이다. 트윈은 그걸
> 실물 속도로 알고 느려진다(동작 자체는 정상). 페이싱 숫자를 실물답게 보려면 모드 1을 쓴다.

## 모드 3 — 실물 (로봇이 오면)

**서버·트윈은 그대로 두고 라파에서 실행 파일만 바꾼다.**

```bash
# 라파에서 — 벤치 대신 이것만
AGV_ID=1 python3 -m hardware.rpi_main
```

벤치가 가짜로 채우던 STM 완료 신호(`0x81`)를 진짜 STM이 UART로 보내준다. 그 외에는 동일하다.

---

## 그 외

```bash
pytest                                          # 알고리즘 회귀 (algorithm/)
python3 -m virtual_test.software_in_the_loop.run_sil   # UART 프로토콜 검증 (가짜 STM + pty)
```

- 서버 IP는 `hardware/config.py`의 `MQTT_HOST` (현재 `UB-Region5.local` — mDNS라 IP 무관)
- 마커 ID = 노드 번호. 맵에 없는 ID는 서버가 무시한다 (수정 59)

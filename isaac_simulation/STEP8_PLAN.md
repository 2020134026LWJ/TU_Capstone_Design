# Step 8 — Articulation 물리 이동 작업 계획

> 작성일: 2026-04-29
> 베이스: `isaac_simulation/step7_kinematic.py` (1160줄, kinematic 패턴 거의 완성)
> 목표 산출물: `isaac_simulation/step8_articulation.py`

---

## 왜 Step 8 인가 (Step 7 한계)

Step 7(현재) 한계:
- `kinematic=True` 이라 코드가 매 프레임 `Set(xformOp:translate)`로 위치를 직접 박음
- 충돌체(`CollisionAPI`)가 있어도 **AGV가 벽/작업대/다른 AGV를 그냥 통과**함
- PhysX 기본: kinematic↔kinematic은 충돌 이벤트 자체도 보고 안 됨

Step 8 목표:
- AGV body는 dynamic rigid body, 바퀴는 `RevoluteJoint` + `JointDrive`
- 코드는 바퀴 목표 속도만 명령 → 물리엔진이 토크/마찰/관성으로 실제 이동
- 두 AGV / AGV↔벽 / AGV↔작업대가 **자연스럽게 못 겹침**
- 실물 STM32 PWM 제어와 1:1 대응 → 졸업 후 실물 전환에도 코드 흐름 유지

---

## 핵심 변경 사항 요약

| 구성 요소 | Step 7 (현재) | Step 8 (목표) |
|---|---|---|
| AGV 바디 | `DynamicCuboid` + `kinematic=True` + 코드가 위치 set | dynamic rigid body, 바퀴 토크로만 움직임 |
| AGV 좌/우 바퀴 | Visual cylinder, 시각만 회전 | `RevoluteJoint`(축=Y) + `JointDrive`로 속도 명령 |
| 회전 / 직진 | 코드가 heading/위치 직접 갱신 | 바퀴 좌/우 속도 차이로 차동구동 |
| 명령 처리 (`execute_cmd`) | `forward` → 1 grid set, `turn_left` → angle 90 set | `forward` → 양 바퀴 V 동일, T초 / `turn_left` → 좌 -V, 우 +V, T초 |
| 선반 (carrying) | 자식 prim의 로컬 좌표로 따라옴 | `FixedJoint` 또는 `D6Joint`로 AGV에 접합 / lift 시 분리 |
| 위치 추정 (마커 감지) | 코드가 set한 좌표 → 마커 거리 비교 | 물리 시뮬 결과 pose 읽어 마커 거리 비교 |
| 시저리프트 | 시각만 (arccos 공식, prismatic 없음) | `PrismaticJoint` + `JointDrive` (수직 이동) |
| 작업대/선반 collision | 작업대는 collision 없음, 선반은 `collision_box` 자식 | 동일 + 작업대에도 `CollisionAPI` 추가 |
| 벽 | `build_warehouse_env: pass` (빈 함수, 사용자 결정으로 추가 안 함) | 동일 (벽 없음 유지) |

---

## 작업 단계 (체크리스트)

### Stage 0 — 베이스 파일 복사
- [ ] `step7_kinematic.py` → `step8_articulation.py`로 복사
- [ ] 헤더 docstring 업데이트 (Step 8 목적 명시)

### Stage 1 — AGV Articulation 구조 (가장 큼, 핵심)

#### 1.1 USD 구조 변경
- [ ] `/World/AGV_<rid>` 루트 Xform 생성, 그 아래 body / wheel_l / wheel_r / scissor 배치
- [ ] body: `DynamicCuboid(kinematic=False)` + `RigidBodyAPI` + `MassAPI`
- [ ] 좌/우 바퀴: `Cylinder` + `RigidBodyAPI` + `CollisionAPI` (각각 별도 prim)
- [ ] body↔좌바퀴, body↔우바퀴: `UsdPhysics.RevoluteJoint` (축=local Y, 회전 축이 차체 좌우 방향)
- [ ] 루트에 `UsdPhysics.ArticulationRootAPI` 적용

#### 1.2 JointDrive 설정
- [ ] 각 바퀴 RevoluteJoint에 `UsdPhysics.DriveAPI:angular` 적용
- [ ] driveType="velocity", targetVelocity=0 (초기), damping/stiffness 적당값
- [ ] **[검증]** `simulation_app` 한 사이클 돌려서 바퀴 천천히 돌아가는지 확인

#### 1.3 `IsaacAGV.execute_cmd` 차동구동 변환
- [ ] `forward` 명령 → 양 바퀴 V_FWD, 1 grid 거리 / V_FWD = 시간 후 정지
- [ ] `turn_left` 명령 → 좌 -V_TURN, 우 +V_TURN, 90도 / V_TURN = 시간 후 정지
- [ ] `turn_right` 대칭, `turn_180` 시간 2배
- [ ] 회전 정확도 — 시간 기반 정지는 오차 누적 가능 → body의 yaw 각도를 읽어 목표 도달 시 정지하는 로직(피드백) 권장
- [ ] **[검증]** AGV 1대만 띄우고 forward 1번 / turn_left 1번이 정확히 1 grid / 90도 동작하는지

#### 1.4 위치 추정 (마커 감지)
- [ ] `agv.pos`를 코드가 직접 갱신하지 않고, body prim의 worldPose에서 읽어옴
- [ ] `_get_world_pos(prim) → np.array([x, y])` 헬퍼 추가
- [ ] `IsaacCamera.detect()` 입력으로 사용

### Stage 2 — 선반 들고 옮기기 (FixedJoint 동적 생성/제거)

- [ ] 현재: `_sync_shelf`가 선반 prim의 translate를 매 프레임 갱신 (delta 방식)
- [ ] 변경: pickup 시 AGV body↔선반 루트에 `UsdPhysics.FixedJoint` 동적 생성, putdown 시 제거
- [ ] 단순 fix joint로 안 되면 D6Joint(모든 축 lock) 사용
- [ ] **[검증]** AGV가 선반을 들고 회전해도 선반이 함께 회전하는지

### Stage 3 — 시저리프트 (PrismaticJoint)
- [ ] 시저리프트 상판에 PrismaticJoint(축=Z) + DriveAPI(linear, position 또는 velocity)
- [ ] `lift_up` cmd → 목표 위치 0.25m, 도달 시 cmd_ack
- [ ] `lift_down` cmd → 목표 0.0m
- [ ] 또는 step6/7처럼 시각만 유지하고 물리 prismatic은 생략 (선택)

### Stage 4 — 환경 충돌체 적용
- [ ] 작업대 (`build_workstation`): 테이블 본체 / 컨베이어 / 폴 등 큰 부피에 `CollisionAPI` 추가 (Visual 유지)
- [ ] 천장 조명 / 모니터 화면 / 작업자 등 장식은 그대로 (collision 없음)
- [ ] 바닥은 이미 OK (Step 7에서 적용됨)

### Stage 5 — 검증 시나리오
- [ ] AGV 1대 단독 — forward / turn / lift 정상
- [ ] AGV 2대 동시 동작 — 서버 알고리즘이 회피하니까 평소엔 충돌 없음
- [ ] **의도적 동시 진입 시뮬** — DEMO_MODE=False, 두 AGV가 같은 칸 노리도록 강제 → 물리적으로 못 겹치는지 시각 확인
- [ ] 벽 없음 → 그리드 외곽으로 나가도 막힘 없음 (정상, 알고리즘이 외곽 안 가게 함)
- [ ] 작업대 옆 통과 시 막힘 / 통과 시각 확인

---

## 위험 요소 (사전 체크)

1. **회전 정확도** — 시간 기반 정지(`V_TURN × T = 90°`)는 시뮬 시간 누적 오차로 73° / 102° 같은 어긋남 발생 가능. 야우(yaw) 피드백으로 목표 각도 도달 시 정지하는 PI 컨트롤 권장.
2. **선반 FixedJoint 분리/접합 타이밍** — 시뮬 진행 중 joint 동적 생성은 가능하지만 lock 깨짐/지연 주의. 처음부터 모든 선반↔AGV joint를 lock=False 로 만들어두고 lock 토글 방식도 검토.
3. **마찰계수** — 너무 작으면 바퀴 헛돌고 너무 크면 멈출 때 진동. PhysX MaterialAPI로 floor↔wheel 사이 friction 조정 필요.
4. **서버 충돌 회피와의 정합성** — 서버는 timestep + 노드 단위로 동작하는데 물리는 연속 시간. AGV가 노드 도착 보고는 마커 감지로 하니까 큰 문제 없을 듯하지만, "next_node 도착 직전 잠깐 다른 AGV가 거기 있는" 케이스에서 시각적 충돌 가능 — 알고리즘 회피 + reservation 동작이 우선이라 발생 빈도 낮음.
5. **CPU 부하** — Articulation 수가 늘면 시뮬 fps 저하 가능. AGV 2대 + 선반 8개 정도면 무난할 것.

---

## 코드 레퍼런스 (작성 시 참고할 파일/라인)

- 베이스: `isaac_simulation/step7_kinematic.py`
  - line 881~924: `build_agv` (AGV 외형 — 이 함수를 통째로 재작성하는 게 핵심)
  - line 1041~1052: 물리 레이어 초기화 (kinematic 적용 부분, 제거 또는 변경)
- 명령 처리: `IsaacAGV.execute_cmd` (현재 step7에서 위치 직접 set 하는 로직)
- 마커 감지: `hardware/camera.py`의 `IsaacCamera.detect()` (위치 입력만 worldPose로 바꾸면 OK)
- 서버 측: 변경 없음 (`server/` 그대로 — MQTT 인터페이스 유지)

---

## 검증 — 기존 회귀 테스트는 그대로 통과해야 함

```bash
cd /home/won-ububtu/Desktop/Projects/TU_Capstone_Design
pytest                       # 20개 통과 (Stage 2 산출물)
```

서버 코드 변경 없으니 테스트 모두 통과해야 정상.

---

## 다음 세션에서 이어가는 방법

1. 이 파일(`STEP8_PLAN.md`) 먼저 읽기
2. `step7_kinematic.py` 전체 한 번 훑어 현재 패턴 확인
3. Stage 0 → 1.1 → 1.2 → 1.3 순서대로 진행
4. 각 stage 끝나면 Isaac Sim 띄워서 직접 시각 확인 후 다음 단계로

처음 시작은 Stage 1.1 (USD 구조 변경) — 30분~1시간 분량.

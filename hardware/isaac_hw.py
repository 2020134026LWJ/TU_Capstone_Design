"""
Isaac Sim 가상 모터 드라이버

인터페이스 설계 원칙:
  set_speeds(left, right) 시그니처를 실물 RPi/STM32와 동일하게 유지.
  나중에 실물 전환 시 이 파일만 교체하면 됨.

교체 경로:
  시뮬레이션: hardware/isaac_hw.py  → IsaacMotors
  실물 RPi  : ../webots_simulation/controllers/agv_mqtt_controller/hardware/raspi_hw.py
              → RaspiMotors (set_speeds 채우기만 하면 됨)

STM32 UART 프로토콜 예시 (raspi_hw.py RaspiMotors.set_speeds 에 채울 내용):
  cmd = bytes([0xAA, int(left * 100) & 0xFF, int(right * 100) & 0xFF, 0x55])
  serial.write(cmd)
"""

WHEEL_BASE = 0.44  # m — 좌우 바퀴 간격 (WHEEL_OFFSETS: 2 × 0.22m)


class IsaacMotors:
    """Isaac Sim 가상 차동구동 모터

    실물 전환 시 교체 대상:
      Isaac Sim 에서는 get_velocity() 로 위치를 직접 계산.
      RPi 에서는 set_speeds() 가 STM32 UART 명령으로 대체됨.
    """

    def __init__(self):
        self._left  = 0.0
        self._right = 0.0

    # ─── 공개 인터페이스 (RPi/STM32 와 동일 시그니처) ─────────────────────────

    def set_speeds(self, left: float, right: float):
        """좌우 모터 속도 설정 (m/s)"""
        self._left  = left
        self._right = right

    def stop(self):
        """모터 정지"""
        self._left  = 0.0
        self._right = 0.0

    # ─── Isaac Sim 전용 (실물 전환 시 불필요) ─────────────────────────────────

    def get_velocity(self):
        """현재 모터 속도 → (선속도 m/s, 각속도 rad/s)

        Isaac Sim 위치 업데이트에만 사용.
        실물 RPi 에서는 이 함수가 필요 없음 (STM32 가 직접 바퀴를 구동).
        """
        linear  = (self._left + self._right) / 2.0
        angular = (self._right - self._left) / WHEEL_BASE
        return linear, angular

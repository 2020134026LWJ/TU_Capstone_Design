"""AGV 컨트롤러 진입점 - 플랫폼 자동감지"""

import os
import sys

# 경로 설정 (paho, lib 폴더)
controller_dir = os.path.dirname(os.path.abspath(__file__))
if controller_dir not in sys.path:
    sys.path.insert(0, controller_dir)

# cv2/numpy 로드 (lib 폴더 폴백)
try:
    import numpy as np
    import cv2
except ImportError:
    try:
        lib_dir = os.path.join(controller_dir, "lib")
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        import numpy as np
        import cv2
    except ImportError as e:
        print(f"[AGV] cv2/numpy not available: {e}")

# 플랫폼 감지
try:
    from controller import Supervisor
    PLATFORM = "webots"
except ImportError:
    PLATFORM = "raspi"


def main():
    if PLATFORM == "webots":
        from hardware.webots_hw import create_webots_hardware
        hw = create_webots_hardware()
    else:
        from hardware.raspi_hw import create_raspi_hardware
        rid = int(sys.argv[1]) if len(sys.argv) > 1 else 0
        start_node = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        hw = create_raspi_hardware(rid, start_node)

    from agv_controller import AGVMainController
    controller = AGVMainController(hw)
    controller.run()


if __name__ == "__main__":
    main()

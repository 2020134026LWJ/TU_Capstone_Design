"""
Isaac Sim 가상 카메라 — ArUco 마커 근접 감지
TU Capstone Design - AGV 물류 피킹 시스템

  IsaacCamera — 실제 카메라 대신 AGV 위치와 노드 거리로 마커 감지
  ※ 실물 카메라(RpiCamera)는 hardware/camera.py (주원이 opencv 비전 기반)

사용법:
  from camera import IsaacCamera
  cam = IsaacCamera(
      get_pos_fn=lambda: agv.pos,
      get_heading_fn=lambda: agv.heading,
      nodes=nodes,
      marker_node_ids=set(nodes.keys()),
  )
  marker_id, heading_deg = cam.detect()
"""

import numpy as np
from abc import ABC, abstractmethod
from typing import Callable, Optional, Set, Tuple


class Camera(ABC):
    """ArUco 마커 감지 인터페이스"""

    @abstractmethod
    def detect(self) -> Tuple[Optional[int], Optional[int]]:
        """
        마커 감지

        Returns
        -------
        (marker_id, heading_deg) 또는 (None, None)
        heading_deg: 서버 기준 (0=North, 90=East, 180=South, 270=West)
        """
        ...


class IsaacCamera(Camera):
    """
    Isaac Sim 가상 카메라 — 근접 기반 ArUco 마커 감지

    실제 카메라 대신 AGV 위치와 노드 위치의 거리로 마커를 감지.

    Parameters
    ----------
    get_pos_fn : callable
        () → np.ndarray [x, y]  — AGV 현재 월드 좌표
    get_heading_fn : callable
        () → float  — AGV 현재 heading (라디안, 0=East, π/2=North)
    nodes : dict
        {node_id: {"x": float, "y": float}}  — 전체 노드 좌표
    marker_node_ids : set
        마커가 있는 노드 ID 집합 (None이면 전체 노드)
    detect_radius : float
        감지 반경 (m), 기본값 0.087
    detect_interval : int
        매 N 프레임마다 감지, 기본값 5
    """

    def __init__(
        self,
        get_pos_fn: Callable,
        get_heading_fn: Callable,
        nodes: dict,
        marker_node_ids: Optional[Set[int]] = None,
        detect_radius: float = 0.087,
        detect_interval: int = 5,
    ):
        self._get_pos     = get_pos_fn
        self._get_heading = get_heading_fn
        self._nodes       = nodes
        self._marker_ids  = marker_node_ids if marker_node_ids is not None else set(nodes.keys())
        self._radius      = detect_radius
        self._interval    = detect_interval

        self._frame_count = 0
        self._last_marker: Optional[int] = None

    def set_last_marker(self, marker_id: Optional[int]):
        """중복 감지 방지용 — 직전 감지 마커 설정"""
        self._last_marker = marker_id

    def detect(self) -> Tuple[Optional[int], Optional[int]]:
        self._frame_count += 1
        if self._frame_count % self._interval != 0:
            return None, None

        pos     = self._get_pos()
        heading = self._get_heading()

        for node_id in self._marker_ids:
            n    = self._nodes.get(node_id)
            if n is None:
                continue
            dist = float(np.linalg.norm(pos - np.array([n["x"], n["y"]])))
            if dist < self._radius and node_id != self._last_marker:
                # AGV rad → 서버 degree
                heading_deg = int(round((90.0 - np.degrees(heading)) % 360))
                self._last_marker = node_id
                return node_id, heading_deg

        return None, None

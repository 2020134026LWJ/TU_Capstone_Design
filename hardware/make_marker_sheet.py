"""
ArUco 마커 인쇄 시트 생성 (A4 PDF)
TU Capstone Design - AGV 물류 피킹 시스템

isaac_simulation/aruco_markers/aruco_{N}.png (DICT_4X4_250, ID=노드번호)를
실물 크기로 배치한 PDF를 만든다.

크기 규약 (중요):
  검은 사각형 한 변 = 25mm — hardware/camera.py 의 self.marker_size = 25 와 반드시 일치.
  어긋나면 solvePnP가 내놓는 x/y offset(mm)이 그 비율만큼 전부 틀어져 STM PID가 오보정한다.

방향 규약 (중요):
  시트의 '↑ 북' 화살표가 맵 북쪽(노드 1~8 행 방향)을 향하도록 바닥에 붙인다.
  모든 마커의 방향이 같아야 한다 — STM이 마커를 볼 때마다 IMU heading을 재영점하므로
  한 장이라도 돌아가 있으면 그 위를 지날 때 기준이 튀어 AGV가 헛돈다.

사용:
  python3 -m hardware.make_marker_sheet                 # 노드 1~48 전체
  python3 -m hardware.make_marker_sheet 9 10 11 19      # 지정 노드만 (벤치 테스트용 카드)
  python3 -m hardware.make_marker_sheet --size 40       # 마커 한 변 40mm (camera.py도 같이 고칠 것)
"""

import argparse
import os

import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from matplotlib.backends.backend_pdf import PdfPages

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARUCO_DIR = os.path.join(_ROOT, "isaac_simulation", "aruco_markers")
OUT_PDF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marker_sheet.pdf")

MM = 1.0 / 25.4          # mm → inch
A4_W, A4_H = 210.0, 297.0  # mm
MARGIN = 15.0            # mm — 페이지 여백
LABEL_H = 8.0            # mm — 마커 아래 라벨 높이
QUIET = 6.0              # mm — 마커 주변 흰 여백 (ArUco 검출 필수)


def build_sheet(node_ids, marker_mm: float, out_path: str) -> None:
    cell_w = marker_mm + 2 * QUIET
    cell_h = marker_mm + 2 * QUIET + LABEL_H

    cols = max(1, int((A4_W - 2 * MARGIN) // cell_w))
    rows = max(1, int((A4_H - 2 * MARGIN) // cell_h))
    per_page = cols * rows

    with PdfPages(out_path) as pdf:
        for start in range(0, len(node_ids), per_page):
            page_ids = node_ids[start:start + per_page]
            fig = plt.figure(figsize=(A4_W * MM, A4_H * MM))

            for i, nid in enumerate(page_ids):
                png = os.path.join(ARUCO_DIR, f"aruco_{nid}.png")
                if not os.path.exists(png):
                    print(f"  [건너뜀] {png} 없음")
                    continue

                r, c = divmod(i, cols)
                # 좌상단 기준 배치 → figure 좌표(0~1, 좌하단 원점)로 변환
                x_mm = MARGIN + c * cell_w + QUIET
                y_mm = A4_H - MARGIN - (r + 1) * cell_h + LABEL_H + QUIET

                ax = fig.add_axes([
                    x_mm / A4_W, y_mm / A4_H,
                    marker_mm / A4_W, marker_mm / A4_H,
                ])
                ax.imshow(mpimg.imread(png), cmap="gray", interpolation="nearest")
                ax.set_xticks([]); ax.set_yticks([])
                for s in ax.spines.values():
                    s.set_visible(False)

                # 라벨 — 노드 번호 + 북쪽 방향 (마커 위쪽이 북)
                fig.text(
                    (x_mm + marker_mm / 2) / A4_W,
                    (y_mm - LABEL_H / 2) / A4_H,
                    f"node {nid}   ↑N",
                    ha="center", va="center", fontsize=7,
                )

            fig.text(0.5, 0.015,
                     f"ArUco DICT_4X4_250 · marker {marker_mm:.0f}mm "
                     f"(= camera.py marker_size) · '↑N' 을 맵 북쪽으로",
                     ha="center", fontsize=6, color="0.4")
            pdf.savefig(fig)
            plt.close(fig)

    print(f"생성: {out_path}")
    print(f"  마커 {len(node_ids)}개 · 한 변 {marker_mm:.0f}mm · 페이지당 {per_page}개")
    print("  [인쇄] '실제 크기 / 100%' 로 인쇄할 것 — '용지에 맞춤'은 크기를 바꿔버림")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("nodes", nargs="*", type=int, help="노드 번호 (없으면 1~48 전체)")
    p.add_argument("--size", type=float, default=25.0, help="마커 한 변 mm (기본 25)")
    p.add_argument("--out", default=OUT_PDF)
    args = p.parse_args()

    node_ids = args.nodes if args.nodes else list(range(1, 49))
    build_sheet(node_ids, args.size, args.out)


if __name__ == "__main__":
    main()

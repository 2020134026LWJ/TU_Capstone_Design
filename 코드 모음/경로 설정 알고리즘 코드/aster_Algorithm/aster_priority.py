import heapq
import math
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# --------------------------------
# 1. 맵 및 유틸 정의
# --------------------------------

# 0: 빈칸, 1: 장애물
GRID = [
    [0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 0],
]

H = len(GRID)       # 행 개수
W = len(GRID[0])    # 열 개수

# 상하좌우 이동
MOVES = [(1, 0), (-1, 0), (0, 1), (0, -1)] # 방향 벡터

# 좌표가 맵 안에 있는지 확인
def in_bounds(x, y):
    return 0 <= x < H and 0 <= y < W

# 빈칸인지 확인
def is_free(x, y):
    return GRID[x][y] == 0

# 휴리스틱 함수
def heuristic(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])

# --------------------------------
# 2. 시간 포함 A* (예약 테이블 사용)
# --------------------------------

# 시간 포함 A* 검색
# 한 로봇의 시공간 경로 찾기
def astar_with_time(start, goal, reserved, max_time=50):
    """
    start, goal: (x, y)
    reserved: set of (x, y, t)  -> 이미 다른 로봇이 점유한 상태
    반환: [(x, y, t), ...] 또는 None
    """
    start_state = (start[0], start[1], 0)  # t=0에서 시작
    goal_xy = (goal[0], goal[1])            # 목표 위치

    # 우선순위 큐: (f, g, (x,y,t))
    open_heap = [] # 우선순위 큐 초기화
    heapq.heappush(open_heap, (0 + heuristic(start, goal), 0, start_state)) # 시작 상태 추가

    came_from = {}  # (x,y,t) -> 이전 (x,y,t)
    g_score = {start_state: 0} # g 값 저장

    # 시간 제한
    while open_heap:
        f, g, (x, y, t) = heapq.heappop(open_heap)

        # goal 위치에 도달하면 종료 (t는 얼마든지 가능)
        if (x, y) == goal_xy:
            # 경로 복원
            path = [(x, y, t)]
            cur = (x, y, t)
            while cur in came_from:
                cur = came_from[cur]
                path.append(cur)
            path.reverse()
            return path

        if t >= max_time:
            continue

        # 1) 대기 (제자리에서 1 step 기다리기)
        neighbors = [(x, y)]

        # 2) 상하좌우 이동
        for dx, dy in MOVES:
            nx, ny = x + dx, y + dy
            if in_bounds(nx, ny) and is_free(nx, ny):
                neighbors.append((nx, ny))

        for nx, ny in neighbors:
            nt = t + 1
            next_state = (nx, ny, nt)

            # 정적 충돌 방지
            # 이미 다른 로봇이 nt 시점에 (nx,ny)에 있다면 금지
            if (nx, ny, nt) in reserved:
                continue

            # Edge conflict (서로 교차) 방지
            # 이미 다른 로봇이 nt 시점에 (x,y)로 오고, t 시점에 (nx,ny)에 있었다면 스와핑이므로 금지
            if (x, y, nt) in reserved and (nx, ny, t) in reserved:
                continue

            # g 값 갱신
            tentative_g = g + 1
            if next_state not in g_score or tentative_g < g_score[next_state]:
                g_score[next_state] = tentative_g
                f_next = tentative_g + heuristic((nx, ny), goal_xy)
                heapq.heappush(open_heap, (f_next, tentative_g, next_state))
                came_from[next_state] = (x, y, t)

    return None  # 경로 없음

# --------------------------------
# 3. Prioritized A* (우선순위 + 예약 테이블)
# --------------------------------

# 우선순위 계획 함수
# 모든 로봇 경로를 우선순위 순으로 계산하고 예약하기
def prioritized_planning(starts, goals, max_time=50, stay_time_at_goal=3):
    """
    starts, goals: 리스트 [(x,y), ...], 로봇 인덱스 순서가 곧 우선순위 (0이 가장 높음)
    반환: paths: 리스트 [ [(x,y,t), ...], ... ]
    """
    num_robots = len(starts)
    reserved = set()      # 모든 로봇의 (x,y,t)
    paths = [None] * num_robots

    for rid in range(num_robots):
        start = starts[rid]
        goal = goals[rid]

        path = astar_with_time(start, goal, reserved, max_time=max_time)
        if path is None:
            print(f"[WARN] 로봇 {rid} 경로를 찾지 못했습니다.")
            return None

        # goal에 도착 후 일정 시간 머무르게 예약 (다른 로봇이 들이받지 않도록)
        gx, gy, gt = path[-1]
        for dt in range(1, stay_time_at_goal + 1):
            reserved.add((gx, gy, gt + dt))

        # 이번 로봇 경로를 예약 테이블에 등록
        for (x, y, t) in path:
            reserved.add((x, y, t))

        paths[rid] = path

    return paths

# --------------------------------
# 4. 시뮬레이션용 준비
# --------------------------------

# 시간별 로봇 위치 인덱스 생성
def build_time_indexed_positions(paths):
    """
    paths: 로봇별 [(x,y,t), ...]
    각 t마다 로봇의 위치를 얻기 쉽게 변환
    반환: positions[robot_id][t] = (x,y)
    """
    num_robots = len(paths)
    last_t = 0
    for p in paths:
        if p:
            last_t = max(last_t, p[-1][2])

    positions = []
    for rid in range(num_robots):
        p = paths[rid]
        pos_at_time = []
        if not p:
            positions.append(pos_at_time)
            continue

        # t=0부터 last_t까지 각 시점 위치 채우기
        idx = 0
        cur_x, cur_y, cur_t = p[0]
        for t in range(last_t + 1):
            # path에서 t에 해당하는 상태를 찾거나, 이미 지난 좌표 유지
            while idx + 1 < len(p) and p[idx + 1][2] <= t:
                idx += 1
            cur_x, cur_y, cur_t = p[idx]
            pos_at_time.append((cur_x, cur_y))
        positions.append(pos_at_time)

    return positions, last_t

# --------------------------------
# 5. matplotlib 애니메이션
# --------------------------------
def run_simulation(paths, starts, goals):
    positions, last_t = build_time_indexed_positions(paths)
    num_robots = len(paths)

    fig, ax = plt.subplots()
    ax.set_aspect('equal')
    ax.set_xlim(-0.5, W - 0.5)
    ax.set_ylim(-0.5, H - 0.5)
    ax.invert_yaxis()  # (0,0)을 왼쪽 위처럼 보이게

    # 격자 & 장애물 그리기
    for i in range(H):
        for j in range(W):
            if GRID[i][j] == 1:
                ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, color='black', alpha=0.5))
            else:
                ax.add_patch(plt.Rectangle((j - 0.5, i - 0.5), 1, 1, edgecolor='lightgray', facecolor='none'))

    # 시작/목표 표시
    for rid in range(num_robots):
        sx, sy = starts[rid]
        gx, gy = goals[rid]
        ax.text(sy, sx, f"S{rid}", color='blue', ha='center', va='center')
        ax.text(gy, gx, f"G{rid}", color='red', ha='center', va='center')

    # 로봇 마커 준비
    colors = ['tab:orange', 'tab:green', 'tab:purple', 'tab:pink']
    robot_patches = []
    for rid in range(num_robots):
        x0, y0 = positions[rid][0]
        patch = plt.Circle((y0, x0), 0.3, color=colors[rid % len(colors)])
        robot_patches.append(patch)
        ax.add_patch(patch)

    time_text = ax.text(0.02, 0.95, '', transform=ax.transAxes)

    def update(frame):
        t = frame
        for rid in range(num_robots):
            if t < len(positions[rid]):
                x, y = positions[rid][t]
                robot_patches[rid].center = (y, x)
        time_text.set_text(f"t = {t}")
        return robot_patches + [time_text]

    ani = FuncAnimation(fig, update, frames=range(last_t + 1), interval=500, blit=True, repeat=False)
    plt.show()

# --------------------------------
# 6. 메인 실행부
# --------------------------------


if __name__ == "__main__":
    # 로봇 2대의 시작/목표 설정
    # 좌표는 (행, 열) = (x, y)
    starts = [
        (0, 0),  # 로봇 0
        (4, 0),  # 로봇 1
    ]
    goals = [
        (2, 3),  # 로봇 0의 목표
        (1, 3),  # 로봇 1의 목표 (서로 교차해서 지나가야 하는 상황)
    ]

    paths = prioritized_planning(starts, goals, max_time=50, stay_time_at_goal=3)

    if paths is None:
        print("경로 계획 실패")
    else:
        for rid, p in enumerate(paths):
            print(f"로봇 {rid} 경로:")
            for x, y, t in p:
                print(f"  t={t}: ({x}, {y})")
        run_simulation(paths, starts, goals)

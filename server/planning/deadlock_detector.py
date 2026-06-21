"""
교착(wait-for 사이클) 감지 — 순수 계산 도구 (core가 가져다 씀, 수정 54).

core가 로봇 상태에서 wait_for 맵(rid → 자기가 가려는 노드를 점유한 상대 rid)을
만들어 넘기면, 이 모듈은 그래프에서 사이클만 찾는다. 서버 상태를 읽거나 명령을
내리지 않음 (layering: planning은 결정/발행 X — 그건 core 몫).

원리: 막힌 로봇 각각의 "다음 노드"는 하나뿐 → wait_for는 out-degree ≤ 1
(functional graph). 사이클 탐지 = 화살표를 따라가다 이미 본 노드를 다시 만나면
그 지점부터가 사이클. 사이클 = 서로 영구히 막힌 로봇 집합 = 확정 교착.
(2대 head-on은 길이 2, 2×2 사각 회전 교착은 길이 4의 특수 케이스.)
"""
from typing import Dict, List, Optional


def find_wait_cycle(wait_for: Dict[int, int]) -> Optional[List[int]]:
    """wait_for(rid → 점유자 rid) functional graph에서 사이클 멤버 리스트 반환.

    없으면 None. 진입 꼬리(사이클로 들어가는 비순환 경로)는 제외하고 순수
    사이클만 반환. 점유자가 wait_for에 없으면(=막히지 않음, 곧 떠남) 체인이
    거기서 끝나 사이클 미형성 → 일시 블록을 교착으로 오판하지 않음.
    """
    for start in wait_for:
        seen: List[int] = []
        cur = start
        while cur in wait_for and cur not in seen:
            seen.append(cur)
            cur = wait_for[cur]
        if cur in seen:
            return seen[seen.index(cur):]
    return None

#!/bin/bash
# =====================================================================
# reset_db.sh — 시연/테스트 처음부터 다시: warehouse.db 완전 초기화
#
#   warehouse 서버 끄고 → 이 스크립트 실행 → 서버 다시 켜기
#
# 하는 일:
#   1) warehouse_server_v2.py 가 아직 떠 있으면 중단(라이브 서버 밑에서 DB를
#      갈면 그 프로세스가 옛 스키마를 물고 있어 `no such column: reserved`로 터짐)
#   2) 기존 warehouse.db 삭제
#   3) `데이터 베이스.xlsx`(재고 마스터)에서 inventory 테이블 재생성
#      → stock 원복, reserved/picking_progress/user_state 는 서버 기동 시
#        init_db()가 자동으로 다시 만든다(비어있는 깨끗한 상태).
# =====================================================================
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$DIR/warehouse.db"
GEN="$DIR/excel_to_sqlite.py"
XLSX="$DIR/데이터 베이스.xlsx"

echo "── warehouse.db 초기화 ────────────────────────────────"

# 1) 라이브 서버 가드
if pgrep -f "warehouse_server_v2.py" > /dev/null; then
  echo "❌ warehouse_server_v2.py 가 아직 실행 중입니다."
  echo "   서버를 먼저 끄고(해당 터미널 Ctrl+C) 다시 실행하세요."
  echo "   (라이브 서버 밑에서 DB를 갈면 스키마가 어긋나 오류가 납니다)"
  exit 1
fi

# 2) 필수 파일 확인
if [ ! -f "$GEN" ]; then
  echo "❌ 생성 스크립트가 없습니다: $GEN"; exit 1
fi
if [ ! -f "$XLSX" ]; then
  echo "❌ 재고 마스터 엑셀이 없습니다: $XLSX"; exit 1
fi

# 3) 삭제 → 재생성
if [ -f "$DB" ]; then
  rm -f "$DB"
  echo "  ✓ 기존 warehouse.db 삭제"
else
  echo "  · warehouse.db 없음 (새로 생성)"
fi

cd "$DIR"
python3 excel_to_sqlite.py

echo "───────────────────────────────────────────────────────"
echo "✅ 초기화 완료. 이제 warehouse_server_v2.py 를 켜면 처음부터 시작됩니다."

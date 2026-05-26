#!/bin/bash
# 시연/테스트 시작 전 완전 초기화
# - warehouse.db 삭제 → excel_to_sqlite.py로 xlsx에서 재생성
# - inventory(stock): xlsx의 마스터 값으로 복원
# - picking_progress / user_state: 협업자 server 기동 시 자동 재생성(비어있음)

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$DIR/warehouse.db"
SCRIPT="$DIR/excel_to_sqlite.py"

if [ ! -f "$SCRIPT" ]; then
  echo "❌ excel_to_sqlite.py 가 없습니다: $SCRIPT"
  exit 1
fi

if [ -f "$DB" ]; then
  rm "$DB"
  echo "  ✓ 기존 warehouse.db 삭제"
fi

cd "$DIR"
python3 excel_to_sqlite.py
echo "✅ 완전 초기화 완료 (xlsx 기준 재생성)"

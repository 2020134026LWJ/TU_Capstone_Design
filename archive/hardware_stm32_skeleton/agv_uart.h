#ifndef AGV_UART_H
#define AGV_UART_H

#include "stm32f4xx_hal.h"  /* 사용하는 시리즈에 맞게 변경 */

/* ── 패킷 헤더 ── */
#define PACKET_HEAD  0xAA

/* ── 수신 명령 코드 (RPi → STM32) ── */
#define CMD_FORWARD       0x01
#define CMD_STOP          0x02
#define CMD_LIFT_UP       0x03
#define CMD_LIFT_DOWN     0x04
#define CMD_ROTATE_LEFT   0x05
#define CMD_ROTATE_RIGHT  0x06
#define CMD_ROTATE_180    0x07

/* ── 송신 이벤트 코드 (STM32 → RPi) ── */
#define EVT_DONE  0x81  /* 동작 완료 (forward 제외 — ArUco로 감지) */
#define EVT_ACK   0xFF  /* 명령 수신 확인 */

/* ── 공개 함수 ── */
void AGV_UART_Init(UART_HandleTypeDef *huart);
void AGV_UART_SendDone(void);

/* ── 동작 함수 (agv_uart.c 하단에 실제 제어 코드 채워넣기) ── */
void AGV_Forward(void);
void AGV_Stop(void);
void AGV_RotateLeft(void);
void AGV_RotateRight(void);
void AGV_Rotate180(void);
void AGV_LiftUp(void);
void AGV_LiftDown(void);

#endif /* AGV_UART_H */

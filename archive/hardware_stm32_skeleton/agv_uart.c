#include "agv_uart.h"

#define RX_BUF_SIZE 64

static UART_HandleTypeDef *_huart;
static uint8_t _rx_byte;
static uint8_t _rx_buf[RX_BUF_SIZE];
static uint8_t _rx_idx = 0;


/* ════════════════════════════════════════════
   내부 유틸리티
   ════════════════════════════════════════════ */

static uint8_t calc_crc(uint8_t cmd, uint8_t len, uint8_t *payload) {
    uint8_t crc = cmd ^ len;
    for (uint8_t i = 0; i < len; i++) crc ^= payload[i];
    return crc;
}

static void uart_send(uint8_t event, uint8_t *payload, uint8_t len) {
    uint8_t pkt[RX_BUF_SIZE];
    pkt[0] = PACKET_HEAD;
    pkt[1] = event;
    pkt[2] = len;
    for (uint8_t i = 0; i < len; i++) pkt[3 + i] = payload[i];
    pkt[3 + len] = calc_crc(event, len, payload ? payload : (uint8_t *)"");
    HAL_UART_Transmit(_huart, pkt, 4 + len, 100);
}


/* ════════════════════════════════════════════
   공개 함수
   ════════════════════════════════════════════ */

void AGV_UART_Init(UART_HandleTypeDef *huart) {
    _huart = huart;
    HAL_UART_Receive_IT(_huart, &_rx_byte, 1);
}

void AGV_UART_SendDone(void) {
    uart_send(EVT_DONE, NULL, 0);
}


/* ════════════════════════════════════════════
   UART 수신 인터럽트
   ════════════════════════════════════════════ */

static void parse_and_execute(void) {
    if (_rx_buf[0] != PACKET_HEAD) return;

    uint8_t cmd     = _rx_buf[1];
    uint8_t len     = _rx_buf[2];
    uint8_t *payload = &_rx_buf[3];
    uint8_t crc_recv = _rx_buf[3 + len];

    if (calc_crc(cmd, len, payload) != crc_recv) return;

    uart_send(EVT_ACK, NULL, 0);

    switch (cmd) {
        case CMD_FORWARD:      AGV_Forward();      break;
        case CMD_STOP:         AGV_Stop();         break;
        case CMD_ROTATE_LEFT:  AGV_RotateLeft();   break;
        case CMD_ROTATE_RIGHT: AGV_RotateRight();  break;
        case CMD_ROTATE_180:   AGV_Rotate180();    break;
        case CMD_LIFT_UP:      AGV_LiftUp();       break;
        case CMD_LIFT_DOWN:    AGV_LiftDown();     break;
    }
}

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart) {
    if (huart != _huart) return;

    _rx_buf[_rx_idx++] = _rx_byte;

    if (_rx_idx == 1 && _rx_byte != PACKET_HEAD) {
        _rx_idx = 0;
    } else if (_rx_idx >= 3) {
        uint8_t expected = 3 + _rx_buf[2] + 1;
        if (_rx_idx >= expected) {
            parse_and_execute();
            _rx_idx = 0;
        }
    }

    HAL_UART_Receive_IT(_huart, &_rx_byte, 1);
}


/* ════════════════════════════════════════════
   동작 함수 — 실제 제어 코드 채워넣기
   ════════════════════════════════════════════ */

void AGV_Forward(void) {
    /* TODO: 모터 전진 */
    /* forward는 완료 후 AGV_UART_SendDone() 호출 안 해도 됨 */
    /* — RPi 카메라가 ArUco 마커 감지해서 서버에 알림 */
}

void AGV_Stop(void) {
    /* TODO: 모터 정지 */
}

void AGV_RotateLeft(void) {
    /* TODO: 제자리 좌회전 90° */
    AGV_UART_SendDone();
}

void AGV_RotateRight(void) {
    /* TODO: 제자리 우회전 90° */
    AGV_UART_SendDone();
}

void AGV_Rotate180(void) {
    /* TODO: 제자리 180° 회전 */
    AGV_UART_SendDone();
}

void AGV_LiftUp(void) {
    /* TODO: 리프트 올리기 */
    AGV_UART_SendDone();
}

void AGV_LiftDown(void) {
    /* TODO: 리프트 내리기 */
    AGV_UART_SendDone();
}

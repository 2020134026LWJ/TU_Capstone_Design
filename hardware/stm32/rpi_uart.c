/*
 * rpi_uart.c
 *
 *  Created on: 2026. 5. 24.
 *      Author: juwon
 */

#include "rpi_uart.h"

int Parse_ArUcoMarker_Data(const uint8_t* buf)
{
	int data = 0;

	if (buf[0] == '+' || buf[0] == '-')
	{
		data = (buf[1] - '0') * 1000 + (buf[2] - '0') * 100
				+ (buf[3] - '0') * 10 + (buf[4] - '0');

		if (buf[0] == '-') data *= -1;
	}

	return data;
}

void Read_RPI_Data(RPI_Data_t* data, const uint8_t* buf)
{
	data->command = buf[1] - '0';
	data->x_offset = Parse_ArUcoMarker_Data(&buf[3]);
	data->y_offset = Parse_ArUcoMarker_Data(&buf[9]);
	data->yaw_offset = Parse_ArUcoMarker_Data(&buf[15]);
}

void Send_Event(UART_HandleTypeDef* huart, AGV_Event_t evt)
{
	HAL_UART_Transmit(huart, &evt, 1, 10);
	HAL_Delay(10);
	HAL_UART_Transmit(huart, &evt, 1, 10);
	HAL_Delay(10);
	HAL_UART_Transmit(huart, &evt, 1, 10);
}

void Reset_RPI_Data(RPI_Data_t* data)
{
	data->command = 0;
	data->x_offset = 0;
	data->y_offset = 0;
	data->yaw_offset = 0;
}

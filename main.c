/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <stdio.h>
#include <string.h>
#include <math.h>
#include "pid.h"
#include "BNO055_STM32.h"
#include "rpi_uart.h"
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define CMD_FORWARD       0x01
#define CMD_STOP          0x02
#define CMD_LIFT_UP       0x03
#define CMD_LIFT_DOWN     0x04
#define CMD_ROTATE_LEFT   0x05
#define CMD_ROTATE_RIGHT  0x06
#define CMD_ROTATE_180    0x07
#define CMD_READY		  0x08
#define ABS(x) (((x) > 0) ? (x) : (-x))

// 캘리브레이션 값이 저장되어있는 플래시 메모리 주소 (Sector11)
#define CALIB_ADDR  ((uint32_t)0x081C0000)

#define MOTOR_CPR			64.0f
#define GEAR_RATIO			70.0f
#define ENCODER_CPR			(MOTOR_CPR * GEAR_RATIO)
#define WHEEL_DIAMETER_MM	100.0f
#define WHEEL_CIRCUMFERENCE	(M_PI * WHEEL_DIAMETER_MM)
#define TARGET_DISTANCE		500.0f
/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

I2C_HandleTypeDef hi2c1;

TIM_HandleTypeDef htim1;
TIM_HandleTypeDef htim2;
TIM_HandleTypeDef htim3;
TIM_HandleTypeDef htim4;
TIM_HandleTypeDef htim5;
TIM_HandleTypeDef htim6;
TIM_HandleTypeDef htim8;

UART_HandleTypeDef huart2;
UART_HandleTypeDef huart3;
DMA_HandleTypeDef hdma_usart2_rx;

/* USER CODE BEGIN PV */
volatile AGV_State_t state = STATE_READY;
volatile AGV_Command_t command = 0x00;
volatile AGV_Event_t event = 0x00;

volatile uint8_t control_flag = 0, print_flag = 0, usart2_flag = 0, lift_done_flag = 0;
int16_t MotorL_Speed = 0, MotorR_Speed = 0;
float Target_Yaw = 0.0f, Target_Distance = 0.0f;

// 속도, 거리 pid 제어용 변수들
uint16_t EncoderL_PrevCount, EncoderR_PrevCount;
uint16_t EncoderL_CurrCount, EncoderR_CurrCount;
int16_t EncoderL_DeltaCount, EncoderR_DeltaCount;

uint32_t EncoderL_TotalCount, EncoderR_TotalCount;
uint32_t Encoder_TotalCount;
float Total_Distance;

PID_t MOTOR_L, MOTOR_R;
PID_t DISTANCE;

// imu yaw 제어용 변수들
BNO055_Init_t bno055_init = {
		UNIT_ACC_MS2 | UNIT_GYRO_DPS | UNIT_EUL_DEG | UNIT_TEMP_CELCIUS,
		DEFAULT_AXIS_REMAP,
		DEFAULT_AXIS_SIGN,
		BNO055_NORMAL_MODE,
		NDOF,
		CLOCK_EXTERNAL,
		Range_2G
};

float BNO055_Yaw;
float Delta_Yaw;
float Yaw_Offset;
PID_t FORWARD_YAW, ROTATE_YAW;

// ArUco Marker 인식 및 보정용 변수들
uint8_t ArUcoMarker_RxBuf[RPI_RxBuf_SIZE];
RPI_Data_t rpi_data;
AGV_Event_t evt;

// lift 상태를 나타내는 변수
volatile uint8_t lift_state = 0;		// UP: 1, DOWN: 0
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MPU_Config(void);
static void MX_GPIO_Init(void);
static void MX_DMA_Init(void);
static void MX_TIM1_Init(void);
static void MX_TIM3_Init(void);
static void MX_TIM4_Init(void);
static void MX_TIM6_Init(void);
static void MX_TIM8_Init(void);
static void MX_USART3_UART_Init(void);
static void MX_I2C1_Init(void);
static void MX_USART2_UART_Init(void);
static void MX_TIM2_Init(void);
static void MX_TIM5_Init(void);
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
int _write(int file, char *ptr, int len)
{
    HAL_UART_Transmit(&huart3, (uint8_t*) ptr, len, HAL_MAX_DELAY);
    return len;
}

void Calib_Load(uint8_t *offset)
{
    for (int i = 0; i < 22; i++) {
        // 메모리 주소를 포인터로 변환하여 직접 값을 읽어옵니다.
        offset[i] = *(uint8_t *)(CALIB_ADDR + i);
    }
}

void Set_Zero_Yaw(void)
{
	BNO055_Sensors_t bno055_sensor_data;

	ReadData(&bno055_sensor_data, SENSOR_EULER);

	Yaw_Offset = bno055_sensor_data.Euler.X;
}

void Set_Marker_Yaw(float marker_yaw)
{
	BNO055_Sensors_t bno055_sensor_data;

	ReadData(&bno055_sensor_data, SENSOR_EULER);

	Yaw_Offset = bno055_sensor_data.Euler.X - marker_yaw;

	if (Yaw_Offset < 0.0f)    Yaw_Offset += 360.0f;
	if (Yaw_Offset >= 360.0f) Yaw_Offset -= 360.0f;
}

void AGV_START(void)
{
	uint8_t offset[22];

	printf("--- AGV Main Program Start ---\r\n");

	HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_1);			// MOTOR_R_PWM
	HAL_TIM_PWM_Start(&htim8, TIM_CHANNEL_1);			// MOTOR_L_PWM

	HAL_TIM_Encoder_Start(&htim3, TIM_CHANNEL_ALL);  	// MOTOR_R_ENCODER
	HAL_TIM_Encoder_Start(&htim4, TIM_CHANNEL_ALL);  	// MOTOR_L_ENCODER

	BNO055_Init(bno055_init);

	Calib_Load(offset);

	if (offset[0] != 0xFF)
	{
	    printf("Saved Calibration Data found! Applying to BNO055...\r\n");

	    setSensorOffsets(offset);

	    printf("Calibration Applied successfully.\r\n");
	}
	else
	{
	    printf("WARNING: No Calibration Data! Sensor may drift.\r\n");
	    printf("Please run the Calibration Tool firmware first.\r\n");
	}

	HAL_Delay(500);

	Set_Zero_Yaw();

	// LIFT Start
	HAL_GPIO_WritePin(STEP_EN_GPIO_Port, STEP_EN_Pin, GPIO_PIN_SET);

	__HAL_TIM_CLEAR_IT(&htim5, TIM_IT_UPDATE);
	HAL_TIM_Base_Start_IT(&htim5);
}

void ENCODER_COUNT_UPDATE(void)
{
	EncoderL_CurrCount = __HAL_TIM_GET_COUNTER(&htim3);
	EncoderR_CurrCount = __HAL_TIM_GET_COUNTER(&htim4);

	EncoderL_DeltaCount = ABS((int16_t)(EncoderL_CurrCount - EncoderL_PrevCount));
	EncoderR_DeltaCount = ABS((int16_t)(EncoderR_CurrCount - EncoderR_PrevCount));

	EncoderL_PrevCount = EncoderL_CurrCount;
	EncoderR_PrevCount = EncoderR_CurrCount;
}

float ReadYawData(void)
{
	BNO055_Sensors_t bno055_sensor_data;
	float yaw;

	ReadData(&bno055_sensor_data, SENSOR_EULER);
	yaw = bno055_sensor_data.Euler.X - Yaw_Offset;

	if (yaw < 0.0f)
		yaw += 360.0f;
	else if (yaw >= 360.0f)
		yaw -= 360.0f;

	return yaw;
}

// forward, rotate를 위한 pid control 함수

void SPEED_PID_Control(float target)
{
	PID_SetTarget(&MOTOR_L, target + 0.45f);
	PID_SetTarget(&MOTOR_R, target);

	PID_Compute(&MOTOR_L, EncoderL_DeltaCount);
	PID_Compute(&MOTOR_R, EncoderR_DeltaCount);
}

void DISTANCE_PID_Control(float target)
{
	EncoderL_TotalCount += EncoderL_DeltaCount;
	EncoderR_TotalCount += EncoderR_DeltaCount;

	Encoder_TotalCount = (EncoderL_TotalCount + EncoderR_TotalCount) / 2;

	Total_Distance = ((float)Encoder_TotalCount / ENCODER_CPR) * WHEEL_CIRCUMFERENCE;

	PID_SetTarget(&DISTANCE, target);

	PID_Compute(&DISTANCE, Total_Distance);
}

void FORWARD_YAW_PID_Control(void)
{
	BNO055_Yaw = ReadYawData();
	Delta_Yaw = Target_Yaw - BNO055_Yaw;

	// error wrap
	if (Delta_Yaw > 180.0f) Delta_Yaw -= 360.0f;
	if (Delta_Yaw < -180.0f) Delta_Yaw += 360.0f;
	//printf("Delta_Yaw: %f\r\n", Delta_Yaw);

	PID_SetTarget(&FORWARD_YAW, 0.0f);

	PID_Compute(&FORWARD_YAW, Delta_Yaw);
}

void ROTATE_YAW_PID_Control(void)
{
	BNO055_Yaw = ReadYawData();
	Delta_Yaw = Target_Yaw - BNO055_Yaw;

	// error wrap
	if (Delta_Yaw > 180.0f) Delta_Yaw -= 360.0f;
	if (Delta_Yaw < -180.0f) Delta_Yaw += 360.0f;

	if (Delta_Yaw >= 0.0f)
	{
		HAL_GPIO_WritePin(MOTOR_L_DIR_GPIO_Port, MOTOR_L_DIR_Pin, GPIO_PIN_RESET);
		HAL_GPIO_WritePin(MOTOR_R_DIR_GPIO_Port, MOTOR_R_DIR_Pin, GPIO_PIN_SET);
	}
	else
	{
		HAL_GPIO_WritePin(MOTOR_L_DIR_GPIO_Port, MOTOR_L_DIR_Pin, GPIO_PIN_SET);
		HAL_GPIO_WritePin(MOTOR_R_DIR_GPIO_Port, MOTOR_R_DIR_Pin, GPIO_PIN_RESET);
	}


	PID_SetTarget(&ROTATE_YAW, 0.0f);

	PID_Compute(&ROTATE_YAW, ABS(Delta_Yaw));
}

void AGV_FORWARD(float target_distance, uint16_t basePwm, float target)
{
	float pwm, count;

	HAL_GPIO_WritePin(MOTOR_L_DIR_GPIO_Port, MOTOR_L_DIR_Pin, GPIO_PIN_RESET);
	HAL_GPIO_WritePin(MOTOR_R_DIR_GPIO_Port, MOTOR_R_DIR_Pin, GPIO_PIN_RESET);

	if (control_flag)
	{
		control_flag = 0;
		print_flag++;

		ENCODER_COUNT_UPDATE();

		//DISTANCE_PID_CONTROL
		DISTANCE_PID_Control(target_distance);

		if (Total_Distance >= 0 && Total_Distance <= (target_distance * 0.3f))
		{
			pwm = basePwm + ((target_distance - ABS(DISTANCE.pidOutput)) * 2.5f);
			count = pwm / 20.0f;
		}
		else if (Total_Distance > (target_distance * 0.3f) && Total_Distance < (target_distance * 0.7f))
		{
			pwm = basePwm + (target_distance * 0.3f) * 2.5f;
		    count = pwm / 20.0f;
		}
		else if (Total_Distance >= (target_distance * 0.7f) && Total_Distance <= target_distance)
		{
			pwm = basePwm + (ABS(DISTANCE.pidOutput) * 2.5f);
			count = pwm / 20.0f;
		}
		else if (Total_Distance >= target_distance)
		{
			printf("Distance: %4.1f\r\n", Total_Distance);
			Total_Distance = 0.0f;
			EncoderL_TotalCount = 0;
			EncoderR_TotalCount = 0;
			Encoder_TotalCount = 0;

			__HAL_TIM_SET_COMPARE(&htim8, TIM_CHANNEL_1, 0);
			__HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, 0);

			evt = EVT_DONE;
			Send_Event(&huart2, evt);
			state = STATE_READY;

			return;
		}

		// SPEED PID CONTROL
		SPEED_PID_Control(count);

		// HEADING PID CONTROL
		FORWARD_YAW_PID_Control();

		MotorL_Speed = pwm + MOTOR_L.pidOutput - FORWARD_YAW.pidOutput;
		MotorR_Speed = pwm + MOTOR_R.pidOutput + FORWARD_YAW.pidOutput;

		//printf("foya: %f\r\n", FORWARD_YAW.pidOutput);

		if (MotorL_Speed > 2160) MotorL_Speed = 2160;
		if (MotorL_Speed < 0) MotorL_Speed = 0;
		if (MotorR_Speed > 2160) MotorR_Speed = 2160;
		if (MotorR_Speed < 0) MotorR_Speed = 0;

		__HAL_TIM_SET_COMPARE(&htim8, TIM_CHANNEL_1, MotorL_Speed);
		__HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, MotorR_Speed);

		if (print_flag >= 10)
		{
		    print_flag = 0;
		    printf("target: %4.1f, yaw: %4.1f, L_spd: %d, R_spd: %d, L_enc: %d, R_enc: %d\r\n",
		           Target_Yaw, BNO055_Yaw, MotorL_Speed, MotorR_Speed,
		           EncoderL_DeltaCount, EncoderR_DeltaCount);
		    printf("Distance: %4.1f\r\n", Total_Distance);
		}
	}
}

void AGV_STOP(void)
{
	__HAL_TIM_SET_COMPARE(&htim8, TIM_CHANNEL_1, 0);
	__HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, 0);
}

void AGV_LIFT_UP(void)
{
	if (!lift_state)
	{
		HAL_GPIO_WritePin(STEP_DIR_GPIO_Port, STEP_DIR_Pin, GPIO_PIN_RESET);
		HAL_TIM_PWM_Start(&htim2, TIM_CHANNEL_1);
		lift_state = 1;
	}
}

void AGV_LIFT_DOWN(void)
{
	if (lift_state)
	{
		HAL_GPIO_WritePin(STEP_DIR_GPIO_Port, STEP_DIR_Pin, GPIO_PIN_SET);
		HAL_TIM_PWM_Start(&htim2, TIM_CHANNEL_1);
		lift_state = 0;
	}
}

void AGV_ROTATE(uint16_t basePwm, float target)
{
	float pwm, count;
	uint16_t base = basePwm;
	static uint8_t n = 0;

	if (control_flag)
	{
		control_flag = 0;
		print_flag++;

		ENCODER_COUNT_UPDATE();

		// ROTATE PID CONTROL
		ROTATE_YAW_PID_Control();

		if (ABS(Delta_Yaw) < 10.0f) base = basePwm * 0.5f;
		else if (ABS(Delta_Yaw) < 5.0f) base = basePwm * 0.25f;

		pwm = base + (ABS(ROTATE_YAW.pidOutput) * 1.5f);
		count = target + ((ABS(ROTATE_YAW.pidOutput) * 1.5f) / 20.0f);

		// SPEED PID CONTROL
		SPEED_PID_Control(count);

		MotorL_Speed = pwm + MOTOR_L.pidOutput;
		MotorR_Speed = pwm + MOTOR_R.pidOutput;

		if (MotorL_Speed > 2160) MotorL_Speed = 2160;
		if (MotorL_Speed < 0) MotorL_Speed = 0;
		if (MotorR_Speed > 2160) MotorR_Speed = 2160;
		if (MotorR_Speed < 0) MotorR_Speed = 0;

		if (ABS(Delta_Yaw) <= 0.5f)
		{
			n++;
			MotorL_Speed = 0;
			MotorR_Speed = 0;

			if (n >= 10)
			{
				n = 0;
				evt = EVT_DONE;
				Send_Event(&huart2, evt);
				state = STATE_READY;
			}
		}
		else
		{
		    n = 0;
		}
		__HAL_TIM_SET_COMPARE(&htim8, TIM_CHANNEL_1, MotorL_Speed);
		__HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, MotorR_Speed);

		if (print_flag >= 10)
		{
		    print_flag = 0;
		    printf("pidOutput: %4.1f, target: %4.1f\r\n", ROTATE_YAW.pidOutput, count);
		    printf("target: %4.1f, yaw: %4.1f, L_spd: %d, R_spd: %d, L_enc: %d, R_enc: %d\r\n",
		           Target_Yaw, BNO055_Yaw, MotorL_Speed, MotorR_Speed,
		           EncoderL_DeltaCount, EncoderR_DeltaCount);
		}
	}
}
/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MPU Configuration--------------------------------------------------------*/
  MPU_Config();

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_DMA_Init();
  MX_TIM1_Init();
  MX_TIM3_Init();
  MX_TIM4_Init();
  MX_TIM6_Init();
  MX_TIM8_Init();
  MX_USART3_UART_Init();
  MX_I2C1_Init();
  MX_USART2_UART_Init();
  MX_TIM2_Init();
  MX_TIM5_Init();
  /* USER CODE BEGIN 2 */
  PID_Init(&MOTOR_L, 1.0, 0.0, 0.0, 0.01, 0, 4320, -4320);
  PID_Init(&MOTOR_R, 1.0, 0.0, 0.0, 0.01, 0, 4320, -4320);
  PID_Init(&FORWARD_YAW, 30.0, 0.15, 0.055, 0.01, 0, 4320, -4320);
  PID_Init(&ROTATE_YAW, 2.5, 0.0001, 0.05, 0.01, 0, 4320, -4320);
  PID_Init(&DISTANCE, 1.0, 0.0, 0.0, 0.01, 0, 4320, -4320);

  HAL_TIM_Base_Start_IT(&htim6);
  HAL_UARTEx_ReceiveToIdle_DMA(&huart2, ArUcoMarker_RxBuf, RPI_RxBuf_SIZE);
  __HAL_DMA_DISABLE_IT(&hdma_usart2_rx, DMA_IT_HT);

  AGV_START();

  Set_Marker_Yaw(90.0f);

  Send_Event(&huart2, EVT_DONE);

  float correct_angle = 0.0f, x_mm = 0.0f, y_mm = 0.0f, base_angle = 90.0f;
  float marker_yaw = 0.0f, imu_yaw = 0.0f, dyaw = 0.0f;
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
	  if (state == STATE_MOVE)
	  {
		  switch(command)
		  {
		  case CMD_FORWARD:
			  AGV_FORWARD(Target_Distance, 450, 25.0f);
			  break;
		  case CMD_STOP:
			  AGV_STOP();
			  break;
		  case CMD_LIFT_UP:
			  AGV_LIFT_UP();
			  break;
		  case CMD_LIFT_DOWN:
			  AGV_LIFT_DOWN();
			  break;
		  case CMD_ROTATE_LEFT:
			  AGV_ROTATE(300, 20.0f);
			  break;
		  case CMD_ROTATE_RIGHT:
			  AGV_ROTATE(300, 20.0f);
			  break;
		  case CMD_ROTATE_180:
			  AGV_ROTATE(300, 20.0f);
			  break;
		  default:
			  AGV_STOP();
			  break;
		  }
	  }
	  else if (state == STATE_READY)
	  {
		  if (usart2_flag)
		  {
			  usart2_flag = 0;
			  Read_RPI_Data(&rpi_data, ArUcoMarker_RxBuf);

			  command = rpi_data.command;
			  x_mm = rpi_data.x_offset / 10.0f;
			  y_mm = rpi_data.y_offset / 10.0f;
			  marker_yaw = (rpi_data.yaw_offset / 10.0f);

			  imu_yaw = ReadYawData();
			  dyaw = marker_yaw - imu_yaw;

			  if (dyaw > 180.0f) dyaw -= 360.0f;
			  if (dyaw < -180.0f) dyaw += 360.0f;

			  if (command >= CMD_FORWARD && command <= CMD_ROTATE_180)
			  {
			      if (ABS(dyaw) <= 0.5f)
			      {
			    	  printf("UPDATE YAW OFFSET!!!!!\r\n");
			    	  Set_Marker_Yaw(base_angle);
			      }

			      if (command == CMD_FORWARD)
			      {
			          // lateral(x) 조준은 여기(직진)에서만. 멀리 벌어졌을 때만 각을 준다.
			          float dx = -x_mm;
			          float dy = TARGET_DISTANCE - y_mm;

			          if (ABS(x_mm) >= 5.0f)
			              correct_angle = atan2f(dx, dy) * 180.0f / M_PI;
			          else
			              correct_angle = 0.0f;

			          if (correct_angle > 5.0f)  correct_angle = 5.0f;
			          if (correct_angle < -5.0f) correct_angle = -5.0f;

			          Target_Yaw = base_angle + correct_angle;

			          Target_Distance = sqrtf(dx * dx + dy * dy);
			          if (Target_Yaw >= 360.0f) Target_Yaw -= 360.0f;
			          if (Target_Yaw < 0.0f)    Target_Yaw += 360.0f;
			          printf("\r\nx_mm: %.2f, y_mm: %.2f, ang: %.2f, dist: %.2f, base: %.1f\r\n",
			                 x_mm, y_mm, Target_Yaw, Target_Distance, base_angle);
			      }

			      if (command == CMD_ROTATE_LEFT)
			      {
			          base_angle -= 90.0f;
			          if (base_angle >= 360.0f) base_angle -= 360.0f;
			          if (base_angle < 0.0f)    base_angle += 360.0f;

			          Target_Yaw = base_angle;

				      if (ABS(dyaw) >= 5.0f)
				      {
				    	  Target_Yaw -= dyaw;

				          if (Target_Yaw < 0.0f)    Target_Yaw += 360.0f;
				          if (Target_Yaw >= 360.0f) Target_Yaw -= 360.0f;
				      }

			          printf("\r\nx_mm: %.2f, y_mm: %.2f, dyaw: %.2f, ang: %.2f\r\n",
			                 x_mm, y_mm, dyaw, Target_Yaw);
			      }

			      if (command == CMD_ROTATE_RIGHT)
			      {
			          base_angle += 90.0f;
			          if (base_angle >= 360.0f) base_angle -= 360.0f;
			          if (base_angle < 0.0f)    base_angle += 360.0f;

			          Target_Yaw = base_angle;

				      if (ABS(dyaw) >= 5.0f)
				      {
				    	  Target_Yaw -= dyaw;

				          if (Target_Yaw < 0.0f)    Target_Yaw += 360.0f;
				          if (Target_Yaw >= 360.0f) Target_Yaw -= 360.0f;
				      }

			          printf("\r\nx_mm: %.2f, y_mm: %.2f, dyaw: %.2f, ang: %.2f\r\n",
			                 x_mm, y_mm, dyaw, Target_Yaw);
			      }

			      if (command == CMD_ROTATE_180)
			      {
			          base_angle += 180.0f;
			          if (base_angle >= 360.0f) base_angle -= 360.0f;
			          if (base_angle < 0.0f)    base_angle += 360.0f;

			          Target_Yaw = base_angle;

				      if (ABS(dyaw) >= 5.0f)
				      {
				    	  Target_Yaw -= dyaw;

				          if (Target_Yaw < 0.0f)    Target_Yaw += 360.0f;
				          if (Target_Yaw >= 360.0f) Target_Yaw -= 360.0f;
				      }

			          printf("\r\nx_mm: %.2f, y_mm: %.2f, dyaw: %.2f, ang: %.2f\r\n",
			                 x_mm, y_mm, dyaw, Target_Yaw);
			      }

			      evt = EVT_ACK;

			      EncoderL_PrevCount = __HAL_TIM_GET_COUNTER(&htim3);
			      EncoderR_PrevCount = __HAL_TIM_GET_COUNTER(&htim4);

			      Send_Event(&huart2, evt);
			      state = STATE_MOVE;
			  }
		  }
	  }

	  if (lift_done_flag)
	  {
		  lift_done_flag = 0;
		  evt = EVT_DONE;
		  Send_Event(&huart2, evt);
		  state = STATE_READY;
	  }
	  /* USER CODE END WHILE */

	  /* USER CODE BEGIN 3 */
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure LSE Drive Capability
  */
  HAL_PWR_EnableBkUpAccess();

  /** Configure the main internal regulator output voltage
  */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE3);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_BYPASS;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = 4;
  RCC_OscInitStruct.PLL.PLLN = 96;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = 4;
  RCC_OscInitStruct.PLL.PLLR = 2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Activate the Over-Drive mode
  */
  if (HAL_PWREx_EnableOverDrive() != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_3) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief I2C1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_I2C1_Init(void)
{

  /* USER CODE BEGIN I2C1_Init 0 */

  /* USER CODE END I2C1_Init 0 */

  /* USER CODE BEGIN I2C1_Init 1 */

  /* USER CODE END I2C1_Init 1 */
  hi2c1.Instance = I2C1;
  hi2c1.Init.Timing = 0x20303E5D;
  hi2c1.Init.OwnAddress1 = 0;
  hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  hi2c1.Init.OwnAddress2 = 0;
  hi2c1.Init.OwnAddress2Masks = I2C_OA2_NOMASK;
  hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&hi2c1) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure Analogue filter
  */
  if (HAL_I2CEx_ConfigAnalogFilter(&hi2c1, I2C_ANALOGFILTER_ENABLE) != HAL_OK)
  {
    Error_Handler();
  }

  /** Configure Digital filter
  */
  if (HAL_I2CEx_ConfigDigitalFilter(&hi2c1, 0) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN I2C1_Init 2 */

  /* USER CODE END I2C1_Init 2 */

}

/**
  * @brief TIM1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM1_Init(void)
{

  /* USER CODE BEGIN TIM1_Init 0 */

  /* USER CODE END TIM1_Init 0 */

  TIM_MasterConfigTypeDef sMasterConfig = {0};
  TIM_OC_InitTypeDef sConfigOC = {0};
  TIM_BreakDeadTimeConfigTypeDef sBreakDeadTimeConfig = {0};

  /* USER CODE BEGIN TIM1_Init 1 */

  /* USER CODE END TIM1_Init 1 */
  htim1.Instance = TIM1;
  htim1.Init.Prescaler = 1-1;
  htim1.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim1.Init.Period = 4800-1;
  htim1.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim1.Init.RepetitionCounter = 0;
  htim1.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_ENABLE;
  if (HAL_TIM_PWM_Init(&htim1) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterOutputTrigger2 = TIM_TRGO2_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim1, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  sConfigOC.OCMode = TIM_OCMODE_PWM1;
  sConfigOC.Pulse = 0;
  sConfigOC.OCPolarity = TIM_OCPOLARITY_HIGH;
  sConfigOC.OCNPolarity = TIM_OCNPOLARITY_HIGH;
  sConfigOC.OCFastMode = TIM_OCFAST_DISABLE;
  sConfigOC.OCIdleState = TIM_OCIDLESTATE_RESET;
  sConfigOC.OCNIdleState = TIM_OCNIDLESTATE_RESET;
  if (HAL_TIM_PWM_ConfigChannel(&htim1, &sConfigOC, TIM_CHANNEL_1) != HAL_OK)
  {
    Error_Handler();
  }
  sBreakDeadTimeConfig.OffStateRunMode = TIM_OSSR_DISABLE;
  sBreakDeadTimeConfig.OffStateIDLEMode = TIM_OSSI_DISABLE;
  sBreakDeadTimeConfig.LockLevel = TIM_LOCKLEVEL_OFF;
  sBreakDeadTimeConfig.DeadTime = 0;
  sBreakDeadTimeConfig.BreakState = TIM_BREAK_DISABLE;
  sBreakDeadTimeConfig.BreakPolarity = TIM_BREAKPOLARITY_HIGH;
  sBreakDeadTimeConfig.BreakFilter = 0;
  sBreakDeadTimeConfig.Break2State = TIM_BREAK2_DISABLE;
  sBreakDeadTimeConfig.Break2Polarity = TIM_BREAK2POLARITY_HIGH;
  sBreakDeadTimeConfig.Break2Filter = 0;
  sBreakDeadTimeConfig.AutomaticOutput = TIM_AUTOMATICOUTPUT_DISABLE;
  if (HAL_TIMEx_ConfigBreakDeadTime(&htim1, &sBreakDeadTimeConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM1_Init 2 */

  /* USER CODE END TIM1_Init 2 */
  HAL_TIM_MspPostInit(&htim1);

}

/**
  * @brief TIM2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM2_Init(void)
{

  /* USER CODE BEGIN TIM2_Init 0 */

  /* USER CODE END TIM2_Init 0 */

  TIM_ClockConfigTypeDef sClockSourceConfig = {0};
  TIM_MasterConfigTypeDef sMasterConfig = {0};
  TIM_OC_InitTypeDef sConfigOC = {0};

  /* USER CODE BEGIN TIM2_Init 1 */

  /* USER CODE END TIM2_Init 1 */
  htim2.Instance = TIM2;
  htim2.Init.Prescaler = 96-1;
  htim2.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim2.Init.Period = 2000-1;
  htim2.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim2.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_ENABLE;
  if (HAL_TIM_Base_Init(&htim2) != HAL_OK)
  {
    Error_Handler();
  }
  sClockSourceConfig.ClockSource = TIM_CLOCKSOURCE_INTERNAL;
  if (HAL_TIM_ConfigClockSource(&htim2, &sClockSourceConfig) != HAL_OK)
  {
    Error_Handler();
  }
  if (HAL_TIM_PWM_Init(&htim2) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_UPDATE;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_ENABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim2, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  sConfigOC.OCMode = TIM_OCMODE_PWM1;
  sConfigOC.Pulse = 1000;
  sConfigOC.OCPolarity = TIM_OCPOLARITY_HIGH;
  sConfigOC.OCFastMode = TIM_OCFAST_DISABLE;
  if (HAL_TIM_PWM_ConfigChannel(&htim2, &sConfigOC, TIM_CHANNEL_1) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM2_Init 2 */

  /* USER CODE END TIM2_Init 2 */
  HAL_TIM_MspPostInit(&htim2);

}

/**
  * @brief TIM3 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM3_Init(void)
{

  /* USER CODE BEGIN TIM3_Init 0 */

  /* USER CODE END TIM3_Init 0 */

  TIM_Encoder_InitTypeDef sConfig = {0};
  TIM_MasterConfigTypeDef sMasterConfig = {0};

  /* USER CODE BEGIN TIM3_Init 1 */

  /* USER CODE END TIM3_Init 1 */
  htim3.Instance = TIM3;
  htim3.Init.Prescaler = 0;
  htim3.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim3.Init.Period = 65535;
  htim3.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim3.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_DISABLE;
  sConfig.EncoderMode = TIM_ENCODERMODE_TI12;
  sConfig.IC1Polarity = TIM_ICPOLARITY_RISING;
  sConfig.IC1Selection = TIM_ICSELECTION_DIRECTTI;
  sConfig.IC1Prescaler = TIM_ICPSC_DIV1;
  sConfig.IC1Filter = 15;
  sConfig.IC2Polarity = TIM_ICPOLARITY_RISING;
  sConfig.IC2Selection = TIM_ICSELECTION_DIRECTTI;
  sConfig.IC2Prescaler = TIM_ICPSC_DIV1;
  sConfig.IC2Filter = 15;
  if (HAL_TIM_Encoder_Init(&htim3, &sConfig) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim3, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM3_Init 2 */

  /* USER CODE END TIM3_Init 2 */

}

/**
  * @brief TIM4 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM4_Init(void)
{

  /* USER CODE BEGIN TIM4_Init 0 */

  /* USER CODE END TIM4_Init 0 */

  TIM_Encoder_InitTypeDef sConfig = {0};
  TIM_MasterConfigTypeDef sMasterConfig = {0};

  /* USER CODE BEGIN TIM4_Init 1 */

  /* USER CODE END TIM4_Init 1 */
  htim4.Instance = TIM4;
  htim4.Init.Prescaler = 0;
  htim4.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim4.Init.Period = 65535;
  htim4.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim4.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_DISABLE;
  sConfig.EncoderMode = TIM_ENCODERMODE_TI12;
  sConfig.IC1Polarity = TIM_ICPOLARITY_RISING;
  sConfig.IC1Selection = TIM_ICSELECTION_DIRECTTI;
  sConfig.IC1Prescaler = TIM_ICPSC_DIV1;
  sConfig.IC1Filter = 15;
  sConfig.IC2Polarity = TIM_ICPOLARITY_RISING;
  sConfig.IC2Selection = TIM_ICSELECTION_DIRECTTI;
  sConfig.IC2Prescaler = TIM_ICPSC_DIV1;
  sConfig.IC2Filter = 15;
  if (HAL_TIM_Encoder_Init(&htim4, &sConfig) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim4, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM4_Init 2 */

  /* USER CODE END TIM4_Init 2 */

}

/**
  * @brief TIM5 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM5_Init(void)
{

  /* USER CODE BEGIN TIM5_Init 0 */

  /* USER CODE END TIM5_Init 0 */

  TIM_SlaveConfigTypeDef sSlaveConfig = {0};
  TIM_MasterConfigTypeDef sMasterConfig = {0};

  /* USER CODE BEGIN TIM5_Init 1 */

  /* USER CODE END TIM5_Init 1 */
  htim5.Instance = TIM5;
  htim5.Init.Prescaler = 0;
  htim5.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim5.Init.Period = 1050-1;
  htim5.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim5.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_ENABLE;
  if (HAL_TIM_Base_Init(&htim5) != HAL_OK)
  {
    Error_Handler();
  }
  sSlaveConfig.SlaveMode = TIM_SLAVEMODE_EXTERNAL1;
  sSlaveConfig.InputTrigger = TIM_TS_ITR0;
  if (HAL_TIM_SlaveConfigSynchro(&htim5, &sSlaveConfig) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim5, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM5_Init 2 */

  /* USER CODE END TIM5_Init 2 */

}

/**
  * @brief TIM6 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM6_Init(void)
{

  /* USER CODE BEGIN TIM6_Init 0 */

  /* USER CODE END TIM6_Init 0 */

  TIM_MasterConfigTypeDef sMasterConfig = {0};

  /* USER CODE BEGIN TIM6_Init 1 */

  /* USER CODE END TIM6_Init 1 */
  htim6.Instance = TIM6;
  htim6.Init.Prescaler = 9600-1;
  htim6.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim6.Init.Period = 100-1;
  htim6.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_DISABLE;
  if (HAL_TIM_Base_Init(&htim6) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim6, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM6_Init 2 */

  /* USER CODE END TIM6_Init 2 */

}

/**
  * @brief TIM8 Initialization Function
  * @param None
  * @retval None
  */
static void MX_TIM8_Init(void)
{

  /* USER CODE BEGIN TIM8_Init 0 */

  /* USER CODE END TIM8_Init 0 */

  TIM_MasterConfigTypeDef sMasterConfig = {0};
  TIM_OC_InitTypeDef sConfigOC = {0};
  TIM_BreakDeadTimeConfigTypeDef sBreakDeadTimeConfig = {0};

  /* USER CODE BEGIN TIM8_Init 1 */

  /* USER CODE END TIM8_Init 1 */
  htim8.Instance = TIM8;
  htim8.Init.Prescaler = 1-1;
  htim8.Init.CounterMode = TIM_COUNTERMODE_UP;
  htim8.Init.Period = 4800-1;
  htim8.Init.ClockDivision = TIM_CLOCKDIVISION_DIV1;
  htim8.Init.RepetitionCounter = 0;
  htim8.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_ENABLE;
  if (HAL_TIM_PWM_Init(&htim8) != HAL_OK)
  {
    Error_Handler();
  }
  sMasterConfig.MasterOutputTrigger = TIM_TRGO_RESET;
  sMasterConfig.MasterOutputTrigger2 = TIM_TRGO2_RESET;
  sMasterConfig.MasterSlaveMode = TIM_MASTERSLAVEMODE_DISABLE;
  if (HAL_TIMEx_MasterConfigSynchronization(&htim8, &sMasterConfig) != HAL_OK)
  {
    Error_Handler();
  }
  sConfigOC.OCMode = TIM_OCMODE_PWM1;
  sConfigOC.Pulse = 0;
  sConfigOC.OCPolarity = TIM_OCPOLARITY_HIGH;
  sConfigOC.OCNPolarity = TIM_OCNPOLARITY_HIGH;
  sConfigOC.OCFastMode = TIM_OCFAST_DISABLE;
  sConfigOC.OCIdleState = TIM_OCIDLESTATE_RESET;
  sConfigOC.OCNIdleState = TIM_OCNIDLESTATE_RESET;
  if (HAL_TIM_PWM_ConfigChannel(&htim8, &sConfigOC, TIM_CHANNEL_1) != HAL_OK)
  {
    Error_Handler();
  }
  sBreakDeadTimeConfig.OffStateRunMode = TIM_OSSR_DISABLE;
  sBreakDeadTimeConfig.OffStateIDLEMode = TIM_OSSI_DISABLE;
  sBreakDeadTimeConfig.LockLevel = TIM_LOCKLEVEL_OFF;
  sBreakDeadTimeConfig.DeadTime = 0;
  sBreakDeadTimeConfig.BreakState = TIM_BREAK_DISABLE;
  sBreakDeadTimeConfig.BreakPolarity = TIM_BREAKPOLARITY_HIGH;
  sBreakDeadTimeConfig.BreakFilter = 0;
  sBreakDeadTimeConfig.Break2State = TIM_BREAK2_DISABLE;
  sBreakDeadTimeConfig.Break2Polarity = TIM_BREAK2POLARITY_HIGH;
  sBreakDeadTimeConfig.Break2Filter = 0;
  sBreakDeadTimeConfig.AutomaticOutput = TIM_AUTOMATICOUTPUT_DISABLE;
  if (HAL_TIMEx_ConfigBreakDeadTime(&htim8, &sBreakDeadTimeConfig) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN TIM8_Init 2 */

  /* USER CODE END TIM8_Init 2 */
  HAL_TIM_MspPostInit(&htim8);

}

/**
  * @brief USART2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART2_UART_Init(void)
{

  /* USER CODE BEGIN USART2_Init 0 */

  /* USER CODE END USART2_Init 0 */

  /* USER CODE BEGIN USART2_Init 1 */

  /* USER CODE END USART2_Init 1 */
  huart2.Instance = USART2;
  huart2.Init.BaudRate = 115200;
  huart2.Init.WordLength = UART_WORDLENGTH_8B;
  huart2.Init.StopBits = UART_STOPBITS_1;
  huart2.Init.Parity = UART_PARITY_NONE;
  huart2.Init.Mode = UART_MODE_TX_RX;
  huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart2.Init.OverSampling = UART_OVERSAMPLING_16;
  huart2.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
  huart2.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
  if (HAL_UART_Init(&huart2) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART2_Init 2 */

  /* USER CODE END USART2_Init 2 */

}

/**
  * @brief USART3 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART3_UART_Init(void)
{

  /* USER CODE BEGIN USART3_Init 0 */

  /* USER CODE END USART3_Init 0 */

  /* USER CODE BEGIN USART3_Init 1 */

  /* USER CODE END USART3_Init 1 */
  huart3.Instance = USART3;
  huart3.Init.BaudRate = 115200;
  huart3.Init.WordLength = UART_WORDLENGTH_8B;
  huart3.Init.StopBits = UART_STOPBITS_1;
  huart3.Init.Parity = UART_PARITY_NONE;
  huart3.Init.Mode = UART_MODE_TX_RX;
  huart3.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart3.Init.OverSampling = UART_OVERSAMPLING_16;
  huart3.Init.OneBitSampling = UART_ONE_BIT_SAMPLE_DISABLE;
  huart3.AdvancedInit.AdvFeatureInit = UART_ADVFEATURE_NO_INIT;
  if (HAL_UART_Init(&huart3) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART3_Init 2 */

  /* USER CODE END USART3_Init 2 */

}

/**
  * Enable DMA controller clock
  */
static void MX_DMA_Init(void)
{

  /* DMA controller clock enable */
  __HAL_RCC_DMA1_CLK_ENABLE();

  /* DMA interrupt init */
  /* DMA1_Stream5_IRQn interrupt configuration */
  HAL_NVIC_SetPriority(DMA1_Stream5_IRQn, 0, 0);
  HAL_NVIC_EnableIRQ(DMA1_Stream5_IRQn);

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOH_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();
  __HAL_RCC_GPIOF_CLK_ENABLE();
  __HAL_RCC_GPIOE_CLK_ENABLE();
  __HAL_RCC_GPIOD_CLK_ENABLE();

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(GPIOB, LD1_Pin|LD3_Pin|MOTOR_L_DIR_Pin|LD2_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(MOTOR_R_DIR_GPIO_Port, MOTOR_R_DIR_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin Output Level */
  HAL_GPIO_WritePin(GPIOD, STEP_DIR_Pin|STEP_EN_Pin, GPIO_PIN_RESET);

  /*Configure GPIO pin : USER_Btn_Pin */
  GPIO_InitStruct.Pin = USER_Btn_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_IT_RISING;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(USER_Btn_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pins : LD1_Pin LD3_Pin MOTOR_L_DIR_Pin LD2_Pin */
  GPIO_InitStruct.Pin = LD1_Pin|LD3_Pin|MOTOR_L_DIR_Pin|LD2_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOB, &GPIO_InitStruct);

  /*Configure GPIO pin : MOTOR_R_DIR_Pin */
  GPIO_InitStruct.Pin = MOTOR_R_DIR_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(MOTOR_R_DIR_GPIO_Port, &GPIO_InitStruct);

  /*Configure GPIO pins : STEP_DIR_Pin STEP_EN_Pin */
  GPIO_InitStruct.Pin = STEP_DIR_Pin|STEP_EN_Pin;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOD, &GPIO_InitStruct);

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */
void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
	if (huart->Instance == USART3)
	{
		if (command == CMD_ROTATE_LEFT)
			Target_Yaw -= 90.0f;
		else if (command == CMD_ROTATE_RIGHT)
			Target_Yaw += 90.0f;
		else if (command == CMD_ROTATE_180)
			Target_Yaw += 180.0f;

		if (Target_Yaw >= 360.0) Target_Yaw -= 360;
		if (Target_Yaw < 0.0) Target_Yaw += 360;
	}
}

void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *htim)
{
    if (htim->Instance == TIM6) {
    	control_flag++;
    }

	if (htim->Instance == TIM5)
	{
		HAL_TIM_PWM_Stop(&htim2, TIM_CHANNEL_1);
        __HAL_TIM_SET_COUNTER(&htim5, 0);

        lift_done_flag++;
	}
}

void HAL_UARTEx_RxEventCallback(UART_HandleTypeDef *huart, uint16_t Size)
{
	if (huart->Instance == USART2)
	{
		if (Size == RPI_RxBuf_SIZE && ArUcoMarker_RxBuf[0] == '<' && ArUcoMarker_RxBuf[20] == '>')
		{
			//printf("%s\r\n", ArUcoMarker_RxBuf);
			usart2_flag = 1;
		}
		else
		{
			printf("RX FAIL sz=%d [0]=%d [20]=%d\r\n",   // ← 추가
			       Size, ArUcoMarker_RxBuf[0], ArUcoMarker_RxBuf[20]);
			HAL_UART_DMAStop(&huart2);
			HAL_UARTEx_ReceiveToIdle_DMA(&huart2, ArUcoMarker_RxBuf, RPI_RxBuf_SIZE);
			__HAL_DMA_DISABLE_IT(&hdma_usart2_rx, DMA_IT_HT);
		}
	}
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
    if (huart->Instance == USART2)
    {
    	__HAL_UART_CLEAR_OREFLAG(huart);
    	__HAL_UART_CLEAR_NEFLAG(huart);
    	__HAL_UART_CLEAR_FEFLAG(huart);

		HAL_UART_DMAStop(&huart2);
		HAL_UARTEx_ReceiveToIdle_DMA(&huart2, ArUcoMarker_RxBuf, RPI_RxBuf_SIZE);
		__HAL_DMA_DISABLE_IT(&hdma_usart2_rx, DMA_IT_HT);
    }
}
/* USER CODE END 4 */

 /* MPU Configuration */

void MPU_Config(void)
{
  MPU_Region_InitTypeDef MPU_InitStruct = {0};

  /* Disables the MPU */
  HAL_MPU_Disable();

  /** Initializes and configures the Region and the memory to be protected
  */
  MPU_InitStruct.Enable = MPU_REGION_ENABLE;
  MPU_InitStruct.Number = MPU_REGION_NUMBER0;
  MPU_InitStruct.BaseAddress = 0x0;
  MPU_InitStruct.Size = MPU_REGION_SIZE_4GB;
  MPU_InitStruct.SubRegionDisable = 0x87;
  MPU_InitStruct.TypeExtField = MPU_TEX_LEVEL0;
  MPU_InitStruct.AccessPermission = MPU_REGION_NO_ACCESS;
  MPU_InitStruct.DisableExec = MPU_INSTRUCTION_ACCESS_DISABLE;
  MPU_InitStruct.IsShareable = MPU_ACCESS_SHAREABLE;
  MPU_InitStruct.IsCacheable = MPU_ACCESS_NOT_CACHEABLE;
  MPU_InitStruct.IsBufferable = MPU_ACCESS_NOT_BUFFERABLE;

  HAL_MPU_ConfigRegion(&MPU_InitStruct);
  /* Enables the MPU */
  HAL_MPU_Enable(MPU_PRIVILEGED_DEFAULT);

}

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */

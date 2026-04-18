#include "motor.h"

#include <avr/io.h>
#include <util/delay.h>

#define MOTOR_STEP_DELAY_MS  50
#define MOTOR_HOME_SETTLE_MS 200

static const uint8_t MOTOR_FULL_STEP_SEQ[4] = {0x03, 0x06, 0x0C, 0x09};

static int16_t motor_step_pos = 0;
static uint8_t motor_seq_idx = 0;

static void motor_apply(uint8_t pattern) {
    PORTB = (PORTB & 0xF0) | (pattern & 0x0F);
}

void motor_init(void) {
    DDRB |= 0x0F;   /* PB0-PB3 drive the H-bridge inputs */
    motor_release();
}

void motor_home(void) {
    motor_seq_idx = 0;
    motor_apply(MOTOR_FULL_STEP_SEQ[motor_seq_idx]);
    _delay_ms(MOTOR_HOME_SETTLE_MS);
    motor_step_pos = 0;
    motor_release();
}

void motor_release(void) {
    PORTB &= 0xF0;
}

void motor_zero_soft(void) {
    motor_step_pos = 0;
    motor_seq_idx = 0;
}

void motor_step_cw(void) {
    motor_seq_idx = (motor_seq_idx + 1) & 0x03;
    motor_apply(MOTOR_FULL_STEP_SEQ[motor_seq_idx]);
    _delay_ms(MOTOR_STEP_DELAY_MS);
    motor_release();
    _delay_ms(MOTOR_STEP_DELAY_MS);
    motor_step_pos++;
}

void motor_step_ccw(void) {
    motor_seq_idx = (motor_seq_idx + 3) & 0x03;
    motor_apply(MOTOR_FULL_STEP_SEQ[motor_seq_idx]);
    _delay_ms(MOTOR_STEP_DELAY_MS);
    motor_release();
    _delay_ms(MOTOR_STEP_DELAY_MS);
    motor_step_pos--;
}

int16_t motor_get_step_position(void) {
    return motor_step_pos;
}

uint16_t motor_get_angle_deg(void) {
    int32_t wrapped = motor_step_pos % (int32_t)MOTOR_STEPS_PER_REV;

    if (wrapped < 0) {
        wrapped += MOTOR_STEPS_PER_REV;
    }

    return (uint16_t)((wrapped * 360L) / MOTOR_STEPS_PER_REV);
}

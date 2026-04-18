#ifndef EP391_MOTOR_H
#define EP391_MOTOR_H

#include <stdint.h>

/* Calibrated on the project motor:
 *   wave/full step = 200 steps/rev
 *   half step      = 400 steps/rev
 *
 * motor.c currently uses the 4-state full-step sequence, so use 200 here.
 */
#define MOTOR_STEPS_PER_REV  200U

void motor_init(void);
void motor_home(void);
void motor_release(void);
void motor_zero_soft(void);

void motor_step_cw(void);
void motor_step_ccw(void);

int16_t  motor_get_step_position(void);
uint16_t motor_get_angle_deg(void);

#endif

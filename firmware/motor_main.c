/* EP391 project motor bring-up
 * ATmega644PA @ 1 MHz
 *
 * Purpose:
 *   1. Prove the microcontroller can drive the Lab 14 stepper through
 *      the H-bridge on PB0-PB3.
 *   2. Count steps and convert them to a 0-359 degree angle.
 *   3. Provide a clean bench-test scaffold before LED beamforming is
 *      integrated into the final telemetry firmware.
 *
 * Serial output (same FT232 hookup as Labs 12/15):
 *   STEP,ANGLE,ADC0,VMV\r\n
 *
 * Optional serial commands if FT232 TX is also wired to PD0:
 *   a  auto-sweep around zero (default)
 *   f  one clockwise step
 *   b  one counterclockwise step
 *   F  run clockwise continuously
 *   B  run counterclockwise continuously
 *   g90 / go 90  move to absolute angle 90 deg
 *   h  physical home to step 0
 *   r  zero the software counter without moving
 *   s  stop / idle
 *   o  de-energize the coils
 *   ?  print help
 */

#include <avr/io.h>
#include <util/delay.h>
#include <stdio.h>
#include <stdint.h>

#include "motor.h"

#define BAUD      9600UL
#define UBRR_VAL  ((F_CPU / (8UL * BAUD)) - 1)

#define LED_ADC_CH         0U   /* PA0 / pin 40 */
#define UART_CMD_BUF_LEN   16
#define IDLE_POLL_MS       10
#define IDLE_REPORT_MS     500
#define SWEEP_SPAN_STEPS   ((int16_t)(MOTOR_STEPS_PER_REV / 4U))

typedef enum {
    MODE_IDLE = 0,
    MODE_SWEEP,
    MODE_RUN_CW,
    MODE_RUN_CCW
} motor_mode_t;

static void handle_command(char cmd, motor_mode_t *mode, int8_t *sweep_dir);

static void adc_init(void) {
    ADMUX  = (1 << REFS0);                     /* AVCC ref, right aligned */
    ADCSRA = (1 << ADEN) | (1 << ADSC)
           | (1 << ADPS1) | (1 << ADPS0);      /* prescaler /8 @ 1 MHz */
    while (ADCSRA & (1 << ADSC));
}

static uint16_t analog(uint8_t ch) {
    ADMUX = (ADMUX & 0xE0) | (ch & 0x1F);
    ADCSRA |= (1 << ADSC);
    while (ADCSRA & (1 << ADSC)) {
    }

    {
        uint16_t r = ADCL;
        r |= (uint16_t)ADCH << 8;
        return r;
    }
}

static uint16_t adc_to_millivolts(uint16_t counts) {
    return (uint16_t)(((uint32_t)counts * 5000UL) / 1023UL);
}

static void uart_init(void) {
    UBRR0H = (uint8_t)(UBRR_VAL >> 8);
    UBRR0L = (uint8_t)(UBRR_VAL);
    UCSR0A |= (1 << U2X0);
    UCSR0B  = (1 << TXEN0) | (1 << RXEN0);
    UCSR0C  = (1 << UCSZ01) | (1 << UCSZ00);
}

static void uart_putc(char c) {
    while (!(UCSR0A & (1 << UDRE0)));
    UDR0 = c;
}

static void uart_puts(const char *s) {
    while (*s) {
        uart_putc(*s++);
    }
}

static uint8_t uart_available(void) {
    return (UCSR0A & (1 << RXC0)) ? 1 : 0;
}

static char uart_getc(void) {
    return (char)UDR0;
}

static void report(void) {
    char buf[48];
    uint16_t adc0 = analog(LED_ADC_CH);
    uint16_t mv = adc_to_millivolts(adc0);

    snprintf(buf, sizeof(buf), "%d,%u,%u,%u\r\n",
             motor_get_step_position(), motor_get_angle_deg(), adc0, mv);
    uart_puts(buf);
}

static void print_help(void) {
    uart_puts("# cmds: a=sweep f=step-cw b=step-ccw F=run-cw "
              "B=run-ccw g90=goto-90 h=home r=zero s=stop "
              "o=off ?=help\r\n");
}

static uint16_t current_step_wrapped(void) {
    int32_t step = motor_get_step_position() % (int32_t)MOTOR_STEPS_PER_REV;

    if (step < 0) {
        step += MOTOR_STEPS_PER_REV;
    }
    return (uint16_t)step;
}

static uint16_t angle_to_step(uint16_t deg) {
    uint32_t scaled = (uint32_t)deg * (uint32_t)MOTOR_STEPS_PER_REV;
    return (uint16_t)((scaled + 180UL) / 360UL) % MOTOR_STEPS_PER_REV;
}

static void goto_angle(uint16_t target_deg) {
    uint16_t target_step = angle_to_step(target_deg);
    int16_t delta = (int16_t)target_step - (int16_t)current_step_wrapped();
    char buf[64];

    if (delta > (int16_t)(MOTOR_STEPS_PER_REV / 2U)) {
        delta -= (int16_t)MOTOR_STEPS_PER_REV;
    } else if (delta < -(int16_t)(MOTOR_STEPS_PER_REV / 2U)) {
        delta += (int16_t)MOTOR_STEPS_PER_REV;
    }

    snprintf(buf, sizeof(buf), "# goto %u deg -> step %u\r\n",
             target_deg, target_step);
    uart_puts(buf);

    while (delta > 0) {
        motor_step_cw();
        report();
        delta--;
    }

    while (delta < 0) {
        motor_step_ccw();
        report();
        delta++;
    }

    report();
}

static uint8_t parse_angle_command(const char *line, uint16_t *deg_out) {
    const char *p = line;
    uint16_t value = 0;

    while (*p == ' ') {
        p++;
    }

    if (*p == 'g' || *p == 'G') {
        p++;
        if (*p == 'o' || *p == 'O') {
            p++;
        }
    }

    while (*p == ' ') {
        p++;
    }

    if (*p < '0' || *p > '9') {
        return 0;
    }

    while (*p >= '0' && *p <= '9') {
        value = (uint16_t)(value * 10U + (uint16_t)(*p - '0'));
        p++;
    }

    while (*p == ' ') {
        p++;
    }

    if (*p != '\0' || value > 359U) {
        return 0;
    }

    *deg_out = value;
    return 1;
}

static void handle_line_command(const char *line,
                                motor_mode_t *mode,
                                int8_t *sweep_dir) {
    uint16_t target_deg = 0;

    if (parse_angle_command(line, &target_deg)) {
        *mode = MODE_IDLE;
        goto_angle(target_deg);
        return;
    }

    if (line[0] != '\0' && line[1] == '\0') {
        handle_command(line[0], mode, sweep_dir);
        return;
    }

    uart_puts("# unknown cmd\r\n");
    print_help();
}

static void handle_command(char cmd, motor_mode_t *mode, int8_t *sweep_dir) {
    switch (cmd) {
        case 'a':
        case 'A':
            *mode = MODE_SWEEP;
            *sweep_dir = 1;
            uart_puts("# auto sweep\r\n");
            break;

        case 'f':
            *mode = MODE_IDLE;
            motor_step_cw();
            report();
            break;

        case 'b':
            *mode = MODE_IDLE;
            motor_step_ccw();
            report();
            break;

        case 'F':
            *mode = MODE_RUN_CW;
            uart_puts("# run cw\r\n");
            break;

        case 'B':
            *mode = MODE_RUN_CCW;
            uart_puts("# run ccw\r\n");
            break;

        case 'h':
        case 'H':
            *mode = MODE_IDLE;
            motor_home();
            uart_puts("# homed\r\n");
            report();
            break;

        case 'r':
        case 'R':
            *mode = MODE_IDLE;
            motor_zero_soft();
            uart_puts("# zeroed\r\n");
            report();
            break;

        case 's':
        case 'S':
            *mode = MODE_IDLE;
            motor_release();
            uart_puts("# stopped\r\n");
            report();
            break;

        case 'o':
        case 'O':
            *mode = MODE_IDLE;
            motor_release();
            uart_puts("# coils off\r\n");
            break;

        case '?':
            print_help();
            break;

        default:
            break;
    }
}

static void poll_commands(motor_mode_t *mode, int8_t *sweep_dir) {
    static char cmd_buf[UART_CMD_BUF_LEN];
    static uint8_t cmd_len = 0;

    while (uart_available()) {
        char c = uart_getc();

        if (c == '\r' || c == '\n') {
            if (cmd_len > 0) {
                cmd_buf[cmd_len] = '\0';
                handle_line_command(cmd_buf, mode, sweep_dir);
                cmd_len = 0;
            }
            continue;
        }

        if (c == 0x08 || c == 0x7F) {
            if (cmd_len > 0) {
                cmd_len--;
            }
            continue;
        }

        if (cmd_len == 0) {
            switch (c) {
                case 'a':
                case 'A':
                case 'f':
                case 'b':
                case 'F':
                case 'B':
                case 'h':
                case 'H':
                case 'r':
                case 'R':
                case 's':
                case 'S':
                case 'o':
                case 'O':
                case '?':
                    handle_command(c, mode, sweep_dir);
                    continue;

                default:
                    break;
            }
        }

        if (cmd_len < (UART_CMD_BUF_LEN - 1U)) {
            cmd_buf[cmd_len++] = c;
        } else {
            cmd_len = 0;
            uart_puts("# cmd too long\r\n");
        }
    }
}

static void idle_wait(motor_mode_t *mode, int8_t *sweep_dir) {
    uint16_t waited = 0;

    while (waited < IDLE_REPORT_MS) {
        poll_commands(mode, sweep_dir);
        if (*mode != MODE_IDLE) {
            return;
        }
        _delay_ms(IDLE_POLL_MS);
        waited += IDLE_POLL_MS;
    }
}

int main(void) {
    motor_mode_t mode = MODE_SWEEP;
    int8_t sweep_dir = 1;

    motor_init();
    uart_init();
    adc_init();

    uart_puts("# EP391 stepper motor bring-up\r\n");
    uart_puts("# fmt: STEP,ANGLE,ADC0,VMV\r\n");
    uart_puts("# adc0: PA0 / pin 40 / nominal 0-5000 mV only\r\n");
    uart_puts("# SPR=");
    {
        char sprbuf[8];
        snprintf(sprbuf, sizeof(sprbuf), "%u\r\n", MOTOR_STEPS_PER_REV);
        uart_puts(sprbuf);
    }
    print_help();

    motor_home();
    report();

    while (1) {
        poll_commands(&mode, &sweep_dir);

        switch (mode) {
            case MODE_SWEEP:
                if (sweep_dir > 0) {
                    motor_step_cw();
                    if (motor_get_step_position() >= SWEEP_SPAN_STEPS) {
                        sweep_dir = -1;
                    }
                } else {
                    motor_step_ccw();
                    if (motor_get_step_position() <= -SWEEP_SPAN_STEPS) {
                        sweep_dir = 1;
                    }
                }
                report();
                break;

            case MODE_RUN_CW:
                motor_step_cw();
                report();
                break;

            case MODE_RUN_CCW:
                motor_step_ccw();
                report();
                break;

            case MODE_IDLE:
            default:
                idle_wait(&mode, &sweep_dir);
                if (mode == MODE_IDLE) {
                    report();
                }
                break;
        }
    }

    return 0;
}

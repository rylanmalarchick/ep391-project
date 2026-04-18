/* EP391 Final Project — Telemetry + one-LED tracker
 * ATmega644PA @ 1 MHz
 *
 * Packet format (unchanged for ground-station compatibility):
 *     SEQ,VBAT,IBAT,ANGLE,T1,T2\r\n
 *
 * Tracking implementation used here:
 *   - One amplified LED sensor on PA0 / ADC0 / pin 40
 *   - Stepper motor via SN754410/L293D on PB0-PB3 through motor.c
 *   - Hill-climb while light is present
 *   - If the source is lost for 10 s, fall back to continuous sweep
 *
 * Packet field mapping in this tracking bring-up build:
 *   SEQ   : packet counter
 *   VBAT  : raw ADC counts on PA2 (power placeholder)
 *   IBAT  : raw ADC counts on PA3 (power placeholder)
 *   ANGLE : absolute motor angle 0..359 from the tracker
 *   T1    : raw LED sensor counts on PA0 (tracking debug)
 *   T2    : 0 placeholder during tracking bring-up
 *
 * Notes:
 *   - The packet format stays exactly the same, so reader.py, plot.py,
 *     and monitor.py continue to parse it without modification.
 *   - For tracking bring-up, run the dashboards with --raw if you want
 *     T1 to make sense visually, because it is the LED sensor here.
 *   - PA0 must stay in the 0-5 V range at the AVR pin. Do not overdrive it.
 */

#include <avr/io.h>
#include <util/delay.h>
#include <stdio.h>
#include <stdint.h>

#include "motor.h"

#define BAUD      9600UL
#define UBRR_VAL  ((F_CPU / (8UL * BAUD)) - 1)

#define CH_LED   0U
#define CH_VBAT  2U
#define CH_IBAT  3U

#define LED_AVG_SAMPLES         8U
#define LIGHT_DETECT_THRESHOLD  150U   /* ~0.73 V at AVCC = 5 V */
#define TRACK_REVERSE_MARGIN    8U
#define CONTROL_TICK_MS         100U
#define TELEMETRY_PERIOD_MS     1000U
#define LOST_TIMEOUT_MS         10000U
#define TRACK_STEP_TIME_MS      100U   /* must match motor.c step timing */

typedef enum {
    TRACK_STATE_TRACK = 0,
    TRACK_STATE_SWEEP = 1
} tracker_state_t;

static tracker_state_t tracker_state = TRACK_STATE_SWEEP;
static int8_t tracker_dir = 1;
static uint16_t tracker_led_raw = 0;
static uint16_t tracker_lost_ms = LOST_TIMEOUT_MS;

/* ---- UART ---- */
static void uart_write(const char *s) {
    while (*s) {
        while (!(UCSR0A & (1 << UDRE0))) {
        }
        UDR0 = *s++;
    }
}

static void uart_init(void) {
    UBRR0H = (uint8_t)(UBRR_VAL >> 8);
    UBRR0L = (uint8_t)(UBRR_VAL);
    UCSR0A |= (1 << U2X0);
    UCSR0B  = (1 << TXEN0) | (1 << RXEN0);
    UCSR0C  = (1 << UCSZ01) | (1 << UCSZ00);
}

/* ---- ADC ---- */
static void adc_init(void) {
    ADMUX  = (1 << REFS0);                      /* AVCC ref, right aligned */
    ADCSRA = (1 << ADEN) | (1 << ADSC)
           | (1 << ADPS1) | (1 << ADPS0);       /* /8 prescaler */
    while (ADCSRA & (1 << ADSC)) {
    }
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

static uint16_t analog_avg(uint8_t ch, uint8_t n) {
    uint32_t sum = 0;
    uint8_t i = 0;

    for (i = 0; i < n; i++) {
        sum += analog(ch);
    }
    return (uint16_t)(sum / n);
}

/* ---- Telemetry fields ---- */
static uint16_t get_vbat_raw(void) { return analog(CH_VBAT); }
static uint16_t get_ibat_raw(void) { return analog(CH_IBAT); }
static int16_t get_t1_raw(void)    { return (int16_t)tracker_led_raw; }
static int16_t get_t2_raw(void)    { return 0; }
static uint16_t get_angle_deg(void){ return motor_get_angle_deg(); }

/* ---- Tracker ---- */
static const char *tracker_state_name(tracker_state_t state) {
    return (state == TRACK_STATE_TRACK) ? "TRACK" : "SWEEP";
}

static void tracker_set_state(tracker_state_t next) {
    char buf[32];

    if (tracker_state == next) {
        return;
    }

    tracker_state = next;
    snprintf(buf, sizeof(buf), "# tracker %s\r\n", tracker_state_name(next));
    uart_write(buf);
}

static uint16_t led_signal_raw(void) {
    return analog_avg(CH_LED, LED_AVG_SAMPLES);
}

/* Returns approximate elapsed time in ms for this control tick. */
static uint16_t tracker_tick(void) {
    uint16_t before = led_signal_raw();
    uint16_t after = 0;
    uint16_t elapsed_ms = 0;

    tracker_led_raw = before;

    if (before >= LIGHT_DETECT_THRESHOLD) {
        tracker_lost_ms = 0;
        if (tracker_state == TRACK_STATE_SWEEP) {
            tracker_dir = 1;
            tracker_set_state(TRACK_STATE_TRACK);
        }
    } else if (tracker_lost_ms < LOST_TIMEOUT_MS) {
        tracker_lost_ms += CONTROL_TICK_MS;
        if (tracker_lost_ms >= LOST_TIMEOUT_MS) {
            tracker_set_state(TRACK_STATE_SWEEP);
        }
    }

    if (tracker_state == TRACK_STATE_SWEEP) {
        motor_step_cw();
        elapsed_ms += TRACK_STEP_TIME_MS;
        tracker_led_raw = led_signal_raw();
        return elapsed_ms;
    }

    if (before < LIGHT_DETECT_THRESHOLD) {
        return 0;
    }

    if (tracker_dir > 0) {
        motor_step_cw();
    } else {
        motor_step_ccw();
    }
    elapsed_ms += TRACK_STEP_TIME_MS;

    after = led_signal_raw();
    if ((uint16_t)(after + TRACK_REVERSE_MARGIN) < before) {
        if (tracker_dir > 0) {
            motor_step_ccw();
        } else {
            motor_step_cw();
        }
        elapsed_ms += TRACK_STEP_TIME_MS;
        tracker_dir = -tracker_dir;
        tracker_led_raw = before;
    } else {
        tracker_led_raw = after;
    }

    return elapsed_ms;
}

static void telemetry_boot_banner(void) {
    char buf[96];

    uart_write("# EP391 telemetry boot\r\n");
    uart_write("# fmt: SEQ,VBAT,IBAT,ANGLE,T1,T2\r\n");
    uart_write("# tracking: one LED on PA0 / pin 40\r\n");
    uart_write("# t1 = LED raw ADC0 counts, t2 = 0 placeholder\r\n");
    snprintf(buf, sizeof(buf),
             "# tracker cfg: threshold=%u lost_timeout_ms=%u steps_per_rev=%u\r\n",
             LIGHT_DETECT_THRESHOLD, LOST_TIMEOUT_MS, MOTOR_STEPS_PER_REV);
    uart_write(buf);
    snprintf(buf, sizeof(buf), "# tracker %s\r\n", tracker_state_name(tracker_state));
    uart_write(buf);
}

/* ---- Main ---- */
int main(void) {
    char buf[64];
    uint16_t seq = 0;
    uint16_t telemetry_ms = 0;

    uart_init();
    adc_init();
    motor_init();
    motor_home();

    tracker_led_raw = led_signal_raw();
    telemetry_boot_banner();

    while (1) {
        uint16_t elapsed_ms = tracker_tick();

        if (elapsed_ms == 0) {
            _delay_ms(CONTROL_TICK_MS);
            elapsed_ms = CONTROL_TICK_MS;
            tracker_led_raw = led_signal_raw();
        }

        telemetry_ms += elapsed_ms;
        while (telemetry_ms >= TELEMETRY_PERIOD_MS) {
            uint16_t vbat = get_vbat_raw();
            uint16_t ibat = get_ibat_raw();
            uint16_t angle = get_angle_deg();
            int16_t t1 = get_t1_raw();
            int16_t t2 = get_t2_raw();

            snprintf(buf, sizeof(buf),
                     "%u,%u,%u,%u,%d,%d\r\n",
                     seq, vbat, ibat, angle, t1, t2);
            uart_write(buf);

            seq++;
            telemetry_ms -= TELEMETRY_PERIOD_MS;
        }
    }

    return 0;
}

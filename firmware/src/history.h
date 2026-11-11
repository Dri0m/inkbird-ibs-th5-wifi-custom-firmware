#pragma once
#include <stdbool.h>
#include <stdint.h>

#define HISTORY_MAX 8u
#define HISTORY_RECORD_SIZE 17u

/* Samples and completed hours are RAM only. Times must be trusted UTC. */
void history_init(void);
void history_clock_changed(uint32_t old_utc, uint32_t new_utc, bool had_time);
void history_sample(uint32_t utc, bool valid, int16_t t, uint16_t rh);
/* Writes count, zero flags, and records; token identifies this snapshot. */
uint16_t history_snapshot(uint8_t *out, uint32_t *token, uint8_t max_records);
void history_ack(uint32_t token);

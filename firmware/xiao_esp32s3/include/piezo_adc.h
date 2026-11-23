#pragma once

void piezoAdcStart(uint32_t durationSeconds);
void piezoAdcStop();
void piezoAdcPoll();
// Stops DMA immediately without waiting for the next poll (e.g. USB disconnect).
void piezoAdcAbort(const char* reason);
bool piezoAdcIsRunning();

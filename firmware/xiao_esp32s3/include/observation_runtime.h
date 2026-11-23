#pragma once
// Observation loop glue: Reducer state -> pure Policy -> persisted intent ->
// SafetyGuard -> fixed-camera executor -> persisted result. Evidence profile only.

#ifdef BLL_EVIDENCE_STORE
void observationRecoverAfterBoot();   // close an intent left open by a reset (INTERRUPTED)
void observationAfterPassiveStored(); // one policy step after a newly committed candidate
void observationRun();                // "observe-run": the same step, operator initiated
void observationStatus();             // "observe-status": state, view and a read-only preview
void observationSetLight(const char* value);  // "observe-light ok|low|unknown" (RAM only)
void observationSetVision(const char* value); // "observe-vision invoke|sample": capture frame source (RAM only)
void observationPoll();               // loop hook: servo ramp/park and timed follow-up recheck
void observationBegin();              // setup hook: servo pin held LOW (no pulses)
void observationServoCommand(const char* line);  // "servo-*" operator commands
// nullptr when nothing runs or waits (no intent, scheduler idle, servo still, no follow-up
// armed, no park pending); otherwise a short reason. Used before log rotation (0.14.6).
const char* observationBusyReason();
#else
inline void observationRecoverAfterBoot() {}
inline void observationAfterPassiveStored() {}
inline void observationPoll() {}
inline void observationBegin() {}
#endif

#pragma once
// Low-frequency forest on the XIAO (reference only, 0.15.0): the PC bridge sends the 250 ms
// CH1/CH2 1-40 Hz RMS it computes from the AD3 ("lowfreq-rms"), the XIAO runs
// lowfreq_forest_core.h on them and answers each window. Its own arm motions (servo motion +
// settle, +-0.3 s; from the servo driver's own times, 0.15.1) are masked. Nothing is written
// to the Evidence log and the Observation Policy does not use the result.
//
//   lowfreq-begin <run8> <ad3_ms>     reset; ad3_ms = the AD3 sample time now (clock anchor)
//   lowfreq-anchor <ad3_ms>           refresh the anchor (no reset)
//   lowfreq-rms <t_ms> <ch1_nV> <ch2_nV>   one window (centre time on the AD3 axis)
//   lowfreq-end                       close the open span
// Replies: LFF_BEGIN / LFF_ANCHOR / LFF t=.. p=.. a=.. arm=..[ span=t0,t1,peak,n] / LFF_END / LFF_ERROR

#ifdef BLL_EVIDENCE_STORE
void lowfreqCommand(const char* line);
void lowfreqPoll();  // loop hook: records the XIAO's own servo motion intervals
#else
inline void lowfreqPoll() {}
#endif

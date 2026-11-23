// Shared state S and the constant tables of the review page (labels, colours, kinds).
// Split from review.html on 2026-11-14 without behaviour changes.

export const KIND={LARVA_START:'幼虫活動 開始',LARVA_END:'幼虫活動 終了',DIG_START:'人工刺激（掘削）開始',DIG_END:'人工刺激（掘削）終了',NOISE:'外乱（声・物音）',NOISE_START:'外乱（声・物音）開始',NOISE_END:'外乱（声・物音）終了',SYNC_TAP:'同期タップ'};
// Blind labelling: detector / model output hidden; labels saved meanwhile carry blind=true.
export const S={blind:(()=>{try{return localStorage.getItem('bll.review.blind')==='1'}catch(_){return false}})(),run:null,ov:null,cursor:0,wave:null,waveKey:'',timer:null,offset:0,spec:null,dspec:null,dspecKey:'',frameTimer:null,play:null,playWin:null,drawnOv:0,drawnDetail:0};
export const video=()=>S.ov&&S.ov.video&&S.ov.video.available?S.ov.video:null,audioOk=()=>!!(video()&&video().audio&&video().audio.available);
export const PAD={l:52,r:8,t:6,b:18};
export const POSE_COLOR={FRONT:'#d35400',TOP:'#1e8f6e',BACK:'#8e44ad',FIXED:'#555'},POSE_SHORT={FRONT:'F',TOP:'T',BACK:'B',FIXED:'X'};
export const BASIS={XIAO_RESULT_EVENT:'MCU',PC_FRAME_RECEIPT:'PC受信',ESTIMATED_EVEN_SPACING:'推定（等間隔）'};
// Ctrl+drag on any graph selects a time range; the menu saves START at its left edge and
// END at its right edge (two ordinary append-only labels). If END cannot be saved, START is
// retracted so no half range is left behind.
export const RANGE_KINDS={LARVA:['LARVA_START','LARVA_END','幼虫活動','b-larva'],DIG:['DIG_START','DIG_END','人工刺激（掘削）','b-dig'],NOISE:['NOISE_START','NOISE_END','外乱（声・物音）','b-noise']};
export const POINT_KINDS=[['NOISE','外乱（声・物音）','b-noise'],['SYNC_TAP','同期タップ','b-sync']];
// Drag the line of a saved 幼虫活動 / 人工刺激（掘削） START or END to move it. Saving appends a new
// label with replaces_seq (the old one stays in the file, hidden); the START stays before
// its END. Esc cancels the drag.
export const EDGE_KINDS={LARVA_START:['LARVA_START','LARVA_END'],LARVA_END:['LARVA_START','LARVA_END'],DIG_START:['DIG_START','DIG_END'],DIG_END:['DIG_START','DIG_END'],
 NOISE_START:['NOISE_START','NOISE_END'],NOISE_END:['NOISE_START','NOISE_END'],NOISE:null,SYNC_TAP:null};  // null: a point, moves freely

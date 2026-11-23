// Lightbox shared by the pages: click a captured image to see it large.
// Split from review.html on 2026-11-14 without behaviour changes.

// Lightbox: click a captured image to see it large. Esc / background / × closes, ← → steps
// through the same list. Small sensor images (240 px) are scaled with visible pixels so
// nothing is smoothed away. Display only.
import {orient} from './orient.js';
export const LB={el:null,items:[],i:0};
export function lbBuild(){if(LB.el)return LB.el;const d=document.createElement('div');d.className='lb';d.hidden=true;d.setAttribute('role','dialog');d.setAttribute('aria-modal','true');
 d.innerHTML='<button type="button" class="lbclose" aria-label="閉じる">×</button><img alt=""><div class="lbcap"></div><div class="lbbar"><button type="button" data-lb="-1">← 前</button><span class="lbn" style="color:#cfd9d2;align-self:center"></span><button type="button" data-lb="1">次 →</button><span class="lbact"></span><a class="lbopen" target="_blank" rel="noopener" style="color:#cfe5d8;align-self:center">別タブで開く</a></div>';
 d.addEventListener('click',e=>{if(e.target===d||e.target.classList.contains('lbclose'))lbClose();const s=e.target.dataset&&e.target.dataset.lb;if(s)lbShow(LB.i+(+s))});
 document.addEventListener('keydown',e=>{if(!LB.el||LB.el.hidden)return;e.stopImmediatePropagation();e.preventDefault();if(e.key==='Escape')lbClose();else if(e.key==='ArrowLeft')lbShow(LB.i-1);else if(e.key==='ArrowRight')lbShow(LB.i+1)},true);
 document.body.append(d);LB.el=d;return d}
export function lbShow(i){const n=LB.items.length;if(!n)return;LB.i=(i+n)%n;const it=LB.items[LB.i],d=LB.el,img=d.querySelector('img');
 img.style.imageRendering='auto';img.onload=()=>{img.style.imageRendering=img.naturalWidth&&img.naturalWidth<=320?'pixelated':'auto';if(img.naturalHeight)img.style.setProperty('--ar',img.naturalWidth/img.naturalHeight)};img.src=it.src;img.alt=it.caption||'';orient(img,it.pose);
 d.querySelector('.lbcap').textContent=it.caption||'';d.querySelector('.lbn').textContent=n>1?`${LB.i+1} / ${n}`:'';for(const b of d.querySelectorAll('[data-lb]'))b.hidden=n<2;
 d.querySelector('.lbopen').href=it.src;const act=d.querySelector('.lbact');act.replaceChildren();
 if(it.action){const b=document.createElement('button');b.type='button';b.textContent=it.action.label;b.onclick=()=>{lbClose();it.action.run()};act.append(b)}}
export function lightbox(items,index){items=(items||[]).filter(x=>x&&x.src);if(!items.length)return;lbBuild();LB.items=items;LB.el.hidden=false;lbShow(index||0)}
export function lbClose(){if(LB.el)LB.el.hidden=true}

// Execute the actual page script with local DOM/fetch mocks. No real annotation or serial writes.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
const WEB = path.join(__dirname, '..', 'web');

// The page is ES modules (host/web/notes). For this vm harness they become one strict classic
// script: dependencies first (depth-first over the imports), import lines dropped, `export `
// removed. Top-level names are unique across the modules, so page globals such as `state` and
// `showImage` stay reachable from the tests as before.
function pageScript() {
  const entry = html.match(/<script type="module" src="\/static\/([^"]+)"><\/script>/)[1];
  const seen = new Set(), parts = [];
  (function visit(file) {
    if (seen.has(file)) return;
    seen.add(file);
    const text = fs.readFileSync(file, 'utf8');
    for (const m of text.matchAll(/^import \{[^}]*\} from '([^']+)';$/gm)) visit(path.resolve(path.dirname(file), m[1]));
    parts.push(text.replace(/^import .*;$/gm, '').replace(/^export /gm, ''));
  })(path.join(WEB, entry));
  return '"use strict";\n' + parts.join('\n');
}
const script = pageScript();

function element() {
  return {value:'',textContent:'',style:{},children:[],hidden:false,disabled:false,
    classList:{toggle(){}},events:{},setAttribute(){},addEventListener(name,handler){this.events[name]=handler},focus(){this.focused=true},
    replaceChildren(){this.children=[]},append(...items){this.children.push(...items)},
    add(item){this.children.push(item)},getBoundingClientRect(){return {width:0,height:0}}};
}
function sample() {
  return {id:'fixture-image',session_id:'fixture-session',label:'unreviewed',image_url:'/fixture.jpg',
    image_file_path:'C:\\data\\sessions\\fixture-session\\fixture-image.jpg',
    metadata:{width:240,height:240,bytes:100,received_utc_pc:'2026-11-01T00:00:00Z'}};
}
async function page(initial=sample(), fail=false, extra={}) {
  const elements=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],element()]));
  let stored=structuredClone(initial); const posts=[],copies=[];
  const context=vm.createContext({
    document:{getElementById:id=>elements[id],createElement:element,body:element(),execCommand(){return true},
      querySelector:selector=>selector==='meta[name="bll-token"]'?{content:'fixture-token'}:null},
    window:{addEventListener(){},matchMedia(){return {matches:false}}},
    navigator:{clipboard:{writeText:async text=>copies.push(text)}},setTimeout(){},
    EventSource:class {addEventListener(){}},Option:function(text,value){this.text=text;this.value=value},
    fetch:async(path,options)=>{
      if(path==='/api/state')return {ok:true,json:async()=>({sessions:[{id:'fixture-session',purpose:'dataset',created:'2026-11-01T00:00:00Z'}],ports:[],samples:[structuredClone(stored)],...structuredClone(extra)})};
      assert.ok(path==='/api/annotate'||path==='/api/dataset-role'||path==='/api/case-assign'||path.startsWith('/api/session-'),path); posts.push({path,...JSON.parse(options.body)});
      if(path.startsWith('/api/session-'))return {ok:true,json:async()=>({session_id:posts.at(-1).session_id,deleted:path.endsWith('delete'),samples:3})};
      if(fail)return {ok:false,json:async()=>({error:'fixture save failure'})};
      const data=posts.at(-1);
      if(path==='/api/case-assign'){const to=data.case_id??stored.recorded_case_id;const moved=data.sample_ids.filter(id=>id===stored.id&&stored.case_id!==to);
        stored={...stored,case_id:to,case_corrected:to!==stored.recorded_case_id};return {ok:true,json:async()=>({moved,unchanged:[],case_id:data.case_id})}}
      stored=path==='/api/dataset-role'?{...stored,dataset_role:data.role}:
        {...stored,label:data.label,reviewer:data.reviewer,note:data.note,reviewed_at:'2026-11-01T01:00:00Z'};
      return {ok:true,json:async()=>structuredClone(stored)};
    }
  });
  vm.runInContext(script,context);
  await new Promise(resolve=>setImmediate(resolve));
  vm.runInContext('showImage(state.samples[0])',context);
  return {elements,context,posts,copies,saved:()=>stored};
}

test('reviewer is optional and label plus memo are saved without a name',async()=>{
  const p=await page();p.elements.label.value='larva_visible';p.elements.reviewNote.value='fixture note';
  await p.elements.annotateBtn.onclick();
  assert.equal(p.posts.length,1);assert.equal(p.posts[0].reviewer,'');
  assert.equal(p.saved().label,'larva_visible');assert.equal(p.saved().note,'fixture note');
  assert.match(p.elements.reviewStatus.textContent,/保存しました/);
});
test('saved label and note update gallery immediately and survive a fresh page',async()=>{
  const p=await page();p.elements.label.value='background';p.elements.reviewer.value='fixture-reviewer';p.elements.reviewNote.value='fixture annotation';
  await p.elements.annotateBtn.onclick();
  assert.equal(p.posts[0].label,'background');assert.equal(p.elements.imageTag.textContent,'幼虫が見えない');
  assert.equal(p.elements.gallery.children[0].children[0].children[1].textContent,'幼虫が見えない');
  assert.match(p.elements.reviewStatus.textContent,/保存しました/);
  const reload=await page(p.saved());assert.equal(reload.elements.label.value,'background');
  assert.equal(reload.elements.reviewNote.value,'fixture annotation');
  assert.equal(reload.elements.reviewer.value,'fixture-reviewer');
});
test('save failure stays visible locally, preserves edits, and re-enables save',async()=>{
  const p=await page(sample(),true);p.elements.label.value='uncertain';p.elements.reviewer.value='fixture-reviewer';p.elements.reviewNote.value='keep my draft';
  await p.elements.annotateBtn.onclick();
  assert.match(p.elements.reviewStatus.textContent,/fixture save failure/);
  assert.equal(p.elements.reviewNote.value,'keep my draft');assert.equal(p.elements.label.value,'uncertain');
  assert.equal(p.elements.annotateBtn.disabled,false);assert.equal(p.elements.annotateBtn.hidden,false);
  assert.equal(p.elements.gallery.children[0].children[0].children[1].textContent,'未確認');
});
test('opening another image clears the previous reviewer and memo',async()=>{
  const p=await page();p.elements.reviewer.value='other-reviewer';p.elements.reviewNote.value='other note';
  vm.runInContext('showImage(state.samples[0])',p.context);
  assert.equal(p.elements.reviewer.value,'');assert.equal(p.elements.reviewNote.value,'');
});
test('AI model output is shown separately from the human label and stays UNKNOWN pending evaluation',async()=>{
  const item=sample();item.metadata.model_inference={prediction:{status:'MODEL_OUTPUT_UNVERIFIED',top:{class_name:'VISIBLE_LARVA',score:73}}};
  const p=await page(item);
  const text=p.elements.modelResult.children.map(child=>child.textContent).join(' ');
  assert.match(text,/VISIBLE_LARVA|幼虫らしき対象あり/);
  assert.match(text,/UNKNOWN/);
  assert.equal(p.elements.label.value,'unreviewed');
  assert.match(p.elements.imageModelOverlay.textContent,/VISIBLE_LARVA.*73\/100/);
  assert.equal(p.elements.gallery.children[0].children[0].children[0].children[1].textContent,'AI 73/100');
  const reload=await page(p.saved());
  assert.match(reload.elements.imageModelOverlay.textContent,/73\/100/);
  assert.equal(reload.elements.gallery.children[0].children[0].children[0].children[1].textContent,'AI 73/100');
});
test('choosing a label saves immediately, in order, without the save button',async()=>{
  const p=await page();
  p.elements.label.value='larva_visible';const first=p.elements.label.events.change();
  p.elements.label.value='background';const second=p.elements.label.events.change();
  await first;await second;
  assert.deepEqual(p.posts.map(x=>[x.path,x.label]),[['/api/annotate','larva_visible'],['/api/annotate','background']]);
  assert.equal(p.saved().label,'background');
  assert.match(p.elements.reviewStatus.textContent,/保存しました/);
  assert.equal(p.elements.gallery.children[0].children[0].children[1].textContent,'幼虫が見えない');
});
test('unchanged fields do not append annotation history',async()=>{
  const p=await page();
  await p.elements.reviewNote.events.change();await p.elements.label.events.change();
  assert.equal(p.posts.length,0);
  p.elements.reviewNote.value='memo';await p.elements.reviewNote.events.change();
  assert.equal(p.posts.length,1);assert.equal(p.posts[0].note,'memo');
});
test('a save keeps its own image even if another image is opened meanwhile',async()=>{
  const p=await page();
  p.elements.label.value='larva_visible';const pending=p.elements.label.events.change();
  vm.runInContext("selected={...state.samples[0],id:'other-image'}",p.context);
  await pending;
  assert.equal(p.posts[0].sample_id,'fixture-image');
});
test('evaluation-only is stored as a separate role and shown in the gallery',async()=>{
  const p=await page();
  p.elements.evalOnly.checked=true;await p.elements.evalOnly.events.change();
  assert.deepEqual(p.posts.map(x=>[x.path,x.role]),[['/api/dataset-role','evaluation_only']]);
  assert.equal(p.saved().label,'unreviewed');
  assert.match(p.elements.gallery.children[0].children[0].children[1].textContent,/評価専用/);
  const reload=await page(p.saved());assert.equal(reload.elements.evalOnly.checked,true);
  reload.elements.evalOnly.checked=true;await reload.elements.evalOnly.events.change();
  assert.equal(reload.posts.length,0);
});
const CASE_STATE={sessions:[{id:'fixture-session',purpose:'dataset',box_id:'test',created:'2026-11-01T00:00:00Z',title:''},
  {id:'loop-session',purpose:'dataset',box_id:'box-02',acquisition:'OBSERVATION_LOOP',camera_pose:'ARM',created:'2026-11-11T00:00:00Z',title:'11/11 観測ループ 1回目'}],
  cases:[{case_id:'test',display_name:'容器1',role:'training'},{case_id:'box-02',display_name:'',role:'evaluation'}],
  training_models:[{model:'v12-192',status:'OK',images:1}]};
test('session labels use the title and the case display name; switching sessions clears the old image',async()=>{
  const p=await page(sample(),false,CASE_STATE);
  const options=p.elements.sessionSelect.children.map(o=>o.text);
  assert.match(options[1],/^手動撮影 · 容器1（test） · /);
  assert.equal(options[2],'11/11 観測ループ 1回目 · box-02');
  assert.match(p.elements.sessionInfo.textContent,/手動撮影 \/ 容器1（test）/);
  assert.deepEqual(p.elements.sessionBadges.children.map(b=>b.textContent),['A データ収集（手動撮影）','ケース: 学習用']);
  assert.equal(p.elements.image.style.display,'block');
  p.elements.sessionSelect.value='loop-session';p.elements.sessionSelect.onchange();
  assert.equal(p.elements.image.style.display,'none');assert.equal(p.elements.imageInfo.textContent,'');
  assert.deepEqual(p.elements.sessionBadges.children.map(b=>b.textContent),['D 実地検証（観測ループ）','ケース: 評価用']);
  assert.equal(p.elements.sessionTitle.value,'11/11 観測ループ 1回目');
  assert.equal(p.elements.caseRows.children.length,2);
  assert.match(p.elements.trainingModels.textContent,/v12-192 1枚/);
});
test('trained images cannot be reserved for evaluation and say why',async()=>{
  const p=await page(sample(),false,{...CASE_STATE,trained:{'fixture-image':['v12-192']}});
  assert.equal(p.elements.evalOnly.disabled,true);
  assert.match(p.elements.roleNote.textContent,/v12-192 の学習に使いました/);
  assert.match(p.elements.gallery.children[0].children[0].children[1].textContent,/学習済み/);
});
test('a session title is saved without touching the session record',async()=>{
  const p=await page(sample(),false,CASE_STATE);
  vm.runInContext("globalThis.sent=[];const kept=state;api=async(path,data)=>{sent.push([path,data]);return path==='/api/state'?kept:{}}",p.context);
  p.elements.sessionTitle.value='容器1 初日';await p.elements.sessionTitleBtn.onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('sent[0]',p.context))),['/api/session-title',{session_id:'fixture-session',title:'容器1 初日',editor:''}]);
  assert.match(p.elements.message.textContent,/セッション名を保存しました/);
});
test('each saved image copies its absolute file path',async()=>{
  const p=await page();const card=p.elements.gallery.children[0],copyButton=card.children[1];
  await copyButton.events.click();
  assert.deepEqual(p.copies,['C:\\data\\sessions\\fixture-session\\fixture-image.jpg']);
  assert.equal(card.children[2].textContent,'パスをコピーしました');
  assert.equal(copyButton.textContent,'コピー済み');
});
test('unreviewed-only filter hides labelled images and keeps counts',async()=>{
  const labelled={...sample(),id:'fixture-labelled',label:'background',image_url:'/fixture2.jpg'};
  const p=await page(sample(),false,{samples:[sample(),labelled]});
  assert.equal(p.elements.gallery.children.length,2);
  assert.match(p.elements.count.textContent,/2枚（未確認 1枚）/);
  p.elements.unreviewedOnly.checked=true;p.elements.unreviewedOnly.events.change();
  assert.equal(p.elements.gallery.children.length,1);
  assert.equal(p.elements.gallery.children[0].children[0].children[1].textContent,'未確認');
  assert.match(p.elements.count.textContent,/未確認 1枚（全2枚）/);
  p.elements.label.value='larva_visible';await p.elements.annotateBtn.onclick();
  assert.equal(p.elements.gallery.children.length,0);
  assert.equal(p.elements.empty.hidden,false);
  assert.match(p.elements.empty.textContent,/未確認の画像はありません/);
});
test('session filter lists only sessions with unreviewed images and keeps the current one',async()=>{
  const sessions=[{id:'fixture-session',purpose:'dataset',created:'2026-11-01T00:00:00Z'},{id:'done-session',purpose:'dataset',created:'2026-11-02T00:00:00Z'}];
  const done={...sample(),id:'done-image',session_id:'done-session',label:'background'};
  const p=await page(sample(),false,{sessions,samples:[sample(),done]});
  const values=()=>p.elements.sessionSelect.children.map(o=>o.value);
  assert.deepEqual(values(),['','fixture-session','done-session']);
  assert.match(p.elements.sessionSelect.children[1].text,/（未確認 1枚）$/);
  p.elements.sessionUnreviewedOnly.checked=true;p.elements.sessionUnreviewedOnly.events.change();
  assert.deepEqual(values(),['','fixture-session']);
  // labelling the last unreviewed image keeps the session selected instead of jumping away
  p.elements.label.value='larva_visible';await p.elements.annotateBtn.onclick();
  assert.deepEqual(values(),['','fixture-session']);
  assert.equal(p.elements.sessionSelect.value,'fixture-session');
  assert.doesNotMatch(p.elements.sessionSelect.children[1].text,/未確認/);
});

test('images move to a new case with a reason; ticks choose them and the original case stays visible',async()=>{
  const p=await page({...sample(),case_id:'box-01',recorded_case_id:'box-01',case_corrected:false});
  assert.match(p.elements.moveScope.textContent,/1 枚すべて/);
  assert.equal(p.elements.moveBackBtn.disabled,true);
  p.elements.moveCase.value='';await p.elements.moveBtn.onclick();
  assert.equal(p.posts.length,0);assert.match(p.elements.moveStatus.textContent,/ケースID/);
  const tick=p.elements.gallery.children[0].children[3];assert.equal(tick.type,'checkbox');
  tick.checked=true;tick.events.change();assert.match(p.elements.moveScope.textContent,/チェックした 1 枚/);
  p.elements.moveCase.value=' box-02 ';p.elements.moveReason.value='取り違え';await p.elements.moveBtn.onclick();
  assert.deepEqual(p.posts.at(-1),{path:'/api/case-assign',sample_ids:['fixture-image'],case_id:'box-02',reason:'取り違え',editor:''});
  assert.match(p.elements.moveStatus.textContent,/1 枚を box-02 へ移しました/);
  assert.match(p.elements.moveStatus.textContent,/新しいケース box-02 の役割/);
  assert.match(p.elements.imageInfo.textContent,/ケース：box-02（撮影時 box-01 から訂正）/);
  assert.match(p.elements.gallery.children[0].children[0].children[1].textContent,/→box-02/);
  assert.equal(p.elements.moveBackBtn.disabled,false);
  await p.elements.moveBackBtn.onclick();
  assert.equal(p.posts.at(-1).case_id,null);assert.match(p.elements.moveStatus.textContent,/撮影時のケースに戻しました/);
});

test('a session goes to the trash with a reason and comes back from the trash list',async()=>{
  const p=await page(sample(),false,{deleted_sessions:[{id:'old-session',purpose:'dataset',box_id:'box-01',created:'2026-11-01T00:00:00Z',
    samples:3,deleted_at:'2026-11-18T00:00:00Z',delete_reason:'テスト撮影'}]});
  assert.match(p.elements.deleteScope.textContent,/画像 1 枚/);assert.equal(p.elements.deleteBtn.disabled,false);
  assert.equal(p.elements.trashPanel.hidden,false);assert.equal(p.elements.trashCount.textContent,'1');
  const row=p.elements.trashRows.children[0];assert.match(row.children[0].textContent,/画像 3 枚.*テスト撮影/);
  await row.children[1].events.click();
  assert.deepEqual(p.posts.at(-1),{path:'/api/session-restore',session_id:'old-session',editor:''});
  assert.match(p.elements.deleteStatus.textContent,/元に戻しました（画像 3 枚/);
  p.elements.deleteReason.value=' ピンぼけ ';await p.elements.deleteBtn.onclick();
  assert.deepEqual(p.posts.at(-1),{path:'/api/session-delete',session_id:'fixture-session',reason:'ピンぼけ',editor:''});
  assert.match(p.elements.deleteStatus.textContent,/画像 1 枚）を削除しました。.*元に戻せます/);
});

import {availableDomainRows, domainRowRewardSources, scoreGame, type Effect, type GameAction, type GameEvent, type GameState} from '../core';

export interface SettlementTransition { before:GameState; after:GameState; action:GameAction; events:GameEvent[] }
export interface CheckSettlement { version:1; steps:string[]; summary:string[]; hints:string[] }
const names:Record<string,string> = {coins:'钱', seals:'家纹', food:'食物', iron:'铁', pearl:'珍珠', influence:'影响力', points:'即时分'};
const colors:Record<string,string> = {black:'黑',white:'白',coral:'珊瑚'};
const members:Record<string,string> = {courtier:'家臣',gardener:'园丁',warrior:'武士',recruit:'招募家臣',promote:'晋升家臣'};


function effectText(e:Effect):string {
  switch(e.type) {
    case 'gain': return `${names[e.resource]}+${e.amount}`;
    case 'gainChoice': return `任选${e.amount}次资源（${e.resources.map(x=>names[x]).join('/')}，每次+1）`;
    case 'gainPoints': return `即时分+${e.amount}`;
    case 'influence': return `影响力+${e.amount}`;
    case 'majorAction': return `${members[e.action]}行动`;
    case 'lantern': return '灯笼奖励';
    case 'wellAction': return '水井奖励';
    case 'pay': return `${e.optional?'可选：':''}支付${e.amount}${names[e.resource]}→${e.effects.map(effectText).join('、')}`;
    case 'domainAction': return '选择个人工位奖励';
    case 'castleTileAction': return `选择城堡${e.color==='light'?'浅色':e.color==='any'?'任意':colors[e.color]+'色'}格奖励`;
    case 'gardenActivation': return '选择园丁奖励结算顺序';
    case 'actionOrder': return `依次结算（顺序可选）：${e.groups.map(g=>g.effects.map(effectText).join('、')).join('、')}`;
    case 'effectOrder': return `依次结算（顺序可选）：${e.effects.map(effectText).join('、')}`;
    case 'daimyoReward': return '选择顶层奖励';
    case 'castleRefresh': return '更新城堡卡牌';
    case 'chooseOne': return `任选：${e.options.map(option=>option.map(effectText).join('、')).join(' / ')}`;
  }
}
function addedEffects(before:Effect[],after:Effect[]):Effect[] {
  const remaining=before.map(e=>JSON.stringify(e));
  return after.filter(e=>{const i=remaining.indexOf(JSON.stringify(e));if(i<0)return true;remaining.splice(i,1);return false;});
}
export function warriorFactors(state:GameState,seat:number) {
  const player=state.players[seat];
  const courtiers=player.members.filter(m=>m.type==='courtier' && (m.location.startsWith('steward-')||m.location.startsWith('diplomat-')||m.location==='daimyo')).length;
  const yards=state.trainingYards.filter(y=>y.warriors.includes(seat)).map(y=>({id:y.id,count:y.warriors.filter(p=>p===seat).length,value:y.warriorValue}));
  return {courtiers,value:yards.reduce((n,y)=>n+y.count*y.value,0),yards};
}
function warriorScoringText(before:ReturnType<typeof warriorFactors>,after:ReturnType<typeof warriorFactors>):string {
  const value=(a:number,b:number)=>a===b?`${b}`:`${a}→${b}`;
  return `武士价值和（系数）${value(before.value,after.value)}，城内家臣系数${value(before.courtiers,after.courtiers)}。\n   当前武士计分：${after.value}×${after.courtiers}＝${after.value*after.courtiers}`;
}
function floorScore(state:GameState,seat:number):string {
  const locations=state.players[seat].members.filter(m=>m.type==='courtier').map(m=>m.location);
  return [['城门',locations.filter(x=>x==='gate').length,1],['1层',locations.filter(x=>x.startsWith('steward-')).length,3],['2层',locations.filter(x=>x.startsWith('diplomat-')).length,6],['3层',locations.filter(x=>x==='daimyo').length,10]]
    .filter(([,n])=>Number(n)>0).map(([label,n,value])=>`${label}${n}人×${value}`).join('＋')||'0';
}
function values(state:GameState,seat:number):Record<string,number> {
  const p=state.players[seat];return {...p.resources,influence:p.influence,points:p.points};
}
type BalanceKey = 'coins'|'seals'|'food'|'iron'|'pearl'|'influence'|'points';
interface NumericFlow { kind:'cost'|'gain'; key:BalanceKey; from:number; to:number; amount:number; origin:string; overflow?:number }
interface RewardFlow { kind:'gain'; text:string; origin:string }
type Flow = NumericFlow|RewardFlow;
type MemberMoved = Extract<GameEvent,{type:'MemberMoved'}>;
interface Group { trace:SettlementTransition[]; before:GameState; after:GameState; start:number; end:number; automatic:boolean }
const internal = new Set(['beginMajorAction','selectMajorActionSource','selectMajorActionTarget','cancelMajorActionSelection']);

function originText(source:string):string {
  if(source==='well')return '水井奖励';
  if(source==='lantern'||source.endsWith('-lantern'))return '灯笼奖励';
  // A domain source is inherited by nested member rewards. It identifies the
  // chain's entry, not the direct reward; label the row at its activation step.
  return '';
}

/** Preserve ordered payments and gains, including zero-net exchanges, from clone events. */
export function flowLedger(trace:SettlementTransition[],seat:number):NumericFlow[] {
  const flows:NumericFlow[]=[];
  let wellSource = '';
  for(const t of trace) {
    const current=values(t.before,seat);
    for(const [index,event] of t.events.entries()) {
      if(!('player' in event)||event.player!==seat)continue;
      // A queued well reward can resolve after a nested member action. Keep
      // its engine source so that grouping cannot credit the nested target.
      if(event.type==='EffectResolved'&&event.effect.type==='wellAction')wellSource=event.source;
      let key:BalanceKey,amount:number,total:number,source='';
      if(event.type==='ResourceChanged'){key=event.resource;amount=event.amount;total=event.total;}
      else if(event.type==='InfluenceChanged'){key='influence';amount=event.amount;total=event.total;}
      else if(event.type==='EffectResolved'&&event.effect.type==='gainPoints') {
        key='points';amount=event.effect.amount;total=current.points+amount;source=event.source;
      } else continue;
      if(current[key]+amount!==total)throw new Error(`Settlement event balance mismatch: ${key}`);
      const next=t.events[index+1];
      if(!source&&next?.type==='EffectResolved'&&next.player===seat)source=next.source;
      const origin=source==='well'&&wellSource&&wellSource!=='well'
        ? `${wellSource}触发的水井奖励` : originText(source);
      // The engine records both the applied delta and the printed gain. A
      // capped reward is still a resolved choice, including a zero gain.
      const printedGain=event.type==='ResourceChanged'&&next?.type==='EffectResolved'
        &&next.player===seat&&next.effect.type==='gain'&&next.effect.resource===key
        ? next.effect.amount : amount;
      const overflow=Math.max(0,printedGain-amount);
      if(amount!==0||overflow)flows.push({kind:amount<0?'cost':'gain',key,from:current[key],to:total,amount,origin,...(overflow?{overflow}:{})});
      current[key]=total;
    }
    const after=values(t.after,seat);
    if(Object.keys(current).some(key=>current[key]!==after[key]))throw new Error('Settlement balance changed without a rendered event');
  }
  return flows;
}

function groupTransitions(trace:SettlementTransition[]):Group[] {
  const groups:Group[]=[];let index=0;
  for(const t of trace) {
    if(internal.has(t.action.type))continue;
    const automatic=t.action.type==='finishResolution';if(!automatic)index++;
    const previous=groups.at(-1),previousAction=previous?.trace[0].action;
    const exchange=t.action.type==='exchangeSeal'&&previousAction?.type==='exchangeSeal'&&previousAction.receive===t.action.receive;
    const continuation=['chooseEffectOption','selectCastleTileAction','refreshCastleRoom'].includes(t.action.type)&&previous&&!previous.automatic&&previousAction?.type!=='draftDie'&&previousAction?.type!=='exchangeSeal';
    if(previous&&(exchange||continuation)){previous.trace.push(t);previous.after=t.after;previous.end=index;}
    else groups.push({trace:[t],before:t.before,after:t.after,start:index,end:index,automatic});
  }
  return groups;
}

function labelFor(group:Group,seat:number):string {
  const t=group.trace[0],action=t.action;
  if(action.type==='draftDie') {
    const event=t.events.find(e=>e.type==='DieDrafted'&&e.player===seat);
    if(event?.type==='DieDrafted')return `选择${colors[event.bridge]}桥${event.end==='right'?'右':'左'}端${event.die.value}点骰`;
  }
  if(action.type==='placeDie') {
    const row=action.workspace.match(/^p[0-9]+-domain-(courtier|gardener|warrior)$/)?.[1];
    return row ? `激活个人骰子工位（${members[row]}队列行，row=${row}）` : `执行骰子放置效果（${action.workspace}）`;
  }
  if(action.type==='exchangeSeal')return '兑换行动';
  if(action.type==='confirmMajorAction') {
    const moved=t.events.find(e=>e.type==='MemberMoved'&&e.player===seat);
    if(moved?.type==='MemberMoved') {
      const member=t.after.players[seat].members.find(m=>m.id===moved.member);
      const mode=t.before.actionFlow?.mode;
      if(mode==='promote')return `执行晋升家臣行动（${moved.from}→${moved.to}，移动1名家臣）`;
      return `执行${members[mode??'']??'成员'}行动（${moved.to}，放入1名${members[member?.type??'']??'成员'}）`;
    }
  }
  if(action.type==='chooseStartingPair')return `选择开局组合（offer=${action.offer}）`;
  if(t.before.phase==='setup'&&action.type==='chooseEffectOption')return '补选开局资源';
  if(action.type==='finishMajorAction')return '结束可选行动';
  if(action.type==='finishResolution')return group.trace.some(t=>t.events.some(e=>e.type==='RoundEnded'))?'轮末结算':'结束本次行动';
  return '执行所选奖励';
}

function renderFlowPhases(flows:Flow[]):string[] {
  const phases:{kind:'cost'|'gain';items:Flow[]}[]=[];
  for(const flow of flows) {
    let phase=phases.at(-1);
    if(!phase||phase.kind!==flow.kind){phase={kind:flow.kind,items:[]};phases.push(phase);}
    const last=phase.items.at(-1);
    if(last&&'key' in last&&'key' in flow&&!last.overflow&&!flow.overflow&&last.key===flow.key&&last.origin===flow.origin&&last.to===flow.from)last.to=flow.to;
    else phase.items.push({...flow});
  }
  return phases.map(phase=>{
    const buckets:{origin:string;texts:string[]}[]=[];
    for(const flow of phase.items) {
      let bucket=buckets.at(-1);
      if(!bucket||bucket.origin!==flow.origin){bucket={origin:flow.origin,texts:[]};buckets.push(bucket);}
      bucket.texts.push('text' in flow?flow.text:`${names[flow.key]}${flow.from}→${flow.to}${flow.overflow?`（上限${flow.to}，未获得${flow.overflow}）`:''}`);
    }
    const body=buckets.map(b=>b.origin?`${b.origin}（${b.texts.join('、')}）`:b.texts.join('、')).join('；');
    return `${phase.kind==='cost'?'花费':'获得'}：${body}。`;
  });
}

function scoreLines(before:GameState,after:GameState,seat:number,moved:MemberMoved[]):string[] {
  const a=scoreGame(before)[seat],b=scoreGame(after)[seat],lines:string[]=[];
  const courtier=moved.some(e=>after.players[seat].members.find(m=>m.id===e.member)?.type==='courtier');
  if(courtier) {
    const onlyRecruit=moved.every(e=>e.from==='domain'&&e.to==='gate');
    lines.push(`家臣终局分${a.courtiers}→${b.courtiers}${onlyRecruit?'':`；当前家臣计分：${floorScore(after,seat)}＝${b.courtiers}`}。`);
  }
  const w0=warriorFactors(before,seat),w1=warriorFactors(after,seat);
  if(courtier||w0.value!==w1.value||w0.courtiers!==w1.courtiers)lines.push(warriorScoringText(w0,w1)+'。');
  if(a.gardeners!==b.gardeners)lines.push(`园丁终局分${a.gardeners}→${b.gardeners}。`);
  return lines;
}

function constructionLines(before:GameState,after:GameState,seat:number):string[] {
  const lines:string[]=[],cardLines:string[]=[],lamp=addedEffects(before.players[seat].lanternEffects,after.players[seat].lanternEffects);
  const installed=before.phase==='setup'
    &&!before.startingOffers.some(offer=>offer.claimedBy===seat)
    &&after.startingOffers.some(offer=>offer.claimedBy===seat);
  const cardChanged=JSON.stringify(before.players[seat].domainCard)!==JSON.stringify(after.players[seat].domainCard);
  if(installed)lines.push(`灯笼区装入（以后触发灯笼时结算，本次不获得）：${after.players[seat].lanternEffects.map(effectText).join('、')||'无'}。`);
  else if(lamp.length)lines.push(`灯笼新增奖励：${lamp.map(effectText).join('、')}。`);
  for(const row of ['courtier','gardener','warrior'] as const) {
    const a=domainRowRewardSources(before,seat,row),b=domainRowRewardSources(after,seat,row);
    if(JSON.stringify(a.queue)!==JSON.stringify(b.queue)) {
      const gain=addedEffects(a.queue,b.queue),loss=addedEffects(b.queue,a.queue);
      if(gain.length&&!loss.length)lines.push(`个人${members[row]}队列行新增露出奖励位：${gain.map(effectText).join('、')}。`);
      else lines.push(`个人${members[row]}队列行露出奖励位：[${a.queue.map(effectText).join('、')||'无'}]→[${b.queue.map(effectText).join('、')||'无'}]。`);
    }
    if(cardChanged||installed)cardLines.push(`   ${members[row]}队列行：${b.card.map(effectText).join('、')||'无卡牌奖励'}。`);
  }
  if(cardLines.length)lines.push(installed?'个人工位卡装入后的奖励（后续触发，本次不执行）：':'个人工位卡替换后的奖励（后续触发）：',...cardLines);
  return lines;
}

function memberMoves(trace:SettlementTransition[],seat:number):MemberMoved[] {
  return trace.flatMap(t=>t.events).filter((e):e is MemberMoved=>e.type==='MemberMoved'&&e.player===seat);
}

function routeHints(initial:GameState,trace:SettlementTransition[],final:GameState):string[] {
  const seat=initial.currentPlayer,hints:string[]=[],used=new Set<string>();
  const gardens=new Map<string,GameState['gardens'][number]>();
  let roundState=initial;
  for(const t of trace) {
    if(t.after.round===initial.round)roundState=t.after;
    for(const moved of memberMoves([t],seat)) {
      const member=t.after.players[seat].members.find(m=>m.id===moved.member);
      if(member)used.add(member.type);
      const garden=t.after.gardens.find(g=>g.id===moved.to);
      if(garden)gardens.set(garden.id,garden);
    }
  }
  for(const [id,garden] of gardens) {
    const timing=initial.round===3?'第3轮不再触发轮末园丁奖励':`${colors[garden.bridge]}桥本轮${final.round>initial.round?'轮末时':'当前'}余${roundState.bridges[garden.bridge].length}骰；轮末如果仍有骰子，可以额外触发一次该园位奖励，不再支付放置园丁的食物费用；仅支付奖励本身明确要求的费用`;
    const payments=garden.effects.filter((e):e is Extract<Effect,{type:'pay'}>=>e.type==='pay');
    hints.push(`${id}：${timing}${initial.round<3&&payments.length?'；奖励含'+payments.map(effectText).join('、')+'，注意留存支付资源':''}。`);
  }
  if(used.has('courtier'))hints.push('前期关注灯笼奖励构筑；后期关注家臣自身终局分和为武士提供的系数。');
  if(used.has('warrior')||used.has('courtier'))hints.unshift('武士最终得分为武士价值和×城内家臣系数，城门家臣不计入。请关注终局得分，兼顾两个系数的提升。');
  return hints;
}

/** Formats authoritative clone transitions without choosing actions or mutating game state. */
export function buildCheckSettlement(initial:GameState,trace:SettlementTransition[],final:GameState):CheckSettlement {
  const seat=initial.currentPlayer,allFlows=flowLedger(trace,seat),groups=groupTransitions(trace),steps:string[]=[];
  for(const [index,g] of groups.entries()) {
    const flows:Flow[]=flowLedger(g.trace,seat),detail:string[]=[],labels:string[]=[];
    const draft=g.trace[0].events.find(e=>e.type==='DieDrafted'&&e.player===seat);
    if(draft?.type==='DieDrafted')detail.push(draft.lanternTriggered?'获得灯笼奖励（按后续步骤结算）。':'不获得灯笼奖励。');
    for(const t of g.trace) {
      const action=t.action;if(action.type!=='chooseEffectOption')continue;
      const e=t.before.pendingEffects[0]?.effect;if(!e)continue;
      if(e.type==='chooseOne') {
        const selected=e.options[action.option],major=selected?.[0];
        if(!selected)continue;
        const majorChoices=e.options.flatMap(option=>option.length===1&&option[0].type==='majorAction'?[option[0]]:[]);
        labels.push(majorChoices.length===e.options.length&&major.type==='majorAction'?`在${majorChoices.map(o=>members[o.action]).join('／')}行动中选择${members[major.action]}`:`选择${selected.map(effectText).join('、')}`);
      } else if(e.type==='domainAction') {
        const row=availableDomainRows(t.before)[action.option];
        if(row)labels.push(`激活个人${members[row]}队列行奖励`);
      } else if(e.type==='actionOrder')labels.push(`先结算${e.groups[action.option].effects.map(effectText).join('、')}`);
      else if(e.type==='pay'&&action.option===1)labels.push(`跳过支付${e.amount}${names[e.resource]}及其奖励`);
    }
    const pending=g.after.pendingEffects[0];
    if(pending?.effect.type==='majorAction'&&!g.before.pendingEffects.some(p=>p.id===pending.id)) {
      const target=groups.slice(index+1).find(next=>next.before.pendingEffects[0]?.id===pending.id&&next.trace[0].action.type==='confirmMajorAction');
      flows.push({kind:'gain',origin:'',text:`${effectText(pending.effect)}${target?`（接第${target.start}步）`:''}`});
    }
    const moved=memberMoves(g.trace,seat);
    if(!flows.some(f=>f.kind==='cost'))detail.push('花费：无。');
    detail.push(...renderFlowPhases(flows));
    if(!flows.some(f=>f.kind==='gain')&&!moved.length)detail.push('获得：无。');
    const scoring=scoreLines(g.before,g.after,seat,moved),construction=constructionLines(g.before,g.after,seat);
    if(g.automatic&&!flows.length&&!scoring.length&&!construction.length)continue;
    detail.push(...scoring,...construction);
    const label=labelFor(g,seat)+(labels.length?'，'+labels.join('；'):'');
    const body=draft&&!flows.length&&!scoring.length&&!construction.length?'   '+detail.map(s=>s.replace(/。$/,'')).join('；')+'。':detail.map(s=>'   '+s).join('\n');
    const number=g.automatic?'':`${g.start===g.end?g.start:g.start+'–'+g.end}. `;
    steps.push(`${number}${label}：\n${body}`);
  }
  const a=values(initial,seat),b=values(final,seat),touched=new Set<string>(allFlows.map(f=>f.key));
  const balanceSummary=Object.keys(names).filter(k=>k!=='points'&&(a[k]!==b[k]||touched.has(k))).map(k=>`${names[k]}${a[k]}→${b[k]}`);
  const before=scoreGame(initial)[seat],after=scoreGame(final)[seat];
  const summary=[...(balanceSummary.length?[balanceSummary.join('；')+'。']:[]),...scoreLines(initial,final,seat,memberMoves(trace,seat))];
  const lost=new Map<BalanceKey,number>();
  for(const flow of allFlows)if(flow.overflow)lost.set(flow.key,(lost.get(flow.key)??0)+flow.overflow);
  if(lost.size)summary.push(`因上限未获得：${[...lost].map(([key,amount])=>`${names[key]}${amount}`).join('、')}。`);
  if(before.timeTrack!==after.timeTrack)summary.push(`影响力终局分${before.timeTrack}→${after.timeTrack}。`);
  const net=after.total-before.total;
  summary.push(...constructionLines(initial,final,seat));
  // Opening inventory is for the first development chain, not earned points.
  // Keep score facts in the engine outcome; ordinary turns still show all totals.
  if(initial.phase!=='setup')summary.push(`当前分数${before.duringGame}→${after.duringGame}。`,`终局分数${before.total-before.duringGame}→${after.total-after.duringGame}。`,`汇总${before.total}→${after.total}（总分净变化${net>0?'+':''}${net}；已合计当前分数与全部终局计分项）。`);
  if(initial.phase==='setup')summary.push(`执行后：${final.phase==='setup'?`由 P${final.currentPlayer} 完成开局选择`:`进入普通回合，由 P${final.currentPlayer} 首先取骰`}。`);
  return {version:1,steps,summary,hints:routeHints(initial,trace,final)};
}

export function renderCheckSettlement(value:CheckSettlement):string {
  return ['结算过程：',value.steps.join('\n\n'),'','路线汇总：',...value.summary,...(value.hints.length?['','提示：',...value.hints.map((x,i)=>`${i+1}. ${x}`)]:[])].join('\n');
}

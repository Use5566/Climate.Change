import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
const source=await fs.readFile(new URL('../dist/highlights.js',import.meta.url),'utf8');
const {toggleRanges,isCovered,excerpt,highlightLength,highlightsWithinLimit,installInferenceCopyGuard}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const r=(start,end,p=0)=>({p,start,end});
assert.deepEqual(toggleRanges([], [r(2,8)]),[r(2,8)]);
assert.deepEqual(toggleRanges([r(2,8)], [r(2,8)]),[]);
assert.deepEqual(toggleRanges([r(2,10)], [r(4,6)]),[r(2,4),r(6,10)]);
assert.deepEqual(toggleRanges([r(2,8)], [r(5,12)]),[r(2,12)]);
assert.deepEqual(toggleRanges([r(2,8)], [r(8,10)]),[r(2,10)]);
assert.deepEqual(toggleRanges([r(0,8),r(0,5,1)],[r(2,8),r(0,3,1)]),[r(0,2),r(3,5,1)]);
assert(isCovered([r(0,8)],[r(1,4)]));
assert.equal(excerpt('甲🌏乙丙',1,3),'🌏乙');
console.log('8 highlight assertions passed: exact toggle, partial removal, overlaps, adjacency, cross-paragraph, Unicode.');
assert.equal(highlightLength('甲，乙。「丙」！ \n'), 3);
assert.equal(highlightLength('ABC123🌏'), 7);
const paragraphs = [{text:'甲'.repeat(100)+'，。！'+'乙'}];
assert(highlightsWithinLimit([r(0,103)], paragraphs));
assert(!highlightsWithinLimit([r(0,104)], paragraphs));
assert(!highlightsWithinLimit([r(0,20),r(20,104)], paragraphs));
const handlers = {};
globalThis.document = {addEventListener: (type, handler) => {handlers[type]=handler;}};
let active = false;
installInferenceCopyGuard(() => active);
for (const type of ['copy','cut','dragstart','contextmenu']) {
  let prevented = false;
  const event = {preventDefault: () => {prevented = true;}};
  active = false; handlers[type](event); assert.equal(prevented,false);
  active = true; handlers[type](event); assert.equal(prevented,true);
}
for (const key of ['c','x']) {
  let prevented = false;
  handlers.keydown({key,ctrlKey:true,preventDefault:()=>{prevented=true;}});
  assert(prevented);
}
console.log('15 additional assertions passed: 100-character limits, punctuation, merging, and summary copy guards.');

const vm = await import('node:vm');
const chatSource = await fs.readFile(new URL('../dist/learning-ab.js',import.meta.url),'utf8');
const controlsSource = chatSource.slice(chatSource.indexOf('function pendingQuestion()'), chatSource.indexOf('let retryRequest=null;'));
const elements = {};
const context = {work:{messages:[]}, chatBusy:false, updateSelectionButton(){},
  $: key => elements[key] ??= {}};
vm.createContext(context);
vm.runInContext(controlsSource, context);
context.work.messages = Array.from({length:20},(_,i)=>({role:i%2 ? 'model':'user',text:'內容'}));
vm.runInContext('updateChatControls()', context);
assert.equal(elements['#send-message'].disabled,true);
assert.equal(elements['#question'].readOnly,true);
assert.equal(elements['#reading-done'].disabled,false);
assert(elements['#chat-count'].textContent.includes('10 / 10'));
context.work.messages.pop(); // Tenth question awaits retry.
vm.runInContext('updateChatControls()', context);
assert.equal(elements['#send-message'].disabled,false);
assert.equal(elements['#send-message'].textContent,'重試回覆');
context.work.messages.pop(); // Nine completed questions.
vm.runInContext('updateChatControls()', context);
assert.equal(elements['#question'].readOnly,false);
assert.equal(elements['#send-message'].disabled,false);
console.log('8 chat control assertions passed: tenth-turn limit and pending retry.');

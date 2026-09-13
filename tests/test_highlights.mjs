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
const paragraphs = [{text:'甲'.repeat(30)+'，。！'+'乙'}];
assert(highlightsWithinLimit([r(0,33)], paragraphs));
assert(!highlightsWithinLimit([r(0,34)], paragraphs));
assert(!highlightsWithinLimit([r(0,20),r(20,34)], paragraphs));
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
console.log('15 additional assertions passed: 30-character limits, punctuation, merging, and summary copy guards.');

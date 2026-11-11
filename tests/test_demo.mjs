import assert from 'node:assert/strict';
import {analyzeInput, codePointLength, samples} from '../demo/dist/data.js';
assert.equal(codePointLength('😀中a'),3);
assert.equal(analyzeInput('a'.repeat(15000)).length,15000);
assert.throws(()=>analyzeInput('a'.repeat(15001)));
assert.throws(()=>analyzeInput('  '));
for(const sample of Object.values(samples)){
  for(const feedback of sample.feedback)assert.ok(sample.text.includes(feedback.quote));
  assert.equal(sample.weights.reduce((a,b)=>a+b,0),100);
  if(sample.scores)assert.ok(sample.scores.every((v,i)=>v>=0&&v<=sample.weights[i]));
}
console.log('PASS: demo input boundaries, Unicode counts, sample citations and score ranges');

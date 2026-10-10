// stdin/stdout bridge keeps private draft text out of process arguments and logs.
const fs = require('node:fs');
const twitter = require('twitter-text');

function weight(text) { return twitter.parseTweet(text).weightedLength; }
function shorten(text, budget) {
  const cleaned = text.replace(/https?:\/\/\S+/g, '').replace(/[\r\n]+/g, ' ').trim();
  if (weight(cleaned) <= budget) return cleaned;
  const units = Array.from(new Intl.Segmenter('zh', { granularity: 'grapheme' }).segment(cleaned), x => x.segment);
  while (units.length && weight(units.join('') + '…') > budget) units.pop();
  return units.join('') + '…';
}

function execute(request) {
  if (request.operation === 'shorten') return request.texts.map(t => shorten(t, request.budget));
  if (request.operation === 'weights') return request.texts.map(weight);
  throw new Error('Unknown operation');
}
module.exports = { weight, shorten, execute };
if (require.main === module) {
  try { process.stdout.write(JSON.stringify(execute(JSON.parse(fs.readFileSync(0, 'utf8'))))); }
  catch { process.stderr.write('Text validation failed\n'); process.exitCode = 1; }
}

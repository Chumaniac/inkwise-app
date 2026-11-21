import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const read = name => readFile(new URL('../demo/dist/' + name, import.meta.url), 'utf8');
const [styles, theme, page, script] = await Promise.all([read('styles.css'), read('theme.css'), read('index.html'), read('app.js')]);
const withoutComments = styles.replace(/\/\*[\s\S]*?\*\//g, '');

// A colour written outside a token block would not follow the dark appearance.
const rules = withoutComments.replace(/:root[^{]*\{[^{}]*\}/g, '');
assert.deepEqual(rules.match(/#[0-9a-fA-F]{3,8}\b/g) ?? [], [], 'demo rules must take colours from tokens');

// Both ways of reaching the dark appearance define the same tokens with the same values.
const explicit = /:root\[data-theme=dark\]\{([^{}]*)\}/.exec(withoutComments)?.[1];
const system = /:root:not\(\[data-theme=light\]\)\{([^{}]*)\}/.exec(withoutComments)?.[1];
assert.ok(explicit && system, 'explicit and system dark blocks are both present');
assert.equal(explicit, system, 'the chosen and the system dark appearance must not drift apart');

// Every colour token the light theme defines has a dark value.
const names = text => new Set([...text.matchAll(/(--[a-z0-9-]+)\s*:/g)].map(match => match[1]));
const dark = names(explicit);
const colourTokens = [...names(theme)].filter(name => /^--(surface|text|brand|border|focus|success|warning|destructive|info)/.test(name));
assert.deepEqual(colourTokens.filter(name => !dark.has(name)), [], 'light colour tokens without a dark value');

// The toggle exists, is named for assistive technology, and the page stays within its CSP.
assert.match(page, /<button type="button" id="theme-toggle"[^>]*aria-label="[^"]+"/);
assert.match(page, /<meta name="color-scheme" content="light dark">/);
assert.doesNotMatch(page, /\sstyle="/, 'inline styles are blocked by the page CSP');
// Saving a report must not depend on a secure-context-only API.
assert.doesNotMatch(script.replace(/typeof crypto\.randomUUID==='function'\)return crypto\.randomUUID\(\)/, ''), /crypto\.randomUUID/);

console.log('PASS: demo colours are tokenised, dark values are complete and the toggle is wired');

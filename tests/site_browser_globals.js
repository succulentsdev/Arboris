// Runs the website inline script the way a browser does: no `module`,
// no `document`. A browser has neither, so any top-level code that only
// runs when `module` exists never runs there. Exits 0 and prints JSON
// when ArborisRoute is defined with its expected shape, else exits 1.
// Regression guard for the route-viz bug (2026-11-26): the whole
// ArborisRoute IIFE sat inside the `if(typeof module...)` block, so the
// strip never built in a real browser while every vm-based check passed.
const fs = require('fs'), vm = require('vm'), path = require('path');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'website', 'index.html'), 'utf8');
const src = html.split('<script>')[1].split('</script>')[0];
const ctx = {};
vm.createContext(ctx);
vm.runInContext(src, ctx);
const route = ctx.ArborisRoute;
const ok = typeof route === 'object' && route !== null
  && typeof route.createRouteSession === 'function'
  && typeof route.stepsFromMeta === 'function'
  && route.SWEEP_MS === 600;
const modelMeta = { shrink: { changed: true, saved_tokens_est: 5 }, routed_to_small: true, answer_score: 0.87, energy: { energy_wh: 0.017 } };
const modelSteps = route.stepsFromMeta(modelMeta, 0).map(s => s.name);
const localMeta = { shrink: { changed: false, saved_tokens_est: 0 }, local_answer: true, energy: { energy_wh: 0 } };
const localSteps = route.stepsFromMeta(localMeta, 0).map(s => s.name);
const stepsOk = JSON.stringify(modelSteps) === JSON.stringify(['PromptShrink', 'Model cascade', 'Quality check', 'Output cost'])
  && JSON.stringify(localSteps) === JSON.stringify(['PromptShrink', 'Zero-model gate', 'Output cost']);
console.log(JSON.stringify({ arborisRoute: typeof route, sweepMs: route && route.SWEEP_MS, modelSteps, localSteps }));
process.exit(ok && stepsOk ? 0 : 1);

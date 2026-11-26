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
  && route.SWEEP_MS === 450;
console.log(JSON.stringify({ arborisRoute: typeof route, sweepMs: route && route.SWEEP_MS }));
process.exit(ok ? 0 : 1);

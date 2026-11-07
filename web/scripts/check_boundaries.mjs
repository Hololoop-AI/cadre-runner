// The web app's import rules, checked (design gate G2). Each module under
// src/ is reached from outside only through its index.ts, and the
// dependency direction is fixed: ui -> api -> model, ui -> model; model
// depends on nothing. Prints one line per broken import, exits 1 if any.
//
//   node web/scripts/check_boundaries.mjs
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'src');

/** module -> what it may import: other modules (through their index) and npm packages. */
const RULES = {
  model: { modules: [], packages: [] },
  api: { modules: ['model'], packages: [] },
  ui: { modules: ['model', 'api'], packages: ['react', 'react-dom'] },
  '': { modules: ['ui'], packages: ['react', 'react-dom'] }, // src/main.tsx, the composition root
};
/** Test files may also use the test runner. */
const TEST_PACKAGES = ['vitest', '@testing-library/react', '@testing-library/dom'];

function files(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? files(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}

const moduleOf = (file) => {
  const rel = relative(SRC, file).split('/');
  return rel.length > 1 ? rel[0] : '';
};

const problems = [];
for (const file of files(SRC)) {
  const mod = moduleOf(file);
  const rule = RULES[mod];
  const where = relative(join(SRC, '..'), file);
  if (!rule) {
    problems.push(`${where}: src/${mod}/ is not a module in this script's RULES (add it, and to context/modules.md)`);
    continue;
  }
  const test = /\.test\.tsx?$/.test(file);
  const text = readFileSync(file, 'utf8');
  for (const m of text.matchAll(/^\s*(?:import|export)\s[^'"]*?from\s+['"]([^'"]+)['"]|^\s*import\s+['"]([^'"]+)['"]/gm)) {
    const spec = m[1] ?? m[2];
    if (!spec.startsWith('.')) {
      const pkg = spec.startsWith('@') ? spec.split('/').slice(0, 2).join('/') : spec.split('/')[0];
      if (!rule.packages.includes(pkg) && !(test && TEST_PACKAGES.includes(pkg))) {
        problems.push(`${where}: imports package ${spec}; ${mod || 'main'} may use ${rule.packages.join(', ') || 'no packages'}`);
      }
      continue;
    }
    const target = resolve(dirname(file), spec);
    const targetMod = relative(SRC, target).split('/')[0]; // './ui' from main.tsx is module ui
    if (targetMod === mod) continue;
    if (!rule.modules.includes(targetMod)) {
      problems.push(`${where}: imports ${spec} (module ${targetMod || 'main'}); ${mod || 'main'} may depend on ${rule.modules.join(', ') || 'nothing'}`);
    } else if (relative(join(SRC, targetMod), target) !== '' && !/\.css$/.test(spec)) {
      // (a stylesheet is not code: main.tsx may load ui/fleet.css directly)
      problems.push(`${where}: imports ${spec}, inside ${targetMod}/; import '${targetMod}' through its index.ts`);
    }
  }
}
for (const p of problems) console.log(p);
console.log(problems.length ? `${problems.length} broken import${problems.length > 1 ? 's' : ''}` : 'imports follow the module rules');
process.exit(problems.length ? 1 : 0);

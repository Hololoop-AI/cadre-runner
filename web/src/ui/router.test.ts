import { describe, expect, it } from 'vitest';
import { route } from './router';

// The same paths as tests/test_web_app.py: statusd serves the React app for
// exactly the paths this router draws.
const REACT_PATHS = ['/', '/library', '/costs', '/runs', '/tree', '/tree/task-a', '/project/cadre', '/page/abcd'];
const PYTHON_PATHS = ['/costs.json', '/view/abcd', '/history/abcd', '/stranded/x.json', '/cards', '/fs'];

describe('router', () => {
  it('draws the screens statusd serves it for, and nothing else', () => {
    expect(REACT_PATHS.map((p) => route(p)?.screen)).toEqual(['home', 'library', 'costs', 'runs', 'tree', 'tree', 'project', 'page']);
    expect(PYTHON_PATHS.map((p) => route(p))).toEqual(PYTHON_PATHS.map(() => null));
  });
});

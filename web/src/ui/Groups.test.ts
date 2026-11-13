import { describe, expect, it } from 'vitest';
import { OTHER } from '../model';
import { groupSuffix } from './Groups';

// The title names the chosen group after its dot: Cadre.dev, Cadre.kitchen.
describe('groupSuffix', () => {
  const groups = [{ id: 'dev', name: 'Dev' }, { id: 'home-server', name: 'Home server' }];
  it('is the chosen group in lower case, spaces as dashes', () => {
    expect(groupSuffix('dev', groups)).toBe('dev');
    expect(groupSuffix('home-server', groups)).toBe('home-server');
    expect(groupSuffix(OTHER, groups)).toBe('other');
  });
  it('is nothing for Home, or with no groups at all', () => {
    expect(groupSuffix('', groups)).toBe('');
    expect(groupSuffix('dev', [])).toBe('');
  });
});

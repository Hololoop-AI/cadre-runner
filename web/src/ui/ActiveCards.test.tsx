import { cleanup, render, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { Kanban } from '../model';
import { ActiveCards } from './Kanban';

const card = (id: string, title: string, column: string, item = '') =>
  ({ id, title, note: '', column, item, by: 'driver', created_at: 1, moved_at: 1 });
const BOARD: Kanban = {
  board: 'dev', columns: ['backlog', 'ready', 'doing', 'done'],
  cards: [card('aaaaaa', 'Plan the week', 'backlog'), card('bbbbbb', 'Fix the login', 'ready', 'task-login'),
    card('cccccc', 'Ship the board', 'doing', 'task-board'), card('dddddd', 'Old work', 'done', 'task-old')],
};
const WORK = { 'task-board': { title: 'Kanban board, built', state: 'Your turn' } };

afterEach(cleanup);

describe('ActiveCards', () => {
  it('lists only Doing then Ready cards, each with its column, and links to the full board', () => {
    const { container } = render(<ActiveCards board={BOARD} work={WORK} />);
    const rows = [...container.querySelectorAll('li')];
    expect(rows.map((r) => r.querySelector('.kt')?.textContent)).toEqual(['Ship the board', 'Fix the login']);
    expect(rows.map((r) => r.querySelector('.kc')?.textContent)).toEqual(['Doing', 'Ready']);
    expect(within(container).getByRole('link', { name: /open the board/ }).getAttribute('href')).toBe('/kanban');
  });

  it("links each card to its attached work, by the work's title and where it stands when the fleet knows it", () => {
    const { container } = render(<ActiveCards board={BOARD} work={WORK} />);
    const [doing, ready] = [...container.querySelectorAll('li')];
    const known = within(doing).getByRole('link');
    expect(known.getAttribute('href')).toBe('/tree/task-board');
    expect(known.textContent).toBe('Kanban board, built');
    expect(within(doing).getByText('Your turn')).toBeTruthy();
    expect(within(ready).getByRole('link').getAttribute('href')).toBe('/tree/task-login');
  });

  it('says so when nothing is active', () => {
    const { container } = render(<ActiveCards board={{ ...BOARD, cards: [BOARD.cards[0], BOARD.cards[3]] }} work={{}} />);
    expect(container.querySelector('li')).toBeNull();
    expect(container.textContent).toContain('Nothing in Ready or Doing.');
  });
});

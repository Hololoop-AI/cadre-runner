import { describe, expect, it } from 'vitest';
import { byColumn, cardItemHref, columnLabel, kanbanMove, type Kanban } from '.';

const card = (id: string, column: string) => ({ id, title: id.toUpperCase(), note: '', column, item: '', by: 'driver', created_at: 1, moved_at: 1 });
const BOARD: Kanban = {
  board: 'dev', columns: ['backlog', 'ready', 'doing', 'done'],
  cards: [card('aaaaaa', 'doing'), card('bbbbbb', 'doing'), card('cccccc', 'backlog')],
};
const order = (b: Kanban, col: string) => (byColumn(b).get(col) || []).map((c) => c.id);

// The same moves as tests/test_kanban.py, so a drag draws where the server will put it.
describe('kanbanMove', () => {
  it('puts a card in another column in front of a card there, or at its end', () => {
    const moved = kanbanMove(BOARD, 'cccccc', 'doing', 'bbbbbb');
    expect(order(moved, 'doing')).toEqual(['aaaaaa', 'cccccc', 'bbbbbb']);
    expect(order(moved, 'backlog')).toEqual([]);
    expect(order(kanbanMove(moved, 'aaaaaa', 'doing', ''), 'doing')).toEqual(['cccccc', 'bbbbbb', 'aaaaaa']);
  });

  it('leaves the board as it was for a move the server would refuse, or a card dropped on itself', () => {
    for (const [c, col, before] of [['ffffff', 'doing', ''], ['aaaaaa', 'someday', ''], ['aaaaaa', 'doing', 'cccccc'],
      ['aaaaaa', 'doing', 'aaaaaa']]) {
      expect(kanbanMove(BOARD, c, col, before)).toBe(BOARD);
    }
  });
});

describe('the board, read', () => {
  it('names columns and groups cards in board order; links a card to its item', () => {
    expect(columnLabel('doing')).toBe('Doing');
    expect([...byColumn(BOARD).keys()]).toEqual(['backlog', 'ready', 'doing', 'done']);
    expect(cardItemHref('task-a')).toBe('/tree/task-a');
    expect(cardItemHref('page:ab cd')).toBe('/page/ab%20cd');
  });
});

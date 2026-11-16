import { describe, expect, it } from 'vitest';
import { activeCards, byColumn, cardItemHref, cardRuns, cardTally, columnLabel, kanbanMove, runPages, runsOnCards, type Fleet, type Kanban } from '.';

const card = (id: string, column: string) => ({ id, title: id.toUpperCase(), note: '', column, item: '', runs: [] as string[], by: 'driver', created_at: 1, moved_at: 1 });
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

describe('activeCards', () => {
  it('is the cards between the first and last columns, furthest along first, each column in board order', () => {
    const board: Kanban = { ...BOARD, cards: [...BOARD.cards, card('dddddd', 'ready'), card('eeeeee', 'done'), card('ffffff', 'ready')] };
    expect(activeCards(board).map((c) => c.id)).toEqual(['aaaaaa', 'bbbbbb', 'dddddd', 'ffffff']);
  });

  it('is nothing when every card waits in the first column or is done', () => {
    expect(activeCards({ ...BOARD, cards: [card('cccccc', 'backlog'), card('eeeeee', 'done')] })).toEqual([]);
  });
});

describe("a card's runs", () => {
  const sf = (task: string, opened: number, title: string) => ({ path: `/session/${task}000000000000`, task, opened, title });
  const fleet = {
    snap: { surfaces: [sf('t1', 5, 'Waiting on you'), sf('t2', 9, 'Being worked on'), sf('t3', 7, 'Finished'), sf('loose', 1, 'Loose')] },
    tree: { t1: { parent: 'root' }, t2: { parent: 'root', working: true }, t3: {} },
    statuses: {}, returned: { t1: 6 }, items: { t3: { tags: [], done_at: 8, hidden_at: null } },
  } as unknown as Fleet;
  const c = { ...card('dddddd', 'doing'), item: 't3', runs: ['t1', 't2', 'gone'] };
  it('are read by their own tasks, even deep in another tree, newest first, and nothing else', () => {
    const runs = cardRuns(c, fleet);
    expect(runs.map((r) => [r.key, r.state, r.title])).toEqual(
      [['t2', 'working', 'Being worked on'], ['t3', 'done', 'Finished'], ['t1', 'your-turn', 'Waiting on you']]);
    expect(runs[2].href).toBe('/view/t1000000000000');
    expect([...runsOnCards({ ...BOARD, cards: [c] })].sort()).toEqual(['gone', 't1', 't2', 't3']);
  });
  it('leave out the run that parked the idea: where a card came from is not work on it', () => {
    const parked = { ...card('eeeeee', 'backlog'), item: 't3', by: 't3' };
    expect(cardRuns(parked, fleet)).toEqual([]);
    expect([...runsOnCards({ ...BOARD, cards: [parked] })]).toEqual([]);
  });
  it('draw a run as the pages of its own task and every session it launched', () => {
    const pages = [sf('a', 1, 'A'), sf('b', 3, 'B'), sf('c', 2, 'C'), sf('x', 4, 'X')];
    expect(runPages('a', pages, { a: ['b'], b: ['c'] }).map((p) => p.task)).toEqual(['b', 'c', 'a']);
  });
  it('tally what waits on you, what an agent is on and what is done', () => {
    expect(cardTally(cardRuns(c, fleet))).toEqual({ runs: 3, yourTurn: 1, working: 1, done: 1 });
  });
});

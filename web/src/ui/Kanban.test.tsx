import { cleanup, fireEvent, render, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import type { Kanban } from '../model';
import { Board, type BoardChange } from './Kanban';

const card = (id: string, title: string, column: string, extra = {}) =>
  ({ id, title, note: '', column, item: '', runs: [] as string[], by: 'driver', created_at: 1, moved_at: 1, ...extra });
const BOARD: Kanban = {
  board: 'dev', columns: ['backlog', 'ready', 'doing', 'done'],
  cards: [card('aaaaaa', 'Book a table', 'doing', { by: 'task-plan-dinner-20261115-101010-abc123' }),
    card('bbbbbb', 'Plan the week', 'backlog', { item: 'task-a', note: 'Sunday' }),
    card('cccccc', 'Order flowers', 'doing')],
};

afterEach(cleanup);

function draw() {
  const sent: BoardChange[] = [];
  const view = render(<Board board={BOARD} onChange={(c) => sent.push(c)} />);
  const column = (name: string) => view.container.querySelector(`section[data-column="${name}"]`) as HTMLElement;
  const titles = (name: string) => [...column(name).querySelectorAll('.ktitle')].map((b) => b.textContent);
  return { sent, column, titles, view };
}

describe('Board', () => {
  it('shows only the cards that match find, by title, note or id, and counts them against the column', () => {
    const view = render(<Board board={BOARD} onChange={() => {}} find="sunday" />);
    expect([...view.container.querySelectorAll('.ktitle')].map((b) => b.textContent)).toEqual(['Plan the week']);
    expect(view.container.querySelector('section[data-column="doing"] h2')?.textContent).toBe('Doing 0 of 2');
    cleanup();
    const byId = render(<Board board={BOARD} onChange={() => {}} find="cccccc" />);
    expect([...byId.container.querySelectorAll('.ktitle')].map((b) => b.textContent)).toEqual(['Order flowers']);
  });

  it('draws the four columns in order, each card in its column in board order', () => {
    const { view, titles } = draw();
    expect([...view.container.querySelectorAll('.kcol h2')].map((h) => h.textContent)).toEqual(['Backlog 1', 'Ready 0', 'Doing 2', 'Done 0']);
    expect(titles('doing')).toEqual(['Book a table', 'Order flowers']);
    expect(titles('backlog')).toEqual(['Plan the week']);
  });

  it("shows a card's note, its item's link and the agent that added it", () => {
    const { column } = draw();
    expect(within(column('backlog')).getByText('Sunday')).toBeTruthy();
    expect(within(column('backlog')).getByRole('link').getAttribute('href')).toBe('/tree/task-a');
    expect(within(column('doing')).getByText('by plan dinner')).toBeTruthy();
  });

  it('moves a card a column with its arrows, adds from a column, edits and removes', () => {
    const { sent, column } = draw();
    fireEvent.click(within(column('backlog')).getByLabelText('Move to Ready'));
    const add = within(column('done')).getByLabelText('add a card to Done') as HTMLInputElement;
    fireEvent.change(add, { target: { value: 'Pay rent' } });
    fireEvent.submit(add.form as HTMLFormElement);
    expect(add.value).toBe('');
    fireEvent.click(within(column('doing')).getByText('Order flowers'));
    const title = within(column('doing')).getByLabelText('title');
    fireEvent.change(title, { target: { value: 'Order tulips' } });
    fireEvent.submit((title as HTMLInputElement).form as HTMLFormElement);
    fireEvent.click(within(column('doing')).getByText('Book a table'));
    fireEvent.click(within(column('doing')).getByText('Remove'));
    expect(sent).toEqual([
      { action: 'move', card: 'bbbbbb', column: 'ready', before: '' },
      { action: 'add', column: 'done', title: 'Pay rent' },
      { action: 'edit', card: 'cccccc', title: 'Order tulips', note: '' },
      { action: 'remove', card: 'aaaaaa' },
    ]);
  });

  it('a card dropped on a column goes to its end; on a card, in front of it', () => {
    const { sent, column, view } = draw();
    const flowers = within(column('doing')).getByText('Order flowers').closest('li') as HTMLElement;
    const plan = within(column('backlog')).getByText('Plan the week').closest('li') as HTMLElement;
    const data = { getData: () => 'bbbbbb', setData: () => {} };
    fireEvent.dragStart(plan, { dataTransfer: data });
    fireEvent.drop(flowers, { dataTransfer: data, clientY: -1 });
    fireEvent.dragStart(plan, { dataTransfer: data });
    fireEvent.drop(column('done'), { dataTransfer: data });
    expect(sent).toEqual([
      { action: 'move', card: 'bbbbbb', column: 'doing', before: 'cccccc' },
      { action: 'move', card: 'bbbbbb', column: 'done', before: '' },
    ]);
    expect(view.container.querySelectorAll('.dragging').length).toBe(0);
  });
});

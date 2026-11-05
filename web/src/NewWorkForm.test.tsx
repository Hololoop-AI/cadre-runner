import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NewWorkForm } from './NewWorkForm';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function mockFetch(res: Partial<Response>) {
  const fn = vi.fn().mockResolvedValue({ ok: true, status: 200, statusText: 'OK', url: '', ...res });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function fill(text: string, cwd = '') {
  fireEvent.change(screen.getByLabelText(/What should an agent do/), { target: { value: text } });
  if (cwd) fireEvent.change(screen.getByLabelText(/Folder/), { target: { value: cwd } });
  fireEvent.click(screen.getByRole('button', { name: 'Start' }));
}

describe('NewWorkForm', () => {
  it('posts text and folder as a form and shows the redirect notice', async () => {
    const fetch = mockFetch({ url: 'http://h/?notice=Task+submitted%3A+task-abc' });
    const onSubmitted = vi.fn();
    render(<NewWorkForm onSubmitted={onSubmitted} />);
    fill('  fix the thing ', '/home/me/proj');

    expect((await screen.findByRole('status')).textContent).toContain('Task submitted: task-abc');
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe('/tasks');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/x-www-form-urlencoded');
    expect(String(init.body)).toBe('text=fix+the+thing&cwd=%2Fhome%2Fme%2Fproj');
    expect(onSubmitted).toHaveBeenCalledOnce();
    expect((screen.getByLabelText(/What should an agent do/) as HTMLTextAreaElement).value).toBe('');
  });

  it('omits an empty folder', async () => {
    const fetch = mockFetch({ url: 'http://h/?notice=Task+submitted.' });
    render(<NewWorkForm />);
    fill('just text');
    await screen.findByRole('status');
    expect(String(fetch.mock.calls[0][1].body)).toBe('text=just+text');
  });

  it('keeps the text and shows a rejection notice as an error', async () => {
    mockFetch({ url: 'http://h/?notice=Task+rejected%3A+bad+cwd' });
    const onSubmitted = vi.fn();
    render(<NewWorkForm onSubmitted={onSubmitted} />);
    fill('do it', '/nope');
    const status = await screen.findByRole('status');
    expect(status.textContent).toContain('Task rejected: bad cwd');
    expect(status.classList.contains('error')).toBe(true);
    expect(onSubmitted).not.toHaveBeenCalled();
    expect((screen.getByLabelText(/What should an agent do/) as HTMLTextAreaElement).value).toBe('do it');
  });

  it('reports a refused post (403) as an error', async () => {
    mockFetch({ ok: false, status: 403, statusText: 'Forbidden', url: 'http://h/tasks' });
    render(<NewWorkForm />);
    fill('x');
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('403'));
    expect(screen.getByRole('status').classList.contains('error')).toBe(true);
  });

  it('does not submit without text', () => {
    const fetch = mockFetch({});
    render(<NewWorkForm />);
    expect((screen.getByRole('button', { name: 'Start' }) as HTMLButtonElement).disabled).toBe(true);
    expect(fetch).not.toHaveBeenCalled();
  });
});

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BackLink } from './Back';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('BackLink', () => {
  it('goes back in history when the page was reached from this app', () => {
    vi.spyOn(document, 'referrer', 'get').mockReturnValue(location.origin + '/c/dev');
    vi.spyOn(history, 'length', 'get').mockReturnValue(3);
    const back = vi.spyOn(history, 'back').mockImplementation(() => {});
    render(<BackLink up="/c/dev" />);
    fireEvent.click(screen.getByText('← back'));
    expect(back).toHaveBeenCalledOnce();
  });

  it('on a framed screen, goes straight to the page it was opened from, not through the frame\'s history', () => {
    vi.spyOn(document, 'referrer', 'get').mockReturnValue(location.origin + '/c/dev');
    vi.spyOn(history, 'length', 'get').mockReturnValue(7);
    const back = vi.spyOn(history, 'back').mockImplementation(() => {});
    const assign = vi.fn();
    vi.spyOn(window, 'location', 'get').mockReturnValue({ ...window.location, origin: window.location.origin, href: window.location.href, assign } as unknown as Location);
    render(<BackLink up="/" framed />);
    fireEvent.click(screen.getByText('← back'));
    expect(back).not.toHaveBeenCalled();
    expect(assign).toHaveBeenCalledWith(location.origin + '/c/dev');
  });

  it('is a plain link one level up otherwise, with fleet beside it', () => {
    vi.spyOn(document, 'referrer', 'get').mockReturnValue('');
    const back = vi.spyOn(history, 'back').mockImplementation(() => {});
    render(<BackLink up="/c/dev" />);
    expect(screen.getByText('← back').getAttribute('href')).toBe('/c/dev');
    expect(screen.getByText('fleet').getAttribute('href')).toBe('/');
    fireEvent.click(screen.getByText('← back'));
    expect(back).not.toHaveBeenCalled();
  });
});

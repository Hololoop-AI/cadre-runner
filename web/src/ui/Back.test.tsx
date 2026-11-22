import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BackLink, UpLink } from './Back';

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

  it('is a plain link one level up otherwise, with fleet beside it', () => {
    vi.spyOn(document, 'referrer', 'get').mockReturnValue('');
    const back = vi.spyOn(history, 'back').mockImplementation(() => {});
    render(<BackLink up="/c/dev" />);
    expect(screen.getByText('← back').getAttribute('href')).toBe('/c/dev');
    expect(screen.getByText('fleet').getAttribute('href')).toBe('/');
    fireEvent.click(screen.getByText('← back'));
    expect(back).not.toHaveBeenCalled();
  });

  it('the page viewer\'s way back goes to the home of the page\'s cadre, or to the fleet when the cadre is not known', () => {
    render(<UpLink href="/c/dev" name="Dev" />);
    const link = screen.getByText('← back');
    expect(link.getAttribute('href')).toBe('/c/dev');
    expect(link.getAttribute('title')).toBe("back to Dev's home");
    cleanup();
    render(<UpLink href="/" name="" />);
    expect(screen.getByText('← back').getAttribute('href')).toBe('/');
  });
});

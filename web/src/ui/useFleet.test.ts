import { describe, expect, it } from 'vitest';
import { takeInlineFleet } from './useFleet';

// statusd.index_with_fleet writes the first payload into index.html with every
// "<" as \u003c; the app takes it once, in place of its first GET /api/fleet.
describe('takeInlineFleet', () => {
  it('reads the payload statusd put in the page, once', () => {
    document.body.innerHTML = '<script type="application/json" id="cadre-fleet">'
      + '{"version":"v1","snap":{"surfaces":[{"title":"a \\u003c/script> b"}]}}</script>';
    const fleet = takeInlineFleet();
    expect(fleet?.version).toBe('v1');
    expect(fleet?.snap?.surfaces?.[0]?.title).toBe('a </script> b');
    expect(takeInlineFleet()).toBeNull();
  });

  it('is null when the page has none, or it is not JSON', () => {
    document.body.innerHTML = '';
    expect(takeInlineFleet()).toBeNull();
    document.body.innerHTML = '<script type="application/json" id="cadre-fleet">{oops</script>';
    expect(takeInlineFleet()).toBeNull();
  });
});

// The one header every fleet screen draws: whatever the screen puts in it,
// then the notification bell at its right end. No screen writes <header>
// itself (tests/test_web_one_header.py refuses one that does), so a new screen gets the bell
// without anyone remembering to add it. The one screen with no header, the
// frame around a review page (View.tsx), draws the bell in its bar.

import type { ReactNode } from 'react';
import { Bell } from './Bell';

export function TopBar({ children }: { children: ReactNode }) {
  return <header>{children}<Bell /></header>;
}

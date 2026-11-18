// The composition root: the UI and the Python page's stylesheet, mounted.
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App, installTooltips } from './ui';
import './ui/fleet.css';
import './ui/scrollbars.css';

installTooltips();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

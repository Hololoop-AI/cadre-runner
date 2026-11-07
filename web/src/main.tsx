// The composition root: the UI and the Python page's stylesheet, mounted.
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './ui';
import './ui/fleet.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

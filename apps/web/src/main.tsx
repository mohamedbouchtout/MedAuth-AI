import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';

import { App } from './App';
import './index.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('index.html is missing its #root element');
}

createRoot(container).render(
  <StrictMode>
    {/*
      The router lives here rather than inside `App` so tests can supply their
      own (a `MemoryRouter`) and drive a route directly. TASK-071 is what made
      this app need one: a note review screen has to be linkable and has to
      survive a reload, which an in-memory phase of a visit cannot be.
    */}
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);

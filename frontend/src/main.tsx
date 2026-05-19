import React from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import './styles.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element #root not found in index.html');
}

/** Mount the React application into the root DOM node. */
createRoot(rootElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

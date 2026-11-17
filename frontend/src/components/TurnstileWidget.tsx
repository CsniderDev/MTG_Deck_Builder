import React, { useEffect, useRef, useState } from 'react';

declare global {
  interface Window {
    turnstile?: {
      render: (container: HTMLElement, options: Record<string, unknown>) => string;
      remove: (widgetId?: string) => void;
      reset: (widgetId?: string) => void;
    };
  }
}

const TURNSTILE_SCRIPT_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
const SITE_KEY = (import.meta.env.VITE_TURNSTILE_SITE_KEY as string | undefined) || '';

let scriptLoadPromise: Promise<void> | null = null;

function loadTurnstileScript(): Promise<void> {
  /** Load the Cloudflare Turnstile script once and cache the promise for reuse. */
  if (window.turnstile) return Promise.resolve();
  if (!scriptLoadPromise) {
    scriptLoadPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = TURNSTILE_SCRIPT_SRC;
      script.async = true;
      script.defer = true;
      script.onload = () => resolve();
      script.onerror = () => reject(new Error('Failed to load Turnstile script'));
      document.head.appendChild(script);
    });
  }
  return scriptLoadPromise;
}

export interface TurnstileWidgetProps {
  onTokenChange: (token: string) => void;
}

export default function TurnstileWidget({ onTokenChange }: TurnstileWidgetProps): React.ReactElement | null {
  /**
   * Render a Cloudflare Turnstile bot challenge and report the verification token upward.
   * Renders nothing and never calls onTokenChange when VITE_TURNSTILE_SITE_KEY isn't set,
   * matching the backend, which only enforces Turnstile when it has a secret key configured.
   */
  const containerRef = useRef<HTMLDivElement | null>(null);
  const widgetIdRef = useRef<string | undefined>(undefined);
  const [scriptReady, setScriptReady] = useState<boolean>(false);

  useEffect(() => {
    if (!SITE_KEY) return undefined;
    let cancelled = false;
    loadTurnstileScript()
      .then(() => {
        if (!cancelled) setScriptReady(true);
      })
      .catch(() => {
        // Degrade silently - if Turnstile can't load, the backend still works unless
        // it has a secret key configured, in which case the user will see a clear
        // "verification failed" error on submit and can retry.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!scriptReady || !containerRef.current || !window.turnstile) return undefined;
    const container = containerRef.current;
    widgetIdRef.current = window.turnstile.render(container, {
      sitekey: SITE_KEY,
      callback: (token: string) => onTokenChange(token),
      'expired-callback': () => onTokenChange(''),
      'error-callback': () => onTokenChange(''),
    });
    return () => {
      if (widgetIdRef.current && window.turnstile) {
        window.turnstile.remove(widgetIdRef.current);
        widgetIdRef.current = undefined;
      }
    };
  }, [scriptReady, onTokenChange]);

  if (!SITE_KEY) return null;
  return <div className="turnstile-widget" ref={containerRef} />;
}

"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the backend package root is importable when running pytest from /backend.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Make sure no leaked .env from the dev machine activates the LLM during tests.
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:5173")

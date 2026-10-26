# Setup

## API (Windows, PowerShell)
1. cd apps\api
2. py -3.11 -m venv .venv
3. .\.venv\Scripts\python -m pip install -r requirements.txt
4. Run: .\.venv\Scripts\python -m uvicorn app.main:app --reload
5. Open http://127.0.0.1:8000/health
6. Test: .\.venv\Scripts\python -m pytest

## Known issues
- Plain `python` is 3.14 on this machine. Always use `py -3.11` for the venv.

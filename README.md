# DepthFlow-backend

Short README to get started with the DepthFlow backend (FastAPI).

## Requirements
- Python 3.10+ recommended
- See `requirements.txt` for Python dependencies

## Quick start

1. Clone the repo and change into the project directory:

   ```powershell
   git clone <repo-url> depthflow-backend
   cd depthflow-backend
   ```

2. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Create a `.env` file in the project root (example entries):

   ```text
   DATABASE_URL=sqlite:///./db.sqlite3
   SECRET_KEY=your-secret-key
   ```

   Adjust variables to your environment. `main.py` loads environment variables via `python-dotenv`.

5. Initialize the database (tables are created automatically on startup):

   ```powershell
   python -m uvicorn main:app --reload
   ```

   The API will be available at `http://127.0.0.1:8000`.

## Project structure

- `main.py`: FastAPI app and router registration.
- `database.py`: Database engine and Base model.
- `models.py`: SQLAlchemy models.
- `schemas.py`: Pydantic schemas.
- `auth.py`: Authentication helpers.
- `routers/`: API routers
  - `auth_router.py`: authentication endpoints
  - `ai_router.py`: AI-related endpoints

## Common commands
- Run dev server:

  ```powershell
  python -m uvicorn main:app --reload
  ```

- Install new dependency:

  ```powershell
  pip install <package>
  pip freeze > requirements.txt
  ```

## Using the API

Open `http://127.0.0.1:8000/docs` for interactive Swagger UI.

## Notes & troubleshooting
- If you see import errors, ensure your virtual environment is activated and packages from `requirements.txt` are installed.
- If the dev server fails to start, check console output for missing env variables or DB issues.

## Contributing
- Open issues or PRs. Keep changes focused and add tests where appropriate.

---
File created to help new contributors get started quickly. If you want extra sections (deployment, tests, CI), tell me which to add.

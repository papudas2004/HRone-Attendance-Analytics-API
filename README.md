# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run locally

1. Install Python 3.10+ and MongoDB (local or hosted).
2. Install dependencies: `pip install -r requirements.txt`
3. Set `MONGO_URI` and `MONGO_DB` in the environment (or a local `.env` file; do not commit secrets).
4. Start the API from the repository root: `uvicorn app.main:app --port 8000`
5. Open `http://127.0.0.1:8000/docs` to explore the API. `/health` checks MongoDB connectivity.

Indexes are created idempotently at application startup. Calendar dates use IST date strings; instants cross the API boundary as epoch milliseconds and are stored as UTC BSON datetimes.

## Assignment notes

`REVIEW.md` records the review findings and `DECISIONS.md` explains the implementation choices. Before submission, run the concurrency and analytics edge cases against the full dataset; the tiny sample alone does not prove correctness. Do not commit `.env`, secrets, virtual environments, or database dumps.

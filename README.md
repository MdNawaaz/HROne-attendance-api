# HROne Employee Attendance & Analytics API

A robust backend service built with FastAPI and MongoDB for managing employee attendance, business rules, regularization audit trails, and advanced aggregation analytics.

## Local Setup

1. **Clone the repository and install dependencies:**
   python -m venv .venv
   source .venv/bin/activate   # On Windows: .venv\Scripts\Activate.ps1
   pip install -r requirements.txt

2. **Configure Environment Variables:**
   Create a .env file in the root directory and set your MongoDB connection details:
   MONGO_URI=mongodb://localhost:27017
   MONGO_DB=attendance_db

3. **Run the Application:**
   uvicorn app.main:app --port 8000 --reload

## Documentation & Decisions
- **`REVIEW.md`**: Lists reviewed starter code defects and their fixes.
- **`DECISIONS.md`**: Explains architectural choices, indexes, concurrency handling, and tie-breaking logic.

# Technology Stack

## Core Technologies
- **Language**: Python 3.10+
- **Browser Automation**: Playwright (Async Python API)
- **AI / LLM Integration**: DeepSeek API (`deepseek-chat`)
- **Data Persistence**: SQLite (`data/jobs_tracker.db`), JSON (`data/library.json`), CSV (`data/jobs_tracker.csv`)
- **PDF Processing**: PyPDF / PyPDF2 / pdfplumber

## Project Structure
- `src/app_workday.py`: Workday ATS automation script.
- `src/app_greenhouse.py`: Greenhouse ATS automation script.
- `src/app_ashby.py`: Ashby ATS automation script.
- `src/app_common.py`: Shared utilities and DOM helpers.
- `src/job_tracker.py`: CLI engine for job ingestion, evaluation, and application batch tracking.
- `data/library.json`: Master candidate profile and configuration.

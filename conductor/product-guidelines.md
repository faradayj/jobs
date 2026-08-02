# Product Guidelines

## Development & Automation Principles
- **No Direct Submissions**: Never automatically click submit on job applications; always halt on the Review page.
- **Data Fidelity**: Populate forms accurately using candidate profile information from `data/library.json`.
- **Clean Prose Descriptions**: Remove bullet points and format experience descriptions as clean prose paragraphs.
- **Fail-Safe Operation**: Implement deterministic fallbacks when LLM inference is unavailable or rate-limited.
- **Robust ATS Navigation**: Handle dynamic DOM re-renders, inline forms, combobox pills, and page step transitions gracefully.

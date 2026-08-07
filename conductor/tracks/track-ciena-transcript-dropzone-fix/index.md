# Track: Fix Transcript Dropzone Misidentification & Resume Overwrite

## Overview
- **ID**: `track-ciena-transcript-dropzone-fix`
- **Status**: `[~]`
- **Spec**: [Specification](./spec.md)
- **Plan**: [Implementation Plan](./plan.md)

## Summary
Enhances `handle_document_uploads()` DOM context traversal and classification to strictly route transcript dropzones to `TRANSCRIPT_PATH` and resume dropzones to `RESUME_PATH`.

import asyncio
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import evaluator as e
import tracker_db as t

PROFILE = {"skills": ["Python", "SQL"], "personal_info": {"password": "secret", "email": "private"},
           "job_board_mappings": {"holds_security_clearance": "No"}}
DESCRIPTION = "Must hold an active TS/SCI clearance. " + "Job description details. " * 20

def answer(**changes):
    data = {"score": 1, "eligibility": "eligible", "suitability_reason": "Python match",
            "blockers": [], "salary": {"has_salary": False}, "is_remote": False,
            "locations": [], "skills": {"library_matches": ["Python"], "explicit_jd_skills": ["Python", "Rust"]}}
    data.update(changes)
    return data

class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.paths = patch.multiple(t, DB_PATH=root/"jobs.db", CSV_PATH=root/"jobs.csv",
            DETAILS_PATH=root/"details.json", APPLIED_HISTORY_PATH=root/"applied.json",
            FILTERED_HISTORY_PATH=root/"filtered.json")
        self.paths.start()
        self.conn = t.get_db()
        self.conn.execute("INSERT INTO jobs(id,company,role,location,apply_url,status,job_description) VALUES(1,'Example','SWE','CA','https://example.com/job/1','Pending Evaluation',?)", (DESCRIPTION,))
        self.conn.commit()
        self.job = (1, "Example", "SWE", "CA", "https://example.com/job/1", DESCRIPTION)
    def tearDown(self):
        self.conn.close(); self.paths.stop(); self.temp.cleanup()
    def test_failed_json_write_preserves_previous_history(self):
        t.write_json_atomic(t.FILTERED_HISTORY_PATH, {"existing": {"reason":"kept"}})
        with self.assertRaises(TypeError):
            t.write_json_atomic(t.FILTERED_HISTORY_PATH, {"bad":object()})
        self.assertEqual(t.load_filtered_history(), {"existing": {"reason":"kept"}})
        self.assertEqual(list(pathlib.Path(self.temp.name).glob("*.tmp")), [])
    def test_candidate_policies_reach_model_context(self):
        profile = dict(PROFILE, role_evaluation_preferences={"degree_equivalence":"MS counts", "mixed_clearance_wording":"Apply with obtain pathway"})
        self.assertEqual(e.evaluation_profile(profile)["role_evaluation_preferences"], profile["role_evaluation_preferences"])
    def test_nonblocking_cautions_preserve_eligibility(self):
        result = e.parse_evaluation(answer(application_cautions=["Confirm clearance timeline with recruiter"]), PROFILE, DESCRIPTION)
        self.assertEqual(result.eligibility, "eligible")
        self.assertEqual(result.eval_metadata["blockers"], [])
        self.assertEqual(result.eval_metadata["application_cautions"], ["Confirm clearance timeline with recruiter"])
    def test_application_boilerplate_is_not_a_description(self):
        self.assertFalse(e.usable_description("Compensation based on qualifications. Autofill from resume " + "Upload file Degree Education " * 40))
        self.assertFalse(e.usable_description("Upload Resume Personal Details " + "requirements of protected veterans " * 40))
        self.assertTrue(e.usable_description("Responsibilities: Build APIs. Requirements: Python. " + "Role details " * 40))
    def test_skill_aliases_preserve_inventory_names(self):
        profile = {"skills": ["Python Programming", "C++ Programming", "Amazon Web Services"]}
        data = answer(skills={"library_matches": [], "explicit_jd_skills": ["Python", "C++", "AWS", "Rust"]})
        result = e.parse_evaluation(data, profile, DESCRIPTION)
        self.assertEqual(result.target_skills, profile["skills"])
        self.assertEqual(result.eval_metadata["missing_skills"], ["Rust"])
    def test_invalid_responses_do_not_default_to_rejection(self):
        for data in [{}, answer(score=4), answer(score=True), answer(eligibility="invalid"), answer(is_remote="false")]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                e.parse_evaluation(data, PROFILE, DESCRIPTION)
    def test_rejection_requires_real_evidence(self):
        for blockers in [[], [{"requirement":"clearance", "excerpt":"invented phrase", "candidate_fact":"No clearance"}]]:
            with self.assertRaises(ValueError):
                e.parse_evaluation(answer(eligibility="ineligible", blockers=blockers), PROFILE, DESCRIPTION)
        data = answer(eligibility="ineligible", blockers=[{"requirement":"active clearance", "excerpt":"Must hold an active TS/SCI clearance.", "candidate_fact":"No clearance"}])
        result = e.parse_evaluation(data, PROFILE, DESCRIPTION)
        e.store_evaluation(self.conn, self.job, result, DESCRIPTION)
        t.export_db_to_csv()
        history = t.load_filtered_history()[t.clean_url(self.job[4])]
        self.assertEqual(history["eval_metadata"]["blockers"], data["blockers"])
        self.assertEqual(history["job_description"], DESCRIPTION)
    def test_low_priority_survives_export_import(self):
        result = e.parse_evaluation(answer(score=3), PROFILE, DESCRIPTION)
        e.store_evaluation(self.conn, self.job, result, DESCRIPTION)
        t.export_db_to_csv(); t.import_csv_to_db()
        row = self.conn.execute("SELECT status,eval_metadata FROM jobs WHERE id=1").fetchone()
        self.assertEqual(row[0], "Eligible (Priority 3)")
        self.assertEqual(json.loads(row[1])["score"], 3)
    def test_unknown_eligibility_is_retained_for_review(self):
        result = e.parse_evaluation(answer(eligibility="needs_review", score=3), PROFILE, DESCRIPTION)
        e.store_evaluation(self.conn, self.job, result, DESCRIPTION); t.export_db_to_csv()
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "Needs Review")
    def test_missing_skills_never_become_candidate_skills(self):
        result = e.parse_evaluation(answer(), PROFILE, DESCRIPTION)
        self.assertEqual(result.target_skills, ["Python"])
        self.assertEqual(result.eval_metadata["missing_skills"], ["Rust"])
    def test_profile_omits_credentials_and_contains_clearance(self):
        profile = e.evaluation_profile(PROFILE)
        self.assertNotIn("personal_info", profile)
        self.assertEqual(profile["work_authorization"]["holds_security_clearance"], "No")
    def test_applied_and_manifest_protection(self):
        result = e.parse_evaluation(answer(), PROFILE, DESCRIPTION)
        self.conn.execute("UPDATE jobs SET status='Applied',date_applied='2026-07-01' WHERE id=1"); self.conn.commit()
        self.assertFalse(e.store_evaluation(self.conn, self.job, result, DESCRIPTION))
        self.conn.execute("UPDATE jobs SET status='Pending Evaluation' WHERE id=1"); self.conn.commit()
        t.save_to_applied_manifest(self.job[4], date_applied="2026-07-01")
        self.assertFalse(e.store_evaluation(self.conn, self.job, result, DESCRIPTION))
    def test_import_cannot_modify_applied_or_fall_back_from_wrong_url(self):
        path = pathlib.Path(self.temp.name)/"scores.json"
        self.conn.execute("UPDATE jobs SET status='Applied' WHERE id=1"); self.conn.commit()
        path.write_text(json.dumps([dict(answer(score=3), id=1, apply_url=self.job[4]), dict(answer(), id=1, apply_url="https://example.com/wrong")]), encoding="utf-8")
        profile_path = pathlib.Path(self.temp.name)/"profile.json"
        profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
        with patch.object(e, "EVAL_SCORES_PATH", path), patch.object(e, "PROFILE_PATH", profile_path):
            e.run_import_scores()
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "Applied")
    def test_valid_offline_low_priority_and_invalid_result(self):
        path = pathlib.Path(self.temp.name)/"scores.json"
        profile_path = pathlib.Path(self.temp.name)/"profile.json"
        profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
        with patch.object(e, "EVAL_SCORES_PATH", path), patch.object(e, "PROFILE_PATH", profile_path):
            path.write_text(json.dumps([{"id":1,"score":3}]), encoding="utf-8")
            e.run_import_scores()
            self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "Pending Evaluation")
            path.write_text(json.dumps([dict(answer(score=3), id=1)]), encoding="utf-8")
            e.run_import_scores()
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()[0], "Eligible (Priority 3)")
    def test_export_has_new_schema_and_sanitized_profile(self):
        path = pathlib.Path(self.temp.name)/"pending.json"
        profile_path = pathlib.Path(self.temp.name)/"profile.json"
        profile_path.write_text(json.dumps(PROFILE), encoding="utf-8")
        self.conn.execute("UPDATE jobs SET job_description=? WHERE id=1", ("Requirements: Python. " + DESCRIPTION,))
        self.conn.commit()
        with patch.object(e, "EVAL_PENDING_PATH", path), patch.object(e, "PROFILE_PATH", profile_path):
            asyncio.run(e.run_export_prompts(limit=1))
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("eligibility", payload["output_schema"])
        self.assertNotIn("personal_info", payload["profile"])
        self.assertEqual(len(payload["jobs"]), 1)
    def test_exhausted_model_errors_retain_job(self):
        with patch.object(e, "evaluate_job_with_llm", AsyncMock(side_effect=ValueError("bad JSON"))):
            asyncio.run(e._evaluate_single_job(self.conn, self.job, PROFILE, "fake", False))
        self.assertEqual(self.conn.execute("SELECT status,job_description FROM jobs WHERE id=1").fetchone(), ("Evaluation Error", DESCRIPTION))
    def test_failed_scrape_report_preserves_the_returned_page(self):
        text = "Company navigation and culture. " * 30
        report = []
        job = self.job[:-1] + (None,)
        with patch.object(e, "fetch_job_description", AsyncMock(return_value=text)), \
             patch.object(e, "evaluate_job_with_llm", AsyncMock()) as model:
            asyncio.run(e._evaluate_single_job(self.conn, job, PROFILE, "fake", False, report))
        self.assertEqual(report[0]["scraped_text"], text)
        self.assertEqual(report[0]["scrape_detail"], "Page returned, but role requirements were missing")
        model.assert_not_awaited()
    def test_retries_transient_failure_then_success(self):
        request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        responses = [httpx.Response(429, request=request), httpx.Response(200, request=request, json={"choices":[{"message":{"content":json.dumps(answer())}}]})]
        client = AsyncMock(); client.post.side_effect = responses
        with patch.object(e.httpx, "AsyncClient") as factory, patch.object(e.asyncio, "sleep", AsyncMock()):
            factory.return_value.__aenter__.return_value = client
            result = asyncio.run(e.evaluate_job_with_llm("fake", PROFILE, DESCRIPTION))
        self.assertEqual(result.eligibility, "eligible"); self.assertEqual(client.post.call_count, 2)
    def test_invalid_model_output_retries_then_fails(self):
        response = httpx.Response(200, request=httpx.Request("POST", "https://api.deepseek.com"), json={"choices":[{"message":{"content":"{}"}}]})
        client = AsyncMock(); client.post.return_value = response
        with patch.object(e.httpx, "AsyncClient") as factory, patch.object(e.asyncio, "sleep", AsyncMock()):
            factory.return_value.__aenter__.return_value = client
            with self.assertRaises(ValueError):
                asyncio.run(e.evaluate_job_with_llm("fake", PROFILE, DESCRIPTION))
        self.assertEqual(client.post.call_count, 3)

if __name__ == "__main__":
    unittest.main()

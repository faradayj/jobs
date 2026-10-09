import asyncio
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qsl, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import scraper as s

DESCRIPTION = "Responsibilities: Build software. Qualifications: SQL and Python. " * 10
ICIMS_HTML = '<div class="iCIMS_JobContent"><h1>Software Engineer I</h1><p>' + DESCRIPTION + '</p></div>'


class ScraperTests(unittest.TestCase):
    def test_clean_url_preserves_ats_routing_and_job_identity(self):
        self.assertNotEqual(s.clean_url("https://example.com/jobdetail.ftl?job=123"), s.clean_url("https://example.com/jobdetail.ftl?job=456"))
        result=s.clean_url("https://boards.greenhouse.io/embed/job_app?token=123&utm_source=test")
        self.assertEqual(result,"https://boards.greenhouse.io/embed/job_app?token=123")
        result=s.clean_url("https://sjobs.brassring.com/jobs?partnerid=1&siteid=2&PageType=JobDetails&jobid=3&ref=test")
        self.assertEqual(dict(parse_qsl(urlparse(result).query)), {"partnerid":"1","siteid":"2","PageType":"JobDetails","jobid":"3"})
    def test_listing_views_preserve_job_identity(self):
        for url in ["https://jobs.lever.co/example/123/apply", "https://apply.workable.com/example/j/ABC/apply"]:
            self.assertEqual(s.listing_url(url), url[:-6])
        self.assertEqual(s.listing_url("https://example.com/apply"), "https://example.com/apply")
    def test_workday_structured_posting_includes_title_location_and_requirements(self):
        response = Mock()
        response.json.return_value = {"jobPostingInfo":{"title":"SWE", "location":"CA", "additionalLocations":["WA"], "jobDescription":"<p>"+DESCRIPTION+"</p>"}}
        with patch.object(s.requests,"get",return_value=response) as get:
            text=s.fetch_workday_content("https://tenant.wd5.myworkdayjobs.com/en-US/External/job/CA/SWE_123")
        self.assertEqual(get.call_args.args[0], "https://tenant.wd5.myworkdayjobs.com/wday/cxs/tenant/External/job/CA/SWE_123")
        self.assertIn("CA; WA",text)
        self.assertIn("Responsibilities",text)
    def test_workday_empty_response_falls_back(self):
        response=Mock();response.json.return_value={}
        with patch.object(s.requests,"get",return_value=response):
            self.assertIsNone(s.fetch_workday_content("https://tenant.wd5.myworkdayjobs.com/External/job/CA/SWE_123"))
    def test_discovers_script_board_instead_of_guessing_company_name(self):
        markup = "<script>const API_URL='https://boards-api.greenhouse.io/v1/boards/employeractual/jobs';</script>"
        with patch.object(s, "_fetch_greenhouse_api", return_value=DESCRIPTION) as fetch:
            text = s.discover_greenhouse_content("https://employer.example/careers?gh_jid=123", markup)
        fetch.assert_called_once_with("employeractual", "123")
        self.assertEqual(text, DESCRIPTION)

    def test_embedded_board_and_direct_links_are_discovered(self):
        markup = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=actualtoken"></iframe><a href="https://job-boards.greenhouse.io/other/jobs/999">Job</a>'
        tokens = s.greenhouse_board_tokens(markup, "https://employer.example/careers")
        self.assertIn("actualtoken", tokens)
        self.assertIn("other", tokens)
        self.assertNotIn("embed", tokens)

    def test_discovery_does_not_fetch_unrequested_role(self):
        markup = '<a href="https://job-boards.greenhouse.io/actual/jobs/999">Other job</a>'
        with patch.object(s, "_fetch_greenhouse_api", return_value=DESCRIPTION) as fetch:
            s.discover_greenhouse_content("https://employer.example/careers?gh_jid=123", markup)
        fetch.assert_called_once_with("actual", "123")

    def test_icims_requests_content_frame_preserving_query(self):
        response = Mock(status_code=200, text=ICIMS_HTML)
        with patch.object(s.requests, "get", return_value=response) as get:
            text = s.fetch_icims_content("https://employer.icims.com/jobs/123/job?mobile=false")
        query = dict(parse_qsl(urlparse(get.call_args.args[0]).query))
        self.assertEqual(query, {"mobile": "false", "in_iframe": "1"})
        self.assertIn("Software Engineer I", text)

    def test_icims_branding_shell_cannot_short_circuit_browser(self):
        response = Mock(status_code=200, text="<title>iCIMS Careers Portal</title>")
        with patch.object(s.requests, "get", return_value=response):
            self.assertIsNone(s.fetch_icims_content("https://employer.icims.com/jobs/123/job"))

    def test_greenhouse_decodes_escaped_html(self):
        response = Mock(status_code=200)
        response.json.return_value = {"title": "SWE", "location": {"name": "CA"},
                                      "content": "&lt;p&gt;" + DESCRIPTION + "&lt;/p&gt;"}
        with patch.object(s.requests, "get", return_value=response):
            text = s._fetch_greenhouse_api("actual", "123")
        self.assertIn("Responsibilities", text)
        self.assertNotIn("<p>", text)

    def test_greenhouse_empty_description_falls_back(self):
        response = Mock(status_code=200)
        response.json.return_value = {"title": "SWE", "location": {}, "content": ""}
        with patch.object(s.requests, "get", return_value=response):
            self.assertIsNone(s._fetch_greenhouse_api("actual", "123"))

    def test_visible_browser_fallback_reads_icims_frame(self):
        page = AsyncMock()
        page.main_frame = Mock()
        frame = AsyncMock()
        frame.url = "https://employer.icims.com/jobs/123/job?in_iframe=1"
        frame.locator = Mock(return_value=AsyncMock())
        frame.content.return_value = ICIMS_HTML
        page.frames = [page.main_frame, frame]
        page.content.return_value = "<title>iCIMS Careers Portal</title>"
        context = AsyncMock()
        context.new_page.return_value = page
        browser = AsyncMock()
        browser.new_context.return_value = context
        playwright = Mock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        with patch.object(s, "try_fetch_from_greenhouse_api", return_value=None), \
             patch.object(s, "fetch_icims_content", return_value=None), \
             patch.object(s, "_find_chrome", return_value=""), \
             patch.object(s, "async_playwright") as factory:
            factory.return_value.__aenter__.return_value = playwright
            text = asyncio.run(s.fetch_job_description("https://employer.icims.com/jobs/123/job"))
        self.assertIn("Software Engineer I", text)
        self.assertFalse(playwright.chromium.launch.call_args.kwargs["headless"])
        browser.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

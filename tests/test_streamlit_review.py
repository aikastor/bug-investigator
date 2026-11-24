import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class StreamlitReviewTests(unittest.TestCase):
    def test_first_time_user_can_open_the_seat_map(self):
        script = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"[]")):
            app = AppTest.from_file(str(script)).run()
        self.assertFalse(app.exception)
        link, = app.get("link_button")
        self.assertEqual(link.proto.label, "Open seat-booking app")
        self.assertEqual(link.proto.url, "http://127.0.0.1:4173")
        self.assertIn("Reserve seats", app.info[0].value)

    def test_failed_investigation_displays_private_link_and_unknown_usage(self):
        script = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        url = "https://smith.example/private-project/r/test"
        def respond(request, **kwargs):
            if request.full_url.endswith("/investigations"):
                payload = [{"incident_id": "incident", "status": "failed"}]
            else:
                payload = {"status": "failed", "result": None, "error": "Diagnosis failed",
                           "trace": {"status": "exported", "url": url},
                           "usage": {"input_tokens": 8, "output_tokens": None, "total_tokens": None, "responses": 1}}
            return io.BytesIO(json.dumps(payload).encode())

        with patch("urllib.request.urlopen", side_effect=respond):
            app = AppTest.from_file(str(script)).run()
        self.assertFalse(app.exception)
        links = {link.proto.label: link.proto.url for link in app.get("link_button")}
        self.assertEqual(links["Open LangSmith trace"], url)
        self.assertIn("Total unknown", app.caption[0].value)
        self.assertEqual(app.error[0].value, "Diagnosis failed")

    def test_decisions_submit_only_on_click_and_survive_rerun(self):
        script = Path(__file__).resolve().parents[1] / "streamlit_app.py"
        for choice in ("approved", "rejected"):
            with self.subTest(choice=choice):
                result = {"summary": "Checkout fee", "verification": {"outcome": "verified", "attempts": []},
                          "review": {"status": "pending", "token": "review-token"}}
                posts = []

                def respond(request, **kwargs):
                    if request.method == "POST":
                        posts.append(request.full_url.split("/investigations/")[1])
                        result["review"].update(status=choice, decision=choice, decided_at="2026-11-24")
                        if choice == "approved":
                            result["local_patch"] = {"status": "queued"}
                        payload = result["review"]
                    elif request.full_url.endswith("/investigations"):
                        payload = [{"incident_id": "incident", "status": "diagnosed", "message": "Checkout fee"}]
                    else:
                        payload = {"result": result, "github_target": {"repo": "example/app", "base": "main"}}
                    return io.BytesIO(json.dumps(payload).encode())

                with patch("urllib.request.urlopen", side_effect=respond):
                    app = AppTest.from_file(str(script)).run()
                    self.assertFalse(app.exception)
                    self.assertFalse(posts)
                    app.button(key=f"{'approve' if choice == 'approved' else 'reject'}-incident").click().run()
                    app.run()
                    self.assertFalse(app.exception)
                    self.assertEqual(posts, [f"incident/review/review-token/{choice}"])
                    self.assertEqual([button.label for button in app.button], ["Refresh investigations"])

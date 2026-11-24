import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class StreamlitReviewTests(unittest.TestCase):
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
                    self.assertEqual(len(app.button), 0)

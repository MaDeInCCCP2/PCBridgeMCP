import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "server"))
_tmp = tempfile.mkdtemp()
os.environ["PCBRIDGE_ROOT"] = _tmp
import supervisor as S  # noqa: E402

LOG = """t=1 lvl=info msg="starting web service" obj=web addr=127.0.0.1:4041 allow_hosts=[]
t=2 lvl=info msg="started tunnel" obj=tunnels name=command_line addr=http://localhost:8765 url=https://abc.ngrok-free.app
"""


class NgrokLog(unittest.TestCase):
    def test_url(self):
        self.assertEqual(S.parse_ngrok_log(LOG), {"url": "https://abc.ngrok-free.app", "api": "127.0.0.1:4041"})

    def test_error(self):
        r = S.parse_ngrok_log('t=1 lvl=eror msg="failed" err="authentication failed ERR_NGROK_4018"')
        self.assertEqual(r, {"error": "ERR_NGROK_4018"})

    def test_url_after_error(self):
        r = S.parse_ngrok_log('t=0 err="ERR_NGROK_8012"\n' + LOG)
        self.assertEqual(r.get("url"), "https://abc.ngrok-free.app")
        self.assertNotIn("error", r)

    def test_empty(self):
        self.assertEqual(S.parse_ngrok_log(""), {})


class Helpers(unittest.TestCase):
    def test_args(self):
        self.assertEqual(S.ngrok_args({}, 8765), ["http", "8765", "--host-header=rewrite", "--inspect=false", "--log=stdout"])
        self.assertEqual(S.ngrok_args({"ngrok_domain": "https://x.ngrok.app/"}, 1)[-1], "--url=https://x.ngrok.app")
        self.assertEqual(S.ngrok_args({"ngrok_domain": "x.ngrok.app", "ngrok_url_flag": "domain"}, 1)[-1],
                         "--domain=x.ngrok.app")

    def test_mcp_url(self):
        self.assertEqual(S.mcp_url("https://a/", {}), "https://a/mcp")
        self.assertEqual(S.mcp_url("https://a", {"token_auth": False, "secret": "S"}), "https://a/S/mcp")

    def test_permanent(self):
        for c in (105, 107, 4018, 121, 108):
            self.assertIn(c, S.PERMANENT_NGROK)


class SupervisorState(unittest.TestCase):
    def setUp(self):
        S.LOGS.mkdir(parents=True, exist_ok=True)
        S.CFG_PATH.write_text(json.dumps({"port": 1, "secret": "s"}), encoding="utf-8")

    def test_state_and_backoff(self):
        sup = S.Supervisor()
        sup.save_state()
        st = json.loads(S.STATE.read_text(encoding="utf-8"))
        self.assertEqual(st["server"], "off")
        self.assertEqual(st["mcp_url"], "")
        c = sup.server
        c.failed("x")
        c.failed("y")
        self.assertEqual(c.restarts, 2)
        self.assertGreater(c.next_try, 0)
        self.assertFalse(sup.llm.enabled())
        sup.save_pids()
        self.assertEqual(json.loads(S.PIDS.read_text(encoding="utf-8"))["supervisor"], os.getpid())
        S.PAUSE_FLAG.write_text("1")
        self.assertIn("ПАУЗА", sup.status_text())
        S.PAUSE_FLAG.unlink()

    def test_url_changed_writes_file(self):
        sup = S.Supervisor()
        sup.url_changed("", "https://q.ngrok-free.app")
        self.assertEqual((S.ROOT / "URL.txt").read_text(encoding="utf-8").strip(), "https://q.ngrok-free.app/mcp")

    def test_quota_rotates_without_exposing_token_in_command(self):
        sup = S.Supervisor()
        S.write_json(S.ROOT / "ngrok_accounts.json", {"accounts": [
            {"name": "reserve", "token": "secret-token", "domain": "reserve.ngrok.app"}]})
        tunnel = sup.ngrok
        tunnel.quota_exceeded()
        self.assertEqual(tunnel.profiles()["active"], "reserve")
        with patch.object(tunnel, "exe", return_value="ngrok"):
            argv = tunnel.cmd()[0]
        self.assertIn("--url=https://reserve.ngrok.app", argv)
        self.assertNotIn("secret-token", " ".join(argv))
        self.assertEqual(json.loads(Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8"))["agent"]["authtoken"], "secret-token")
        tunnel.quota_exceeded()
        self.assertEqual(tunnel.status, "error")
        self.assertIn("ERR_NGROK_725", tunnel.error)

    def test_probe_rotates_only_for_quota_error(self):
        sup = S.Supervisor()
        tunnel = sup.ngrok
        tunnel.url = "https://example.ngrok.app"
        error = S.urllib.error.HTTPError(tunnel.url, 429, "quota", {"ngrok-error-code": "ERR_NGROK_725"}, None)
        with patch.object(S.urllib.request, "urlopen", side_effect=error), patch.object(tunnel, "quota_exceeded") as switch:
            tunnel.probe()
            switch.assert_called_once()

    def test_exited_ngrok_permanent_error_does_not_restart(self):
        sup = S.Supervisor()
        sup.server.status = "ok"
        tunnel = sup.ngrok
        tunnel.proc = unittest.mock.Mock()
        tunnel.proc.poll.return_value = 1
        (S.LOGS / "ngrok.log").write_text("ERR_NGROK_320", encoding="utf-8")
        tunnel.check()
        self.assertEqual(tunnel.status, "error")
        self.assertEqual(tunnel.error, "ERR_NGROK_320")
        self.assertFalse(tunnel.due())

    def test_started_panel_can_manage_ngrok_accounts(self):
        import panel
        sup = S.Supervisor()
        with patch.object(S, "load_panel", return_value=panel), patch.object(panel.Panel, "start", return_value=1234):
            sup.start_panel()
        self.assertIsNotNone(sup.panel)
        accounts_file = S.ROOT / "ngrok_accounts.json"
        accounts_file.unlink(missing_ok=True)
        self.assertEqual(sup.panel.action("ngrok_add", {"name": "reserve", "token": "secret"}), {"ok": True})
        self.assertEqual(sup.panel.action("ngrok_select", {"name": "reserve"}), {"ok": True})
        self.assertEqual(sup.ngrok.profiles()["active"], "reserve")
        self.assertNotIn("secret", str(sup.panel.state().get("ngrok_accounts")))
        self.assertEqual(sup.panel.action("ngrok_select", {"name": "По умолчанию"}), {"ok": True})
        self.assertEqual(sup.panel.action("ngrok_delete", {"name": "reserve"}), {"ok": True})


if __name__ == "__main__":
    unittest.main()

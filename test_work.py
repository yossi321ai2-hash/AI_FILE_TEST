import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from work_core import CommandRunner, WorkError, Workspace, parse_reply, file_link, refresh_file_links
from work_browser import RequestConnection, parse_sse, hidden_window_style, TASKBAR_BITS


##################################################
class NetworkTests(unittest.TestCase): # 3,551
    def test_partial_stream_shows_text_without_authorizing_actions(self): # 417
        stream = self.stream(False)
        preview = parse_sse(stream, partial=True)
        self.assertEqual(preview, {"text": "Hello world", "complete": False})
        self.assertEqual(parse_sse(stream + 'data: {"unfinished":', partial=True)["text"], "Hello world")
        with self.assertRaises(WorkError):
            parse_sse(stream)
    # class_NetworkTests
    def test_taskbar_mask_preserves_unrelated_styles(self): # 290
        style = 0x100 | 0x40000
        hidden = hidden_window_style(style)
        self.assertEqual(hidden & ~TASKBAR_BITS, style & ~TASKBAR_BITS)
        self.assertTrue(hidden & 0x80)
        self.assertFalse(hidden & 0x40000)
    # class_NetworkTests
    def stream(self, complete=True, channel="final"): # 706
        message = {"id": "new-answer", "author": {"role": "assistant"}, "content": {"parts": [""]}, "channel": channel, "status": "in_progress", "end_turn": False}
        events = [{"p": "", "o": "add", "v": {"message": message, "conversation_id": "test-cid"}}, {"p": "/message/content/parts/0", "o": "append", "v": "Hello"}, {"v": " world"}]
        if complete:
            events.append({"p": "", "o": "patch", "v": [{"p": "/message/status", "o": "replace", "v": "finished_successfully"}, {"p": "/message/end_turn", "o": "replace", "v": True}]})
        return "\n\n".join("data: " + json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"

    # class_NetworkTests
    def test_sse_delta_inheritance_and_complete(self): # 129
        self.assertEqual(parse_sse(self.stream())["text"], "Hello world")

    # class_NetworkTests
    def test_incomplete_or_reasoning_never_becomes_actions(self): # 296
        for stream in (self.stream(False), self.stream(channel="analysis"), self.stream().replace("data: [DONE]", "")):
            with self.subTest(stream=stream), self.assertRaises(WorkError):
                parse_sse(stream)

    # class_NetworkTests
    def test_later_incomplete_final_does_not_return_earlier_answer(self): # 471
        incomplete = {"message": {"id": "later", "author": {"role": "assistant"}, "content": {"parts": ["partial"]}, "channel": "final", "status": "in_progress", "end_turn": False}, "conversation_id": "test-cid"}
        stream = self.stream().replace("data: [DONE]", "data: " + json.dumps(incomplete) + "\n\ndata: [DONE]")
        with self.assertRaises(WorkError):
            parse_sse(stream)

    # class_NetworkTests
    def test_only_current_prompt_request_is_tracked(self): # 1,049
        connection = RequestConnection.__new__(RequestConnection)
        connection.prompt = "current prompt"
        connection.request_id = None
        connection.finished = False
        connection.status = None
        connection.failure = None
        def event(prompt, request_id):
            return {"method": "Network.requestWillBeSent", "params": {"requestId": request_id, "request": {"url": "https://chatgpt.com/backend-api/f/conversation", "method": "POST", "postData": json.dumps({"messages": [{"content": {"parts": [prompt]}}]})}}}
        connection.event(event("old prompt", "old"))
        self.assertIsNone(connection.request_id)
        connection.event(event("current prompt", "new"))
        connection.event({"method": "Network.loadingFinished", "params": {"requestId": "old"}})
        self.assertFalse(connection.finished)
        connection.event({"method": "Network.loadingFinished", "params": {"requestId": "new"}})
        self.assertTrue(connection.finished)


##################################################
class CoreTests(unittest.TestCase): # 4,746
    def test_link_mapping_encoding_and_fresh_version(self): # 705
        path = self.root / "תיקיה" / "some file.py"
        path.parent.mkdir()
        path.write_text("x = 1\n")
        first = file_link(path, self.root.parent)
        self.assertIn("/project/%D7%AA", first)
        self.assertIn("some%20file.py?v=", first)
        path.write_text("x = 2\n")
        second = refresh_file_links(first, self.root.parent)
        self.assertNotEqual(first, second)
        self.assertEqual(first.split("?v=")[0], second.split("?v=")[0])
        outside = self.root.parent / "outside.txt"
        outside.write_text("text")
        with self.assertRaises(WorkError):
            file_link(outside, self.root)

    # class_CoreTests
    def test_protected_files_cannot_be_linked(self): # 214
        path = self.root / ".env"
        path.write_text("not a real secret")
        with self.assertRaises(WorkError):
            file_link(path, self.root)
    # class_CoreTests
    def setUp(self): # 245
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.workspace = Workspace(self.root, Path(self.temporary.name) / "backups")

    # class_CoreTests
    def tearDown(self): # 57
        self.temporary.cleanup()
    # class_CoreTests
    def test_protocol_accepts_fenced_reply(self): # 249
        value = [{"type": "message", "text": "שלום"}, {"type": "read", "path": "a.py"},]

        self.assertEqual(parse_reply("```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"), value,)

    # class_CoreTests
    def test_duplicate_and_unknown_actions_rejected(self): # 274
        with self.assertRaises(WorkError):
            parse_reply('[{"type":"message","text":"x","text":"y"}]')

        with self.assertRaises(WorkError):
            parse_reply('[{"type":"delete","path":"x"}]')
    # class_CoreTests
    def test_plain_text_is_not_an_action(self): # 204
        result = parse_reply('Explanation {"type":"run"}')

        self.assertEqual(result, [{"type": "message", "text": 'Explanation {"type":"run"}',}],)

    # class_CoreTests
    def test_paths_are_bounded_and_secrets_protected(self): # 314
        for path in ("../outside", "C:/Windows/x", "/outside", ".env", "browser_profile/x", ".git/config", "x:stream", "CON.txt", "file."):
            with self.subTest(path=path), self.assertRaises(WorkError):
                self.workspace.path(path)

    # class_CoreTests
    def test_write_checks_hash_and_preserves_backup(self): # 637
        (self.root / "hello.py").write_bytes(b"print('old')\r\n")
        original = self.workspace.read("hello.py")
        action = {"type": "write", "path": "hello.py", "content": "print('new')\n", "base_sha256": original["sha256"]}
        change = self.workspace.prepare(action)
        result = self.workspace.apply(change)
        self.assertEqual((self.root / "hello.py").read_text(), "print('new')\n")
        self.assertEqual(Path(result["backup"]).read_bytes(), b"print('old')\r\n")
        with self.assertRaises(WorkError):
            self.workspace.apply(change)

    # class_CoreTests
    def test_changed_after_review_not_overwritten(self): # 447
        path = self.root / "a.txt"
        path.write_text("old")
        change = self.workspace.prepare({"type": "write", "path": "a.txt", "content": "new", "base_sha256": self.workspace.read("a.txt")["sha256"]})
        path.write_text("external")
        with self.assertRaises(WorkError):
            self.workspace.apply(change)
        self.assertEqual(path.read_text(), "external")

    # class_CoreTests
    def test_new_file_and_invalid_python(self): # 435
        with self.assertRaises(SyntaxError):
            self.workspace.prepare({"type": "write", "path": "a.py", "content": "def broken(", "base_sha256": "NEW"})
        change = self.workspace.prepare({"type": "write", "path": "sub/a.py", "content": "print(42)\n", "base_sha256": "NEW"})
        self.workspace.apply(change)
        self.assertTrue((self.root / "sub/a.py").is_file())

    # class_CoreTests
    def test_command_output_and_exit(self): # 308
        result = CommandRunner().run([sys.executable, "-c", "print('WORK_RUN_OK')"], self.root, threading.Event(), lambda text: None, workspace=self.workspace)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("WORK_RUN_OK", result["output"])

    # class_CoreTests
    def test_command_timeout(self): # 348
        start = time.monotonic()
        result = CommandRunner().run([sys.executable, "-c", "import time; time.sleep(20)"], self.root, threading.Event(), lambda text: None, timeout=0.3, workspace=self.workspace)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - start, 10)


##################################################
class DesktopTests(unittest.TestCase): # 9,276
    def test_input_alignment_live_output_and_dates(self): # 1,350
        from AI_WORK import WorkWindow, Qt, output_stamp
        with tempfile.TemporaryDirectory() as directory, patch.object(WorkWindow, "refresh_tabs", lambda self: None):
            window = WorkWindow(directory)
            window.prompt.setPlainText("English first\nעברית")
            self.assertEqual(window.prompt.document().defaultTextOption().alignment(), Qt.AlignRight)
            window.begin_live()
            started = window.live_started
            window.update_live("partial")
            window.add_message("assistant", "final response")
            self.assertIn("final response", window.live.toPlainText())
            self.assertIn(output_stamp(started), window.live.toPlainText())
            self.assertIn(output_stamp(window.session["messages"][-1]["time"]), window.chat.toPlainText())
            window.append_output("command result")
            self.assertRegex(window.console.toPlainText(), r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}")
            window.link_files.setChecked(True)
            self.assertTrue(window.policy.allows("links"))
            window.close()
            saved = json.loads((Path(directory) / "settings.json").read_text())
            self.assertTrue(saved["links"])
            window.deleteLater()
            self.app.processEvents()

    # class_DesktopTests
    def test_agent_reads_deliver_link_without_file_body(self): # 929
        from AI_WORK import AgentWorker, Policies
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory, Path(directory) / "backups")
            (Path(directory) / "code.py").write_bytes(b"x = 1\n")
            policy = Policies()
            policy.set("links", True)
            worker = AgentWorker(None, "test", "test", workspace, policy, True)
            with patch("AI_WORK.file_link", return_value="https://my-files.workslocal.exposed/test/code.py?v=new"):
                result = worker.perform({"type": "read", "path": "code.py"})
            self.assertIn("sha256", result)
            self.assertIn("url", result)
            self.assertNotIn("content", result)
            policy.set("links", False)
            self.assertEqual(worker.perform({"type": "read", "path": "code.py"})["content"], "x = 1\n")
    @classmethod
    # class_DesktopTests
    def setUpClass(cls): # 170
        from AI_WORK import QApplication
        cls.app = QApplication.instance() or QApplication([])

    # class_DesktopTests
    def test_project_url_matches_canonical_conversation(self): # 379
        from AI_WORK import same_conversation
        cid = "6aa878fa-6178-83eb-b515-42e8cd0ee18a"
        self.assertTrue(same_conversation("https://chatgpt.com/g/project/c/" + cid, "https://chatgpt.com/c/" + cid))
        self.assertFalse(same_conversation("https://chatgpt.com/", "https://chatgpt.com/c/" + cid))

    # class_DesktopTests
    def test_windows_scheme_updates_editor_and_chat_colors(self): # 803
        from AI_WORK import WorkWindow, Qt, QPalette
        with tempfile.TemporaryDirectory() as directory, patch.object(WorkWindow, "refresh_tabs", lambda self: None):
            window = WorkWindow(directory)
            window.add_message("assistant", "theme test")
            window.apply_theme(Qt.ColorScheme.Dark)
            self.assertLess(window.chat.palette().color(QPalette.Base).lightness(), 128)
            self.assertIn(window.theme_colors["assistant"], window.chat.toHtml())
            window.apply_theme(Qt.ColorScheme.Light)
            self.assertGreater(window.chat.palette().color(QPalette.Base).lightness(), 128)
            window.close()
            window.deleteLater()
            self.app.processEvents()

    # class_DesktopTests
    def test_editor_persistence_and_dynamic_policies(self): # 1,618
        from AI_WORK import WorkWindow
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            (root / "hello.py").write_text("x = 1\n", encoding="utf-8")
            with patch.object(WorkWindow, "refresh_tabs", lambda self: None):
                window = WorkWindow(Path(directory) / "state")
                self.assertTrue(window.set_workspace(root))
                window.open_file("hello.py")
                editor = window.editors.currentWidget()
                editor.setPlainText("x = 2\n")
                editor.document().setModified(True)
                self.assertTrue(window.save_editor(editor))
                self.assertEqual((root / "hello.py").read_text(), "x = 2\n")
                window.policy_boxes["write"].setChecked(True)
                self.assertTrue(window.policy.allows("write"))
                window.policy_boxes["write"].setChecked(False)
                self.assertFalse(window.policy.allows("write"))
                window.add_message("user", "test session")
                window.prompt.setPlainText("saved draft")
                window.close()
                records = list((Path(directory) / "state/sessions").glob("*.json"))
                saved = json.loads(records[0].read_text(encoding="utf-8"))
                self.assertEqual(saved["draft"], "saved draft")
                self.assertEqual(saved["messages"][0]["text"], "test session")
                window.deleteLater()
                self.app.processEvents()
    # class_DesktopTests
    def test_agent_runs_read_write_test_loop(self): # 1,735
        from AI_WORK import AgentWorker, Policies

        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory, Path(directory) / "backups",)

            replies = [[{"type": "message", "text": "plan",}, {"type": "plan", "files": [], "criteria": [{"id": "output", "description": "Program prints AGENT_OK", "kind": "behavior",}],},], [{"type": "write", "path": "hello.py", "content": "print('AGENT_OK')\n", "read_id": "NEW",}], [{"type": "verify", "argv": [sys.executable, "hello.py"], "cwd": ".", "criterion": "output", "kind": "behavior", "expected": "AGENT_OK",}], [{"type": "message", "text": "finished",}],]

            # class_test_agent_runs_read_write_test_loop
            class FakeBrowser:
                # class_FakeBrowser
                def ask(self, target, prompt, cancel, update, raw_sse_callback=None,):
                    return {"text": json.dumps(replies.pop(0), ensure_ascii=False,), "url": "https://chatgpt.com/c/test",}

            policy = Policies()
            policy.set("write", True)
            policy.set("run", True)

            worker = AgentWorker(FakeBrowser(), "test", "make hello", workspace, policy, True,)

            failures = []
            messages = []

            worker.failed.connect(failures.append)
            worker.message.connect(lambda role, text: messages.append((role, text)))

            worker.run()

            self.assertEqual(failures, [])
            self.assertTrue((Path(directory) / "hello.py").is_file())
            self.assertTrue(any("AGENT_OK" in text and "exit_code" in text for role, text in messages if role == "tool"))
            self.assertFalse(replies)

    # class_DesktopTests
    def test_threaded_review_and_stop_preserve_unapproved_file(self): # 2,098
        from AI_WORK import AgentWorker, Policies

        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory, Path(directory) / "backups",)

            actions = [{"type": "write", "path": name, "content": "text", "read_id": "NEW",} for name in ("first.txt", "second.txt")]

            # class_test_threaded_review_and_stop_preserve_unapproved_file
            class FakeBrowser:
                # class_FakeBrowser
                def ask(self, target, prompt, cancel, update, raw_sse_callback=None,):
                    return {"text": json.dumps([{"type": "message", "text": "two files",}, *actions,], ensure_ascii=False,), "url": "https://chatgpt.com/c/test",}

            policy = Policies()
            policy.set("write", True)

            worker = AgentWorker(FakeBrowser(), "test", "test", workspace, policy, True,)

            worker.flow.plan([{"id": "test", "description": "fixture", "kind": "behavior",}])

            requests = []
            original = worker.perform

            # class_test_threaded_review_and_stop_preserve_unapproved_file
            def perform(action, batch_state=None):
                result = original(action, batch_state=batch_state,)
                policy.set("write", False)
                return result

            worker.perform = perform
            worker.review.connect(requests.append)

            worker.start()

            deadline = time.monotonic() + 5

            while not requests and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)

            try:
                self.assertTrue(requests)
                self.assertTrue((Path(directory) / "first.txt").exists())
                self.assertFalse((Path(directory) / "second.txt").exists())
            finally:
                worker.stop()
                self.assertTrue(worker.wait(5000))
                self.app.processEvents()

            self.assertFalse((Path(directory) / "second.txt").exists())


if __name__ == "__main__":
    unittest.main()

##################################################
# סוף קוד

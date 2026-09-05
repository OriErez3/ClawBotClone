"""Unit tests for the pure helper functions - the command guardrails in tools.py and the
reply-handling helpers in main.py. Run from the project root with:

    python -m unittest test_helpers -v

Importing main requires the .env to be present (the bot refuses to start without its keys).
No network calls are made and no Telegram/Gemini traffic is generated.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import database
import main
import tools
from tools import _is_blocked, _download_command_redirect
from main import _chunk_message, INVALID_REPLY_PATTERN, TELEGRAM_MAX_MESSAGE_CHARS


class TestBlockedCommands(unittest.TestCase):
    def test_blocks_destructive_commands(self):
        for cmd in [
            "format c:",
            "FORMAT D:",
            "diskpart",
            "mkfs.ext4 /dev/sda1",
            "rm -rf /",
            "sudo rm -rf / --no-preserve-root",
            "shutdown /s /t 0",
            "reboot",
            "reg delete HKLM\\SYSTEM /f",
            ":(){ :|:& };:",  # fork bomb
        ]:
            self.assertIsNotNone(_is_blocked(cmd), f"should be blocked: {cmd}")

    def test_allows_normal_commands(self):
        for cmd in [
            "pip install requests",
            "npm run build",
            "npm run restart",          # regression: bare 'restart' used to false-positive
            "docker restart mycontainer",
            "systemctl restart nginx",
            "rm -rf ./build",           # deleting a local folder is not wiping root
            "echo hello",
            "python main.py",
        ]:
            self.assertIsNone(_is_blocked(cmd), f"should be allowed: {cmd}")


class TestDownloadRedirect(unittest.TestCase):
    def test_redirects_download_commands(self):
        for cmd in [
            "curl -o server.jar https://example.com/server.jar",
            "curl --output file.zip https://example.com/f.zip",
            "curl -O https://example.com/f.zip --remote-name",
            "wget https://example.com/file.tar.gz",
            'Invoke-WebRequest -Uri "https://x.com/f.exe" -OutFile "f.exe"',
            "iwr https://x.com/f.msi -OutFile f.msi",
        ]:
            self.assertIsNotNone(_download_command_redirect(cmd), f"should redirect: {cmd}")

    def test_leaves_non_download_commands_alone(self):
        for cmd in [
            "curl https://api.github.com/repos/x/y/releases/latest",  # API call, no output flag
            "git clone https://github.com/x/y.git",
            "pip install requests",
            "echo wget-like behavior",  # mentions wget but isn't the command
            "pip install wget",         # installing the wget *package* is not a download
        ]:
            self.assertIsNone(_download_command_redirect(cmd), f"should be left alone: {cmd}")


class TestChunkMessage(unittest.TestCase):
    def test_empty_and_short(self):
        self.assertEqual(_chunk_message(""), [""])
        self.assertEqual(_chunk_message("hi"), ["hi"])

    def test_exact_limit_is_one_chunk(self):
        text = "a" * TELEGRAM_MAX_MESSAGE_CHARS
        self.assertEqual(_chunk_message(text), [text])

    def test_over_limit_splits_and_reassembles(self):
        text = "a" * (TELEGRAM_MAX_MESSAGE_CHARS + 1)
        chunks = _chunk_message(text)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(c) <= TELEGRAM_MAX_MESSAGE_CHARS for c in chunks))
        self.assertEqual("".join(chunks), text)


class TestInvalidReplyPattern(unittest.TestCase):
    def test_flags_raw_tool_output(self):
        for text in [
            "[12] a: 'Click here' at (100, 200)",     # raw element-map line
            'Click <a href="x" target="_blank">here</a>',  # pasted HTML
            "Read more: https://news.site/story?utm_source=newsletter",  # tracking URL
        ]:
            self.assertIsNotNone(INVALID_REPLY_PATTERN.search(text), f"should be flagged: {text!r}")

    def test_allows_normal_replies(self):
        for text in [
            "Done! I downloaded the file to your Desktop.",
            "The price is [1] dollars according to the site.",  # brackets without the map format
            "Here's the link: https://example.com/page",        # clean URL, no tracking params
        ]:
            self.assertIsNone(INVALID_REPLY_PATTERN.search(text), f"should be allowed: {text!r}")


class TestSkills(unittest.TestCase):
    def setUp(self):
        #Redirect skill storage to a throwaway temp dir so tests never touch real skills
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(tools, "SKILLS_DIR", self._tmp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_save_load_roundtrip(self):
        tools.save_skill("terraria-server", "Managing the server", "Step 1\nStep 2")
        loaded = tools.load_skill("terraria-server")
        self.assertIn("Managing the server", loaded)
        self.assertIn("Step 1", loaded)

    def test_name_is_sanitized(self):
        #Spaces/punctuation/case collapse to a safe kebab-case filename
        tools.save_skill("Terraria Server!", "desc", "body")
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "terraria-server.md")))

    def test_index_shows_description_only(self):
        tools.save_skill("a-skill", "first line is the description", "hidden body text")
        index = tools.skills_index()
        self.assertIn("- a-skill: first line is the description", index)
        self.assertNotIn("hidden body text", index)

    def test_path_traversal_is_blocked(self):
        #A traversal attempt sanitizes to a harmless name and finds nothing to load
        result = tools.load_skill("../../../etc/passwd")
        self.assertTrue(result.startswith("No skill named"))
        #Nothing was written outside the temp dir
        self.assertEqual([], [f for f in os.listdir(self._tmp.name)])

    def test_delete(self):
        tools.save_skill("temp", "d", "b")
        self.assertIn("temp", tools.skills_index())
        tools.delete_skill("temp")
        self.assertEqual("", tools.skills_index())

    def test_empty_name_rejected(self):
        result = tools.save_skill("!!!", "d", "b")
        self.assertTrue(result.startswith("Error"))


class TestSchedule(unittest.TestCase):
    def setUp(self):
        #Point the schedule at a throwaway file so tests never read the real timetable
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "schedule.txt")
        self._patch = mock.patch.object(tools, "SCHEDULE_FILE", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, text):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_is_empty_not_an_error(self):
        #The morning briefing relies on '' meaning "no schedule section", never an exception
        self.assertEqual("", tools.schedule_text())

    def test_reads_and_strips(self):
        self._write("\n Monday 09:00 Linear Algebra Room 302\n\n")
        self.assertEqual("Monday 09:00 Linear Algebra Room 302", tools.schedule_text())

    def test_comment_lines_are_dropped(self):
        self._write("# a note to myself\nMonday 09:00 Linear Algebra Room 302\n   # indented note\n")
        self.assertEqual("Monday 09:00 Linear Algebra Room 302", tools.schedule_text())

    def test_comments_only_counts_as_no_schedule(self):
        #Copying the template without editing it must not produce a schedule section
        self._write("# just the template header\n# nothing real yet\n")
        self.assertEqual("", tools.schedule_text())

    def test_tool_reports_missing_schedule_with_the_path(self):
        result = tools.read_schedule()
        self.assertIn("No class schedule saved yet", result)
        self.assertIn(self._path, result)

    def test_tool_returns_contents_and_path(self):
        self._write("Tuesday 13:00 Organic Chemistry Lab 4")
        result = tools.read_schedule()
        self.assertIn("Organic Chemistry", result)
        self.assertIn(self._path, result)

    def test_tool_has_a_docstring_for_the_generated_declaration(self):
        #from_callable builds the model-facing description from __doc__ - losing it (e.g. by
        #concatenating a variable into the docstring) would ship a tool the model can't read
        self.assertTrue((tools.read_schedule.__doc__ or "").strip())


class TestMorningBriefingPrompt(unittest.TestCase):
    def test_schedule_section_survives_braces_in_the_file(self):
        #A user's schedule file may contain { or }; formatting must not choke or re-scan them
        section = main.MORNING_SCHEDULE_SECTION.format(schedule="Mon 09:00 {weird} Room {1}")
        prompt = main.MORNING_PROMPT.format(today="Monday, September 01", tomorrow="Tuesday, September 02", schedule_section=section, leetcode_section="")
        self.assertIn("{weird}", prompt)
        self.assertIn("Monday, September 01", prompt)

    def test_prompt_without_a_schedule_still_formats(self):
        prompt = main.MORNING_PROMPT.format(today="Monday, September 01", tomorrow="Tuesday, September 02", schedule_section="", leetcode_section="")
        self.assertNotIn("{schedule_section}", prompt)
        self.assertIn("calendar_list_events", prompt)


class TestLeetcodeQueue(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = os.path.join(self._tmp.name, "leetcode.txt")
        self._patch = mock.patch.object(tools, "LEETCODE_FILE", self._path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _write(self, text):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file_is_empty(self):
        #'' means the feature is simply off, never an exception in the 8am job
        self.assertEqual([], tools.leetcode_queue())

    def test_parses_name_and_topic(self):
        self._write("# header\nTwo Sum | Arrays\nSort List | Linked List\n")
        self.assertEqual([("Two Sum", "Arrays"), ("Sort List", "Linked List")], tools.leetcode_queue())

    def test_line_without_a_topic(self):
        self._write("Two Sum\n")
        self.assertEqual([("Two Sum", "")], tools.leetcode_queue())

    def test_seed_file_parses(self):
        #The committed seed must actually load through the real parser
        with mock.patch.object(tools, "LEETCODE_FILE",
                               os.path.join(os.path.dirname(os.path.abspath(tools.__file__)),
                                            "leetcode.example.txt")):
            queue = tools.leetcode_queue()
        self.assertGreater(len(queue), 50)
        self.assertTrue(all(name and topic for name, topic in queue))


class TestLeetcodePick(unittest.TestCase):
    """The tutor selection itself - pure, so it needs no database or files."""
    QUEUE = [("A", "Arrays"), ("B", "Trees"), ("C", "Arrays"), ("D", "Graphs"), ("E", "Trees")]

    def test_no_stats_yet_walks_the_queue_in_order(self):
        #Before there's enough history, it behaves exactly like the plain sequential walk
        picked, weak = main._pick_leetcode(self.QUEUE, set(), [])
        self.assertEqual([("A", "Arrays"), ("B", "Trees")], picked)
        self.assertIsNone(weak)

    def test_skips_problems_already_attempted(self):
        picked, _ = main._pick_leetcode(self.QUEUE, {"A", "B"}, [])
        self.assertEqual([("C", "Arrays"), ("D", "Graphs")], picked)

    def test_second_pick_drills_the_weakest_topic(self):
        stats = [{"topic": "Trees", "attempts": 4, "score": 0.1}]
        picked, weak = main._pick_leetcode(self.QUEUE, set(), stats)
        self.assertEqual("A", picked[0][0])       # still moving through the queue
        self.assertEqual(("B", "Trees"), picked[1])  # steered to the weak topic
        self.assertEqual("Trees", weak)

    def test_drill_reaches_past_the_next_in_line(self):
        stats = [{"topic": "Graphs", "attempts": 3, "score": 0.0}]
        picked, weak = main._pick_leetcode(self.QUEUE, set(), stats)
        self.assertEqual([("A", "Arrays"), ("D", "Graphs")], picked)
        self.assertEqual("Graphs", weak)

    def test_does_not_make_the_whole_day_one_topic(self):
        #When the queue's next problem is already the weak topic, the second must vary rather
        #than doubling down - otherwise a bad streak turns every day into a single subject
        stats = [{"topic": "Arrays", "attempts": 4, "score": 0.2}]
        picked, weak = main._pick_leetcode(self.QUEUE, set(), stats)
        self.assertEqual(("A", "Arrays"), picked[0])
        self.assertEqual(("B", "Trees"), picked[1])
        self.assertEqual("Arrays", weak)

    def test_weak_topic_with_nothing_left_falls_back_quietly(self):
        #Every Graphs problem is done, so it can't drill - and must not claim it did
        stats = [{"topic": "Graphs", "attempts": 3, "score": 0.0}]
        picked, weak = main._pick_leetcode(self.QUEUE, {"D"}, stats)
        self.assertEqual([("A", "Arrays"), ("B", "Trees")], picked)
        self.assertIsNone(weak)

    def test_exhausted_queue_hands_out_nothing(self):
        picked, weak = main._pick_leetcode(self.QUEUE, {"A", "B", "C", "D", "E"}, [])
        self.assertEqual([], picked)
        self.assertIsNone(weak)


class TestLeetcodeAssignment(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "leetcode.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("A | Arrays\nB | Trees\n")
        self.settings = {}
        self._patches = [
            mock.patch.object(tools, "LEETCODE_FILE", path),
            mock.patch.object(database, "get_setting", lambda k: self.settings.get(k)),
            mock.patch.object(database, "set_setting", lambda k, v: self.settings.__setitem__(k, v)),
            mock.patch.object(database, "leetcode_done_problems", lambda: set()),
            mock.patch.object(database, "leetcode_topic_stats", lambda n: []),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_records_the_pending_assignment(self):
        picked, _ = main._assign_leetcode()
        self.assertEqual([("A", "Arrays"), ("B", "Trees")], picked)
        pending = json.loads(self.settings["leetcode_pending"])
        self.assertEqual([["A", "Arrays"], ["B", "Trees"]], pending["problems"])
        self.assertIn("date", pending)

    def test_no_queue_file_means_the_feature_is_off(self):
        with mock.patch.object(tools, "LEETCODE_FILE", "/nonexistent/leetcode.txt"):
            self.assertEqual(([], None), main._assign_leetcode())
        self.assertNotIn("leetcode_pending", self.settings)


class TestLeetcodeLogging(unittest.TestCase):
    PENDING = {"date": "2026-09-02", "problems": [["Sort List", "Linked List"], ["Triangle", "DP"]]}

    def setUp(self):
        self.settings = {"leetcode_pending": json.dumps(self.PENDING)}
        self.appended = []
        self.logged = []
        self._patches = [
            mock.patch.object(tools, "get_setting", lambda k: self.settings.get(k)),
            mock.patch.object(tools, "set_setting", lambda k, v: self.settings.__setitem__(k, v)),
            mock.patch.object(database, "get_memory", lambda k: "https://docs.google.com/spreadsheets/d/SHEET123/edit#gid=0"),
            mock.patch.object(database, "add_leetcode_attempt",
                              lambda *a, **kw: self.logged.append(a)),
            mock.patch.object(tools.gs, "sheets_append",
                              lambda sid, vals, **kw: self.appended.append((sid, json.loads(vals))) or "Appended 1 row(s)."),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_extracts_the_sheet_id_from_a_pasted_url(self):
        tools.log_leetcode_result("Sort List", "yes", "clean")
        self.assertEqual("SHEET123", self.appended[0][0])

    def test_writes_the_columns_in_sheet_order(self):
        tools.log_leetcode_result("Sort List", "partly", "merge sort was fiddly")
        self.assertEqual([["Sort List", "Linked List", "partly", "merge sort was fiddly"]], self.appended[0][1])
        self.assertEqual(("Sort List", "Linked List", "partly", "merge sort was fiddly"), self.logged[0])

    def test_first_of_two_leaves_the_other_outstanding(self):
        result = tools.log_leetcode_result("Sort List", "yes")
        self.assertIn("Triangle", result)
        self.assertEqual([["Triangle", "DP"]], json.loads(self.settings["leetcode_pending"])["problems"])

    def test_logging_both_clears_the_assignment(self):
        tools.log_leetcode_result("Sort List", "yes")
        tools.log_leetcode_result("Triangle", "no", "no idea on the recurrence")
        self.assertEqual("", self.settings["leetcode_pending"])

    def test_problem_name_is_matched_case_insensitively(self):
        self.assertIn("Logged", tools.log_leetcode_result("sort list", "yes"))

    def test_rejects_an_invalid_got_it_value(self):
        result = tools.log_leetcode_result("Sort List", "kinda")
        self.assertTrue(result.startswith("Error"))
        self.assertEqual([], self.appended)

    def test_problem_outside_the_queue_needs_a_topic(self):
        #Nowhere to look one up, and a blank topic would break the weak-topic stats
        result = tools.log_leetcode_result("Two Sum", "yes")
        self.assertIn("topic", result)
        self.assertEqual([], self.appended)

    def test_extra_problem_logs_without_touching_todays_assignment(self):
        #The user does problems outside the assigned pair; those count, but must not clear it
        result = tools.log_leetcode_result("Merge Two Sorted Lists", "yes", "clean", topic="Linked List")
        self.assertIn("Logged", result)
        self.assertEqual([["Merge Two Sorted Lists", "Linked List", "yes", "clean"]], self.appended[0][1])
        still = json.loads(self.settings["leetcode_pending"])["problems"]
        self.assertEqual([["Sort List", "Linked List"], ["Triangle", "DP"]], still)

    def test_nothing_is_logged_locally_when_the_sheet_write_fails(self):
        #The sheet is the user's record - if it didn't take, local history must not drift ahead
        with mock.patch.object(tools.gs, "sheets_append", lambda *a, **kw: "Error: quota exceeded"):
            result = tools.log_leetcode_result("Sort List", "yes")
        self.assertIn("Nothing was logged", result)
        self.assertEqual([], self.logged)
        self.assertIn("Sort List", self.settings["leetcode_pending"])

    def test_missing_sheet_id_asks_for_it_instead_of_failing(self):
        with mock.patch.object(database, "get_memory", lambda k: None):
            result = tools.log_leetcode_result("Sort List", "yes")
        self.assertIn("save_memory", result)
        self.assertEqual([], self.appended)


class TestLeetcodeAssignmentAdvances(unittest.TestCase):
    """Regression: progress must advance when a problem is ASSIGNED, not when it's reported.
    While done-ness came only from logging, a user who never reported got the same two
    problems every single morning forever."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self._tmp.name, "leetcode.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(f"P{i} | T{i}\n" for i in range(8)))
        self.settings, self.done = {}, set()
        self._patches = [
            mock.patch.object(tools, "LEETCODE_FILE", path),
            mock.patch.object(database, "get_setting", lambda k: self.settings.get(k)),
            mock.patch.object(database, "set_setting", lambda k, v: self.settings.__setitem__(k, v)),
            mock.patch.object(database, "leetcode_topic_stats", lambda n: []),
            mock.patch.object(database, "leetcode_done_problems", lambda: self.done),
            #Stands in for the real table write: assignment is what grows the done-set
            mock.patch.object(database, "mark_leetcode_assigned",
                              lambda picked: self.done.update(n for n, _ in picked)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_three_mornings_without_reporting_give_three_different_pairs(self):
        days = [[n for n, _ in main._assign_leetcode()[0]] for _ in range(3)]
        flat = [n for day in days for n in day]
        self.assertEqual(6, len(flat))
        self.assertEqual(len(flat), len(set(flat)), f"repeated a problem across days: {days}")

    def test_assignment_is_recorded_before_the_pending_state(self):
        picked, _ = main._assign_leetcode()
        #Both must be marked used even though nothing has been reported
        self.assertEqual({n for n, _ in picked}, self.done)


class TestLeetcodePendingBlock(unittest.TestCase):
    def _block(self, value):
        with mock.patch.object(database, "get_setting", lambda k: value if k == "leetcode_pending" else None):
            return main.leetcode_pending_block()

    def test_nothing_pending_adds_nothing(self):
        self.assertEqual("", self._block(None))
        self.assertEqual("", self._block(""))

    def test_unparseable_value_is_ignored_not_raised(self):
        #A corrupt setting must never break every message's system prompt
        self.assertEqual("", self._block("{not json"))
        self.assertEqual("", self._block('{"problems": []}'))

    def test_renders_problems_and_the_column_order(self):
        block = self._block(json.dumps({"date": "2026-09-02", "problems": [["Sort List", "Linked List"], ["Triangle", "DP"]]}))
        self.assertIn("Sort List", block)
        self.assertIn("Linked List", block)
        self.assertIn("Triangle", block)
        self.assertIn("log_leetcode_result", block)


class TestMorningPromptWithLeetcode(unittest.TestCase):
    def test_both_sections_render(self):
        prompt = main.MORNING_PROMPT.format(
            today="Monday, September 07", tomorrow="Tuesday, September 08",
            schedule_section=main.MORNING_SCHEDULE_SECTION.format(schedule="Monday\n  9:00 AM Class Room 1"),
            leetcode_section=main.MORNING_LEETCODE_SECTION.format(problems="  - Sort List (topic: Linked List)"),
        )
        self.assertIn("Sort List", prompt)
        self.assertIn("Linked List", prompt)
        self.assertIn("Room 1", prompt)

    def test_empty_sections_leave_no_placeholders(self):
        prompt = main.MORNING_PROMPT.format(
            today="Monday, September 07", tomorrow="Tuesday, September 08",
            schedule_section="", leetcode_section="")
        self.assertNotIn("{schedule_section}", prompt)
        self.assertNotIn("{leetcode_section}", prompt)
        self.assertIn("calendar_list_events", prompt)


if __name__ == "__main__":
    unittest.main()

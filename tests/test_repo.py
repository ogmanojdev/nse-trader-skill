import json
import os
import re
import unittest

from _load import COMMON, ROOT, SKILLS

ABOUT = ("Six Claude skills for day trading Indian equities: scan the whole NSE cash market "
         "from the exchange's own data, read the filing feed that moves a stock, replay a trade "
         "before you risk a rupee, and see what it costs after STT.")
# The five skills the original has that this pack deliberately does not: they live in
# Sanyam's risk engine, and a second copy here would be a second risk path.
IN_SANYAM = ["trader-risk", "trader-budget", "trader-broker", "trader-exit", "trader-entry"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Structure(unittest.TestCase):
    def filter_root_entries(self, entries):
        return sorted( 
                d for d in entries
                if (not d.startswith(".") or d == ".claude-plugin") 
                and d != "trader" 
            )
    
    def test_six_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_the_root_is_what_it_should_be(self):
        root = self.filter_root_entries(os.listdir(ROOT))
        self.assertEqual(root, sorted([".claude-plugin", "CONTRIBUTING.md", "LICENSE",
                                       "README.md", "nse-common", "tests", "tools"] + SKILLS))
        
    def test_root_filter_excludes_ds_store(self):
        entries = [
            ".DS_Store",
            ".git",
            ".pytest_cache",
            ".claude-plugin",
            "README.md",
            "tests",
            "trader",
        ]
        filtered = self.filter_root_entries(entries)
        self.assertEqual(filtered, [".claude-plugin", "README.md", "tests"])

    def test_no_us_skill_is_left_behind(self):
        for d in os.listdir(ROOT):
            self.assertFalse(d.startswith("trader-"), "%s is from the US pack" % d)

    def test_nse_common_holds_the_canonical_modules(self):
        have = sorted(f for f in os.listdir(os.path.join(ROOT, "nse-common"))
                      if f.endswith(".py"))
        self.assertEqual(have, sorted(COMMON))


class Frontmatter(unittest.TestCase):
    def test_name_and_a_real_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_a_description_says_nse_or_india_so_it_triggers_on_the_right_market(self):
        for s in SKILLS:
            fm, _t = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = fm.lower()
            self.assertTrue("nse" in low or "indian" in low or "india" in low, s)

    def test_every_skill_carries_the_safety_rule(self):
        for s in SKILLS:
            _fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertTrue("not financial advice" in low,
                            "%s/SKILL.md is missing the advice disclaimer" % s)

    def test_the_order_path_skills_say_no_order_without_a_yes(self):
        for s in SKILLS:
            _fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            if "place an order" in low:
                self.assertTrue("typing yes" in low,
                                "%s/SKILL.md mentions placing an order without saying that "
                                "it takes a typed yes" % s)


class Manifest(unittest.TestCase):
    def test_plugin_points_at_the_root(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "nse-day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        self.assertEqual(plugin["license"], "MIT")

    def test_marketplace_agrees_with_the_plugin(self):
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "nse-day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "nse-day-trader-skill")

    def test_the_licence_is_still_the_originals(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("MIT", text)


class Readme(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            self.readme = " ".join(fh.read().split())

    def test_it_opens_with_the_pack_and_carries_the_blurb(self):
        self.assertTrue(self.readme.startswith("# The NSE Day Trader skill"))
        self.assertIn(ABOUT, self.readme)

    def test_every_skill_is_listed(self):
        for s in SKILLS:
            self.assertIn("/" + s, self.readme, s)

    def test_it_credits_the_original_and_keeps_the_licence(self):
        self.assertIn("Jakeschincariol/day-trader-skill", self.readme)
        self.assertIn("MIT", self.readme)

    def test_it_says_where_the_missing_five_went(self):
        # A reader must not conclude the risk checks were dropped.
        self.assertIn("Sanyam", self.readme)
        for s in IN_SANYAM:
            self.assertIn(s, self.readme, s)

    def test_it_carries_the_fine_print(self):
        for line in ("Not financial advice", "Nothing here can place an order",
                     "A stop is a plan, not a guarantee"):
            self.assertIn(line, self.readme, line)


class House(unittest.TestCase):
    def test_no_em_dashes_outside_the_fixtures(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):
                continue        # real published data, kept exactly as the exchange wrote it
            with open(path, encoding="utf-8", errors="replace") as fh:
                # built, not written, so this file passes its own check
                self.assertNotIn("\u2014", fh.read(), rel)

    def test_the_config_example_is_paper_first_and_cost_aware(self):
        with open(os.path.join(ROOT, "nse-common", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_margin", False), False)
        self.assertIs(cfg.get("allow_options", False), False)
        self.assertEqual(cfg["flat_by"], "15:15")
        # 1.0 would be a losing rule once Indian costs land, so the default is above it
        self.assertGreater(cfg["min_reward_risk"], 1.0)
        self.assertIn("charges", cfg)

    def test_every_tool_takes_offline_so_the_suite_never_needs_the_network(self):
        for skill in SKILLS:
            folder = os.path.join(ROOT, skill)
            tools = [f for f in os.listdir(folder)
                     if f.endswith(".py") and f not in COMMON]
            self.assertTrue(tools, skill)
            for f in tools:
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    self.assertIn("--offline", fh.read(), "%s/%s" % (skill, f))


if __name__ == "__main__":
    unittest.main()

"""crucible grill against the fake hearth-ask.

    python3 -m unittest discover -s adapters/crucible/tests -p 'test_grill*'
"""
import copy
import os
import unittest

from test_grill_env import LEDGER, Env, card


class TestFlow(Env):
    def test_ask_post_cards_answer(self):
        code, r = self.ask("Which name fits?", card())
        self.assertEqual((code, r["state"], r["posted"]), (0, "asked", False))
        q = r["q"]
        code, r = self.grill("post", q)
        self.assertEqual((code, r["posted"], r["hearth_id"]), (0, True, q))
        code, feed = self.grill("cards", "--json")
        self.assertEqual(code, 0)
        c = feed["cards"][0]
        self.assertEqual((c["q"], c["door"], c["weight"], c["mode"]), (q, "two-way", "divergent", "single"))
        self.assertEqual([o["label"] for o in c["parts"][0]["options"]], ["Ember", "Ash"])
        self.assertTrue(c["parts"][0]["options"][0]["rec"])
        self.assertIsInstance(c["stamp"], int)
        self.assertTrue(c["viewer_url"].startswith("https://"))
        self.assertEqual(set(feed), {"cards", "taken", "names"})
        code, r = self.grill("answer", q, "--text", "Ash")
        self.assertEqual(code, 0)
        row = self.fake("get", q)[1]
        self.assertEqual((row["status"], row["option_id"], row["via"]), ("answered", "b", "kiln"))
        ans = self.records("question.answered", q)
        self.assertEqual(len(ans), 1)
        self.assertEqual(ans[0]["body"]["picks"], {"p1": "b"})
        self.assertFalse(ans[0]["body"]["was_recommended"])
        self.assertEqual(self.grill("cards", "--json")[1]["cards"], [])

    def test_batch_answer_maps_lines_to_parts(self):
        for i, (t, labels) in enumerate((("First pick", ("Ember", "Ash")), ("Second pick", ("Moss", "Fern")))):
            self.ask(t, card(t, plain=True, labels=labels))
        code, r = self.grill("sit", "--no-open")
        bq = r["pushed"]["q"]
        self.assertEqual(r["pushed"]["parts"], 2)
        code, _ = self.grill("answer", bq, "--text", "Ash\nMoss")
        self.assertEqual(code, 0)
        row = self.fake("get", bq)[1]
        self.assertEqual(row["answers"], {"p1": "b", "p2": "a"})
        self.assertIn(["--pick", "p1=b"], [self.calls("answer")[0][i:i + 2] for i in range(len(self.calls("answer")[0]))])

    def test_yes_and_unmapped_text(self):
        q = self.ask("Which name fits?", card())[1]["q"]
        self.grill("post", q)
        code, r = self.grill("answer", q, "--text", "maybe the second but louder")
        self.assertEqual(code, 0)
        ans = self.records("question.answered", q)[0]["body"]
        self.assertEqual((ans["kind"], ans["text"]), ("text", "maybe the second but louder"))
        self.assertEqual(self.fake("get", q)[1]["text"], "maybe the second but louder")
        q2 = self.ask("Another name?", card("Another name?"))[1]["q"]
        self.grill("post", q2)
        self.grill("answer", q2, "--text", "Yes")
        self.assertEqual(self.records("question.answered", q2)[0]["body"]["picks"], {"p1": "a"})
        self.assertTrue(self.records("question.answered", q2)[0]["body"]["was_recommended"])


class TestLint(Env):
    def bad(self, mutate, needle):
        c = card()
        mutate(c)
        code, r = self.post_card(c)
        self.assertEqual(code, 1, r)
        self.assertTrue(any(needle in e for e in r["errors"]), r)
        self.assertEqual(self.calls("post"), [])

    def test_label_over_48_characters(self):
        self.bad(lambda c: c["parts"][0]["options"][1].update(label="x" * 49), "49 characters")

    def test_seventh_option(self):
        def seven(c):
            c["parts"][0]["options"] += [{"id": k, "label": "Opt" + k, "changes": "data only"} for k in "cdefg"]
        self.bad(seven, "7 options")

    def test_ninth_part(self):
        c = card(plain=True)
        c["parts"] = [dict(copy.deepcopy(c["parts"][0]), id=f"p{i}") for i in range(1, 10)]
        code, r = self.post_card(c)
        self.assertEqual(code, 1)
        self.assertTrue(any("9 parts" in e for e in r["errors"]))
        self.assertEqual(self.calls("post"), [])

    def test_missing_recommendation(self):
        self.bad(lambda c: c["parts"][0]["options"][0].pop("rec"), "exactly one recommended")

    def test_recommended_not_first(self):
        def swap(c):
            o = c["parts"][0]["options"]
            o[0].pop("rec")
            o[1]["rec"] = True
        self.bad(swap, "posted first")

    def test_duplicate_labels(self):
        self.bad(lambda c: c["parts"][0]["options"][1].update(label="ember"), "duplicate label")

    def test_more_than_three_words_and_long_question(self):
        self.bad(lambda c: c["parts"][0]["options"][1].update(label="one two three four"), "words")
        self.bad(lambda c: c.update(text="q" * 91), "91 characters")

    def test_door_weight_and_number_rules(self):
        self.bad(lambda c: c.pop("door"), "door")
        self.bad(lambda c: c.pop("weight"), "weight")
        self.bad(lambda c: c["parts"][0]["options"][1].update(changes="costs 40 hours"), "number 40")
        c = card(evidence={"hours": 40})
        c["parts"][0]["options"][1]["changes"] = "costs 40 hours"
        self.assertEqual(self.post_card(c)[0], 0)

    def test_default_after_never_on_one_way_or_money(self):
        for kw in ({"door": "one-way"}, {"money": True, "class": "money"}):
            c = card(default_after="24h", **kw)
            code, r = self.post_card(c)
            self.assertEqual(code, 1, r)
            self.assertTrue(any("default-after" in e for e in r["errors"]))
        self.assertEqual(self.calls("post"), [])

    def test_argv_has_no_default_after_for_one_way_or_money(self):
        self.assertEqual(self.post_card(card("One way name", door="one-way"), "a.json")[0], 0)
        self.grill("answer", self.records("question.asked")[0]["body"]["q_id"], "--text", "Ember")
        self.assertEqual(self.post_card(card("Money pick", money=True, weight="outcome-critical"), "b.json")[0], 0)
        posts = self.calls("post")
        self.assertEqual(len(posts), 2)
        self.assertTrue(all("--default-after" not in p for p in posts))
        two_way = card("Soft pick", default_after="24h")
        self.grill("answer", self.records("question.asked")[1]["body"]["q_id"], "--text", "Ember")
        self.assertEqual(self.post_card(two_way, "c.json")[0], 0)
        self.assertIn("--default-after", self.calls("post")[2])

    def test_tree_check(self):
        self.assertEqual(self.grill("tree", "--check")[0], 0)
        import tomllib
        from test_grill_env import BASE
        with open(os.path.join(BASE, "trees", "game.v1.toml"), "rb") as f:
            t = tomllib.load(f)
        with open(os.path.join(BASE, "trees", "game.v1.toml")) as f:
            text = f.read()
        bad = os.path.join(self.tmp, "bad.toml")
        a, b = t["node"][0]["id"], t["node"][1]["id"]
        with open(bad, "w") as f:
            f.write(text + f'\n[[node]]\nid = "frame.loopa"\nask = "x?"\nclass = "owner"\ndoor = "two-way"\n'
                    f'weight = "benign"\nprereqs = ["frame.loopb"]\n[[node]]\nid = "frame.loopb"\nask = "y?"\n'
                    f'class = "owner"\ndoor = "two-way"\nweight = "benign"\nprereqs = ["frame.loopa"]\n')
        code, r = self.grill("tree", "--check", "--file", bad)
        self.assertEqual(code, 1)
        self.assertTrue(any("cycle" in e for e in r["errors"]))


class TestPost(Env):
    def test_repost_issues_no_second_post(self):
        q = self.ask("Which name fits?", card())[1]["q"]
        self.assertEqual(self.grill("post", q)[1]["posted"], True)
        code, r = self.grill("post", q)
        self.assertEqual((code, r["posted"], r["unchanged"]), (0, False, True))
        self.assertEqual(len(self.calls("post")), 1)
        self.assertEqual(len(self.records("question.asked", q)), 1)

    def test_description_part_posts_as_single_decision(self):
        c = card("Which mood?", plain=False)
        self.assertEqual(self.post_card(c)[0], 0)
        argv = self.calls("post")[0]
        self.assertIn("--option", argv)
        self.assertNotIn("--part", argv)
        self.assertTrue(any(a.startswith("a:Ember:data only") for a in argv))

    def test_description_part_never_joins_a_batch(self):
        c = card("Two picks", plain=True)
        c["parts"].append(dict(copy.deepcopy(c["parts"][0]), id="p2", plain=False))
        code, r = self.post_card(c)
        self.assertEqual(code, 1)
        self.assertTrue(any("single decision" in e for e in r["errors"]))
        self.assertEqual(self.calls("post"), [])

    def test_sitting_batches_plain_parts_and_leaves_description_part_single(self):
        a = self.ask("Pick a mark", card("Pick a mark", plain=True, labels=("Ember", "Ash")))[1]["q"]
        b = self.ask("Pick a mood", card("Pick a mood", plain=False, labels=("Calm", "Tense"), weight="divergent"),
                     weight="outcome-critical")[1]["q"]
        c = self.ask("Pick a tint", card("Pick a tint", plain=True, labels=("Moss", "Fern")))[1]["q"]
        code, r = self.grill("sit", "--no-open")
        self.assertEqual(code, 0, r)
        # the description part ranks first by weight, so it posts alone as a single decision
        self.assertEqual(r["pushed"]["q"], b)
        argv = self.calls("post")[0]
        self.assertIn("--option", argv)
        self.assertNotIn("--part", argv)
        self.assertEqual(self.grill("sit", "--no-open")[0], 3)
        self.grill("answer", b, "--text", "Calm")
        code, r = self.grill("sit", "--no-open")
        self.assertEqual(r["pushed"]["parts"], 2)
        batch = self.calls("post")[1]
        self.assertEqual(batch.count("--part"), 2)
        self.assertNotIn("--option", batch)
        self.assertFalse(any("Calm" in x or "Tense" in x for x in batch))
        self.assertEqual(self.records("question.asked", r["pushed"]["q"])[0]["body"]["mode"], "batch")
        self.assertEqual({a, c}, {p["src"] for p in self.records("question.asked", r["pushed"]["q"])[0]["body"]["parts"]})

    def test_one_unanswered_push_at_a_time(self):
        c1, c2 = card("First single", weight="outcome-critical"), card("Second single")
        self.assertEqual(self.post_card(c1, "1.json")[0], 0)
        code, r = self.post_card(c2, "2.json")
        self.assertEqual(code, 3, r)
        self.assertFalse(r["posted"])
        self.assertEqual(len(self.calls("post")), 1)
        q1 = self.records("question.asked")[0]["body"]["q_id"]
        self.grill("answer", q1, "--text", "Ember")
        q2 = self.records("question.raised")[1]["body"]["q_id"]
        code, r = self.grill("post", q2)
        self.assertEqual((code, r["posted"]), (0, True))
        self.assertEqual(len(self.calls("post")), 2)

    def test_budget_knob_caps_the_sitting(self):
        os.makedirs(os.path.join(self.root, "design"), exist_ok=True)
        with open(os.path.join(self.root, "design", "knobs.toml"), "w") as f:
            f.write("[sitting]\nbudget = 1\n")
        for t in ("Pick a mark", "Pick a tint", "Pick a tone"):
            self.ask(t, card(t, plain=True, labels=("Ember", "Ash")))
        code, r = self.grill("sit", "--no-open")
        self.assertEqual(len(r["queued"]), 1)
        self.assertEqual(r["pushed"]["mode"], "single")

    def test_label_rename_withdraws_and_reposts_under_dot_two(self):
        self.append("label.created", {"lbl_id": "lbl-AAA", "kind": "mechanic", "text": "Ember"})
        r = self.ask("Which name fits?", card(labels=("{{l:lbl-AAA}}", "Ash")))
        q = r[1]["q"]
        self.grill("post", q)
        self.append("label.edited", {"lbl_id": "lbl-AAA", "text": "Cinder"})
        code, r = self.grill("post", q)
        self.assertEqual((code, r["posted"], r["hearth_id"]), (0, True, q + ".2"))
        self.assertEqual(self.fake("get", q)[1]["status"], "withdrawn")
        self.assertEqual(self.fake("get", q + ".2")[1]["options"][0]["label"], "Cinder")
        self.assertEqual(self.records("question.asked", q)[-1]["body"]["post_n"], 2)
        names = self.grill("cards", "--json")[1]
        self.assertEqual(names["cards"][0]["parts"][0]["options"][0]["label"], "Cinder")
        self.assertEqual(names["names"][0]["text"], "Cinder")
        self.assertEqual(self.grill("post", q)[1]["unchanged"], True)


class TestWords(Env):
    def posted(self, text="Which name fits?"):
        q = self.ask(text, card(text))[1]["q"]
        self.assertEqual(self.grill("post", q)[0], 0)
        return q

    def test_you_decide_is_delegate_without_a_model(self):
        q = self.posted()
        code, r = self.grill("answer", q, "--text", "You decide")
        self.assertEqual((code, r["records"]), (0, "delegate"))
        self.assertEqual(len(self.records("question.delegated", q)), 1)
        d = self.records("decision.settled", q)[0]["body"]
        self.assertEqual((d["authority"], d["chosen"]), ("lead", {"p1": "a"}))
        self.assertEqual(self.records("question.answered", q), [])
        self.assertEqual(self.fake("get", q)[1]["status"], "answered")

    def test_later_is_deferred_with_a_wake_condition(self):
        q = self.posted()
        code, r = self.grill("answer", q, "--text", "later")
        self.assertEqual(code, 0)
        rec = self.records("question.deferred", q)[0]["body"]
        self.assertIn("date", rec["wake"])
        code, due = self.grill("due", "--now", "2999-01-01T00:00:00Z")
        self.assertEqual(due["due"], [{"kind": "deferred", "q": q}])
        self.assertEqual(self.grill("due", "--now", "2000-01-01T00:00:00Z")[1]["due"], [])

    def test_test_it_starts_an_investigation(self):
        q = self.posted()
        self.assertEqual(self.grill("answer", q, "--text", "Test it")[0], 0)
        inv = self.records("investigation.started", q)
        self.assertEqual(len(inv), 1)
        self.assertTrue(inv[0]["body"]["inv_id"].startswith("inv-"))

    def test_wrong_question_withdraws_with_the_note(self):
        q = self.posted()
        self.assertEqual(self.grill("answer", q, "--text", "wrong question: ask about pace")[0], 0)
        w = self.records("question.withdrawn", q)[0]["body"]
        self.assertEqual((w["note"], w["redraft"]), ("ask about pace", True))
        self.assertEqual(self.fake("get", q)[1]["status"], "withdrawn")

    def test_words_call_no_model(self):
        # the grill package imports no model runner and shells out only to hearth-ask
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = ""
        for n in os.listdir(os.path.join(base, "crucible_grill")):
            if n.endswith(".py"):
                with open(os.path.join(base, "crucible_grill", n)) as f:
                    src += f.read()
        for banned in ("claude", "agy", "crucible model", "anthropic"):
            self.assertNotIn(banned, src.lower())


class TestGate(Env):
    def test_guard_question_is_never_taken(self):
        c = card("Which look fits?", weight="benign")
        code, r = self.ask("Which look fits?", c, ("--guard", "pillar"), weight="benign")
        self.assertEqual((code, r["state"]), (0, "asked"))
        code, r2 = self.grill("take", r["q"])
        self.assertEqual(code, 1)
        self.assertIn("guard", r2["error"])
        self.assertEqual(self.records("question.taken"), [])
        # a guard word in the text raises it too
        code, r3 = self.ask("Should we change the save format?", card("Change the save format?", weight="benign"),
                            weight="benign")
        self.assertEqual(r3["state"], "asked")
        # a guarded tree node ends as a card
        code, r4 = self.grill("ask", "--node", "pillar.set", "--draft",
                              self.write("p.json", card("Which pillars?", weight="benign")))
        self.assertEqual(r4["state"], "asked")

    def test_two_way_benign_owner_question_is_taken_and_listed(self):
        code, r = self.ask("Which tint fits?", card("Which tint fits?", weight="benign", why="same family", pips=2),
                           weight="benign")
        self.assertEqual((code, r["state"], r["taken"]), (0, "settled", True))
        self.assertEqual(self.calls("post"), [])
        taken = self.grill("cards", "--json")[1]["taken"]
        self.assertEqual(len(taken), 1)
        self.assertEqual((taken[0]["d"], taken[0]["what"], taken[0]["why"], taken[0]["doubt"]),
                         (r["decision"], "Which tint fits?", "same family", 1))
        code, f = self.grill("flip", r["decision"])
        self.assertEqual(code, 0)
        self.assertEqual(self.grill("cards", "--json")[1]["taken"], [])
        d = self.records("decision.settled")[-1]["body"]
        self.assertEqual((d["chosen"], d["authority"], d["supersedes"]), ({"p1": "b"}, "owner", r["decision"]))
        self.assertEqual(self.run_cli(LEDGER, "build", "--root", self.root)[0], 0)
        self.assertEqual(self.run_cli(LEDGER, "doctor", "--root", self.root)[0], 0)

    def test_gate_table_rows(self):
        cases = [("fact", "two-way", "benign", "routed"), ("measurable", "one-way", "divergent", "routed"),
                 ("owner", "one-way", "benign", "asked"), ("money", "two-way", "benign", "asked"),
                 ("owner", "two-way", "divergent", "asked")]
        for cls, door, weight, state in cases:
            code, r = self.ask(f"Q for {cls} {door}", card(f"Q for {cls} {door}", door=door, weight=weight), cls=cls,
                               door=door, weight=weight, extra=())
            self.assertEqual((code, r["state"]), (0, state), (cls, door, weight, r))
        code, r = self.ask("A lead call", card("A lead call", weight="benign"), cls="lead", weight="benign")
        self.assertEqual(r["state"], "settled")

    def test_off_tree_class_needs_a_clause(self):
        code, r = self.grill("ask", "--text", "x?", "--class", "owner")
        self.assertEqual(code, 2)
        code, r = self.grill("ask", "--text", "x?", "--class", "owner", "--clause", "nope")
        self.assertEqual(code, 2)

    def test_settled_question_is_not_asked_twice(self):
        c = card("Which tint fits?", weight="benign")
        self.ask("Which tint fits?", c, weight="benign")
        code, r = self.ask("Which tint fits?", c, weight="benign")
        self.assertEqual((r["state"], r["already"]), ("settled", True))

    def test_tree_node_inherits_gate_and_frontier_opens(self):
        code, r = self.grill("ask", "--node", "market.comparables")
        self.assertEqual((code, r["state"], r["route"]), (0, "routed", "lookup"))
        code, r = self.grill("sit")
        self.assertEqual(code, 0)
        self.assertTrue(r["opened"])
        self.assertIsNone(r["pushed"])
        self.assertTrue(r["undrafted"] or r["opened"])


class TestProbe(Env):
    def test_probe_is_the_last_part_and_an_override_flips_the_take(self):
        taken = self.ask("Which tint fits?", card("Which tint fits?", plain=True, weight="benign", labels=("Moss", "Fern")),
                         weight="benign")[1]
        self.assertEqual(taken["state"], "settled")
        for t in ("Pick a mark", "Pick a tone"):
            self.ask(t, card(t, plain=True, labels=("Ember", "Ash")))
        code, r = self.grill("sit", "--no-open")
        self.assertTrue(r["pushed"]["probe"])
        bq = r["pushed"]["q"]
        parts = self.records("question.asked", bq)[0]["body"]["parts"]
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[-1]["probe"])
        self.assertEqual(parts[-1]["src"], taken["q"])
        self.assertEqual(self.grill("cards", "--json")[1]["taken"][0]["d"], taken["decision"])
        self.grill("answer", bq, "--text", "Ember\nEmber\nFern")
        ans = self.records("question.answered", bq)[0]["body"]
        self.assertEqual((ans["probe"], ans["probe_parts"], ans["probe_overrides"]), (True, ["p3"], ["p3"]))
        self.assertEqual(self.grill("cards", "--json")[1]["taken"], [])
        self.assertEqual(self.records("question.flipped")[0]["body"]["q_id"], taken["q"])
        # the probe is asked once per question
        self.ask("Pick again", card("Pick again", plain=True, labels=("Ember", "Ash")))
        self.ask("Pick once more", card("Pick once more", plain=True, labels=("Ember", "Ash")))
        code, r = self.grill("sit", "--no-open")
        self.assertFalse(r["pushed"]["probe"])


if __name__ == "__main__":
    unittest.main()

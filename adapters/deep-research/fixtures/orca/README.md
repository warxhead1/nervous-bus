Snapshots replayed by the fake Orca runner in `test_dr.py`.

`composer`, `tools_menu`, `composer_dr` and `ready_to_send` were recaptured 2026-11-27 after Gemini dropped the Deselect chip (`legacy_composer_dr.txt` keeps the old one); the account link was removed. All files except `synthetic_failed.txt` are real accessibility snapshots captured from a signed-in gemini.google.com on 2026-11-26, with the sidebar (chat titles) and account block removed and page and conversation ids replaced by placeholders. `signed_out_tools_menu.txt` was captured signed out. `report_dom_result.json` is the markdown that `REPORT_JS` extracted from the finished report. `done.txt` is cut after the report panel toolbar.

`synthetic_failed.txt` is hand-written: no failed run was observed, so its text is a guess at Gemini's failure wording (`FAILED_TEXT`).

Notebook flow, in order: `nb_splash` (first visit only), `nb_create_form`, `nb_create_form_filled`, `nb_empty_notebook`, `nb_sources_menu`, `nb_text_dialog` or `nb_links_dialog`, `nb_with_sources`.

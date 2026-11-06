# TEST-RESULTS

**Build status: NOT complete.** Every test that can run without the T320 passes
(below). The brief says the build is not complete until *every* acceptance
test passes, and several of them need the real CT, network, Cloudflare Access,
Immich and her phone. Those are listed under **Pending on the CT** with the
exact command or step for each.

Run on 2026-11-06 in a development sandbox (Python 3.13 + the pinned packages;
the CT runs Python 3.11 with the same pins). Meilisearch, Immich and the web
are simulated with in-process fakes; Chromium is real.

## Summary
| Suite | Result |
|---|---|
| `pytest tests/` — unit + acceptance with fakes | **66 passed, 0 failed** |
| `tests/e2e/run.sh` — real Chromium at 360 px against the real app | **31 passed, 0 failed** |
| `pip-audit` on `backend/requirements.lock` | **No known vulnerabilities found** (python-multipart 0.0.20 was flagged and bumped to 0.0.33) |
| `shellcheck -S warning scripts/*.sh` | clean |
| `nft -c` on the rendered firewall | syntax OK |

## Acceptance tests from the brief
| Brief test | Status | Evidence |
|---|---|---|
| **Access and security** | | |
| Request without a valid Access JWT returns 401/403 | PASS (local) · pending on CT | `test_no_jwt_is_401_everywhere`; `verify.sh` |
| Forged, expired, or wrong-audience JWT rejected | PASS (local) | `test_forged_expired_wrong_audience_issuer_algorithm_rejected` (also wrong issuer, unknown kid, HS256 confusion, no email) |
| Child account cannot open /parent or parent APIs | PASS (local) | `test_child_cannot_open_parent_page_or_apis`, e2e "child gets 403 on /parent" |
| Ports 7700 and 2283 not reachable through the app or browser | PASS (local) · pending on CT | `test_internal_services_not_reachable_through_app`; `verify.sh` bind + firewall checks |
| App not reachable on 8080 from an unauthorized LAN host | **pending on CT** | nftables rule; from another LAN host `curl -m 5 http://<CT-IP>:8080/` must time out |
| Script tags in crawled content appear as plain text | PASS (local) | `test_crawled_script_text_is_returned_as_inert_text`, `test_stored_text_is_text_not_markup`, e2e XSS probe |
| Responses include the required security headers | PASS (local) · pending on CT | `test_security_headers_on_every_response`; `verify.sh` |
| No API key, Immich URL, asset path, GPS, EXIF in browser responses | PASS (local) | `test_no_immich_details_reach_the_browser`, `test_secrets_never_in_responses`, `test_thumbnails_have_no_exif_or_gps` |
| **Crawler** | | |
| Redirect from approved to unapproved host is not indexed | PASS (local) | `test_redirect_to_unapproved_host_is_not_indexed` (the unapproved hop is never fetched) |
| Hostname resolving to a private address is blocked | PASS (local) · pending on CT | `test_hostname_resolving_to_private_address_is_blocked`, `test_default_policy_refuses_loopback_before_connecting`; plus nftables egress |
| An http:// link is not followed | PASS (local) | `test_http_links_are_not_followed`, `test_redirect_to_http_is_refused` |
| robots.txt rules are followed | PASS (local) | `test_robots_rules_are_followed` |
| Blocked or challenged site is reported, not bypassed | PASS (local) | `test_site_that_blocks_crawling_is_marked_blocked_not_bypassed`, `test_challenged_site_is_reported_not_bypassed` |
| Per-host limits and delays are enforced | PASS (local) | `test_per_host_limits_and_delays_are_enforced`, `test_crawl_delay_from_robots_is_honoured` |
| **Search and content** | | |
| Every result URL exactly matches an approved host | PASS (local) | `test_every_result_url_exactly_matches_an_approved_host` (stale, apex-domain, http, mismatched-host, javascript:, port docs all dropped) |
| Removing a host hides its results before the next page load | PASS (local) | `test_removing_a_host_hides_results_before_next_page_load` (works even with index deletes disabled) |
| Blocked terms produce the safe message | PASS (local) | `test_blocked_terms_produce_the_safe_message`, e2e |
| Empty results never fall back to the wider web | PASS (local) | `test_empty_results_never_fall_back_to_the_wider_web`, e2e |
| Each tile performs its configured action | PASS (local) | `test_each_tile_performs_its_configured_action` (all 12 tiles), `test_tile_sections_filter_down_and_stay_default_deny`, `test_concise_tiles_open_one_approved_page`, e2e Space tile + Broad/Concise editor |
| **Photos** | | |
| Only photos in approved albums can appear | PASS (local) · pending with real Immich | `test_only_photos_in_approved_albums_appear` |
| A guessed opaque ID returns 404 | PASS (local) | `test_guessed_opaque_id_returns_404` |
| Removing an album immediately returns 404 for its thumbnails | PASS (local) | `test_removing_an_album_immediately_404s_its_thumbnails`, `test_album_disabled_by_hand_edit_is_revoked_even_with_stale_rows` |
| Downloaded thumbnails contain no EXIF or GPS metadata | PASS (local) | `test_thumbnails_have_no_exif_or_gps` (source JPEG carried GPS + camera EXIF) |
| **Phone and recovery** | | |
| Login, search, tiles, photos, links work in Chrome on her phone, Wi-Fi and cellular | **pending — David, on her phone** | after the manual Cloudflare + Family Link steps |
| No horizontal scrolling at 360 px | PASS (local, Chromium) | e2e: home, results, parent page at 360 px |
| Services restart after a CT reboot | **pending on CT** | `pct reboot <CTID>` then `scripts/verify.sh` (all units are `enabled`) |
| Index can be rebuilt from configuration without data loss | PASS (local) · pending on CT | `test_index_rebuilds_from_config_and_sqlite`, `test_reindex_from_page_store`; on CT `scripts/rebuild-index.sh` |
| Restoring the backup restores sites, tiles, library entries, settings | PASS (local, real scripts) | `test_backup_then_restore_round_trip` runs `backup.sh` and `restore.sh` |

## Pending on the CT (in order)
1. `bash scripts/preflight.sh` on the host → PREFLIGHT.md → David approves → `create-ct.sh --yes`.
2. Inside the CT: `bash /root/family-search-src/scripts/install.sh`.
3. Fill the placeholders it lists (see README), re-run `install.sh`.
4. `bash /opt/family-search/scripts/verify.sh` — paste its output here.
5. From another LAN host: `curl -m 5 http://<CT-IP>:8080/` → must time out.
6. `pct reboot <CTID>`; run `verify.sh` again.
7. `scripts/rebuild-index.sh`, then search still works.
8. `scripts/backup.sh` then `scripts/restore.sh <file>` on the CT.
9. David's manual Immich / Cloudflare / Family Link steps; then the phone test on Wi-Fi and on cellular.
10. Watch the first crawls over several nights; any host marked `blocked`, `challenged` or `needs_review` on /parent → David reviews it.

## Raw output — pytest
```
test_access_security.py · test_no_jwt_is_401_everywhere — PASS
test_access_security.py · test_forged_expired_wrong_audience_issuer_algorithm_rejected — PASS
test_access_security.py · test_valid_child_and_parent — PASS
test_access_security.py · test_identity_header_spoofing_is_ignored — PASS
test_access_security.py · test_child_cannot_open_parent_page_or_apis — PASS
test_access_security.py · test_parent_can_open_parent_page — PASS
test_access_security.py · test_denied_events_are_audited — PASS
test_access_security.py · test_csrf_and_origin_required_for_writes — PASS
test_access_security.py · test_csrf_token_is_bound_to_the_user — PASS
test_access_security.py · test_security_headers_on_every_response — PASS
test_access_security.py · test_no_store_on_search_parent_and_photo_responses — PASS
test_access_security.py · test_unconfigured_app_fails_closed — PASS
test_access_security.py · test_internal_services_not_reachable_through_app — PASS
test_access_security.py · test_methods_other_than_get_post_refused — PASS
test_access_security.py · test_query_length_and_rate_limits — PASS
test_access_security.py · test_docs_and_openapi_disabled — PASS
test_backup_restore.py · test_backup_then_restore_round_trip — PASS
test_crawler.py · test_redirect_to_unapproved_host_is_not_indexed — PASS
test_crawler.py · test_redirect_to_http_is_refused — PASS
test_crawler.py · test_hostname_resolving_to_private_address_is_blocked — PASS
test_crawler.py · test_http_links_are_not_followed — PASS
test_crawler.py · test_robots_rules_are_followed — PASS
test_crawler.py · test_site_that_blocks_crawling_is_marked_blocked_not_bypassed — PASS
test_crawler.py · test_challenged_site_is_reported_not_bypassed — PASS
test_crawler.py · test_per_host_limits_and_delays_are_enforced — PASS
test_crawler.py · test_crawl_delay_from_robots_is_honoured — PASS
test_crawler.py · test_depth_limit_skip_rules_tracking_and_canonical — PASS
test_crawler.py · test_stored_text_is_text_not_markup — PASS
test_crawler.py · test_non_html_and_oversized_responses_are_skipped — PASS
test_crawler.py · test_removed_host_is_purged_on_next_run — PASS
test_crawler.py · test_exact_hostname_only — PASS
test_crawler.py · test_crawl_status_written_for_parent_page — PASS
test_crawler.py · test_reindex_from_page_store — PASS
test_crawler.py · test_url_helpers — PASS
test_crawler.py · test_wikitext_stripping — PASS
test_crawler.py · test_indexed_pages_carry_section_prefixes — PASS
test_photos.py · test_only_photos_in_approved_albums_appear — PASS
test_photos.py · test_photo_search_by_caption_album_and_date — PASS
test_photos.py · test_browser_sees_only_opaque_ids — PASS
test_photos.py · test_guessed_opaque_id_returns_404 — PASS
test_photos.py · test_removing_an_album_immediately_404s_its_thumbnails — PASS
test_photos.py · test_album_disabled_by_hand_edit_is_revoked_even_with_stale_rows — PASS
test_photos.py · test_thumbnails_have_no_exif_or_gps — PASS
test_photos.py · test_only_permitted_immich_endpoints_are_called — PASS
test_photos.py · test_no_immich_details_reach_the_browser — PASS
test_photos.py · test_photo_rate_limit — PASS
test_safefetch_tls.py · test_pinned_tls_fetch_and_cert_check — PASS
test_safefetch_tls.py · test_default_policy_refuses_loopback_before_connecting — PASS
test_search_content.py · test_every_result_url_exactly_matches_an_approved_host — PASS
test_search_content.py · test_removing_a_host_hides_results_before_next_page_load — PASS
test_search_content.py · test_blocked_terms_produce_the_safe_message — PASS
test_search_content.py · test_suggestions_respect_blocks_and_approval — PASS
test_search_content.py · test_empty_results_never_fall_back_to_the_wider_web — PASS
test_search_content.py · test_crawled_script_text_is_returned_as_inert_text — PASS
test_search_content.py · test_snippets_are_short_and_never_the_full_body — PASS
test_search_content.py · test_each_tile_performs_its_configured_action — PASS
test_search_content.py · test_library_items — PASS
test_search_content.py · test_ask_a_grownup_saves_only_on_tap — PASS
test_search_content.py · test_search_history_off_by_default_and_parent_toggle — PASS
test_search_content.py · test_parent_changes_are_audited_and_backed_up — PASS
test_search_content.py · test_tiles_editable_without_code_changes — PASS
test_search_content.py · test_index_rebuilds_from_config_and_sqlite — PASS
test_search_content.py · test_search_unavailable_is_a_calm_503 — PASS
test_search_content.py · test_secrets_never_in_responses — PASS
test_search_content.py · test_tile_sections_filter_down_and_stay_default_deny — PASS
test_search_content.py · test_concise_tiles_open_one_approved_page — PASS
```

## Raw output — Chromium e2e (360 px)
```
PASS  home page loads in under 1 s (local)  (141 ms)
PASS  12 tiles in a 2-column grid at 360px
PASS  home: no horizontal scroll at 360px
PASS  home: tap targets >= 44px  ([])
PASS  results render
PASS  results: no horizontal scroll at 360px
PASS  script tags in crawled text appear as plain text  ([null,"<script>window.__pwned=1</script> Moon trick"])
PASS  no injected script/img elements
PASS  result links are https:// with rel=noopener noreferrer
PASS  numbered pagination
PASS  All tab shows a compact photo strip when photos match
PASS  Photos tab: thumbnail grid loads
PASS  full-screen viewer opens
PASS  viewer closes with Escape
PASS  blocked search shows the calm message
PASS  empty results: no outside results, offers Ask a grown-up
PASS  Ask a grown-up saves the request on tap
PASS  Space tile filters to space sites
PASS  child pages: no CSP violations, no dialogs
PASS  4-column tiles on desktop
PASS  parent page lists the 27 approved sites
PASS  parent: no horizontal scroll at 360px
PASS  parent sees the child's Ask-a-grown-up request
PASS  parent page: no CSP violations
PASS  tile editor shows sections and a page count
PASS  each tile has the Broad / Concise switch
PASS  flipping to Concise shows the one-link field
PASS  Concise warns when the site isn't approved
PASS  flipping back to Broad keeps the sections
PASS  tile editor: no horizontal scroll at 360px
PASS  child gets 403 on /parent
```

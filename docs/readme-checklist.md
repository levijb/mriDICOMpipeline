# README checklist (Goal 7)

Each guarantee the README makes, and the test or deployment check that
establishes it. Review this list whenever the README changes.

| README guarantee | Evidence |
|---|---|
| Python 3.11+, Linux and Windows | CI `test` job (Python 3.11-3.14, ubuntu-latest and windows-latest) |
| Case-only duplicate roster IDs are ignored with a warning | `test_identity.py::test_case_only_id_collision_ignored_with_warning` |
| Reference CSV: the three date formats; invalid rows are ignored; a valid ID with an invalid or missing date still registers the ID | `test_identity.py::test_roster_date_formats_and_row_shapes`, `test_roster_that_cannot_be_used_is_refused`, `test_unusable_roster_exits_2_before_anything_is_written` |
| `prepare` checks the reference only for ZIPs it works on; a roster issue never outlives its cause | `test_inputs.py::test_roster_issue_never_outlives_its_cause`, `test_removed_roster_row_is_flagged_by_convert_not_by_prepare` |
| Unknown IDs are held in Uploads; `--allow-unlisted-dates` is needed on both commands | `test_inputs.py::test_zip_whose_id_differs_from_the_roster_by_case_is_held`, `test_unlisted_date_needs_the_option_on_both_commands` |
| Windows device names are rejected as IDs | `test_identity.py::test_reserved_device_names_rejected`, `test_rejected_zip_names` |
| ZIP names: ID characters, eight-digit calendar dates | `test_identity.py::test_accepted_zip_names`, `test_rejected_zip_names` |
| `_N` suffix rules | `test_identity.py::test_session_key_grammar`, `test_rejected_zip_names` |
| A partly copied ZIP fails and is retried when complete | `test_inputs.py::test_truncated_upload_fails_then_prepares_when_complete` |
| A ZIP with no DICOM objects (empty, viewer files only, a nested ZIP) fails and stays in place | `test_inputs.py::test_zip_without_dicom_objects_fails_and_is_retried_when_replaced` |
| ZIP members are streamed to completion to check their CRCs | `test_inputs.py::test_member_with_bad_crc_fails_the_zip` |
| Obviously short uncompressed image objects are rejected | `test_inputs.py::test_image_shorter_than_its_declared_size_fails_the_zip` |
| ZIPs are moved (or prepared in place in SubjectsSorted), never modified or deleted; unaccepted ZIPs stay | `test_inputs.py::test_sorted_zips_are_prepared_in_place_and_never_deleted`, `test_pipeline.py::test_duplicates_and_conflicts_are_cached` |
| An accepted canonical ZIP is immutable; outputs stay unchanged | `test_inputs.py::test_changed_canonical_zip_is_held_and_outputs_unchanged`, `test_canonical_conflict_blocks_conversion` |
| A restored canonical ZIP is accepted without extracting again | `test_inputs.py::test_restored_canonical_zip_accepted_without_reextract` |
| `--force-reextract` never accepts changed bytes | `test_inputs.py::test_force_reextract_refuses_changed_bytes` |
| A missing canonical ZIP gets a nonblocking issue | `test_inputs.py::test_missing_canonical_zip_nonblocking_issue` |
| Byte-identical repeats are stored once; conflicting repeats and multi-study ZIPs are held | `test_inputs.py::test_repeated_identical_instances_written_once`, `test_conflicting_repeated_instance_held`, `test_multi_study_held_until_accepted` |
| Free-space check before extraction | `test_inputs.py::test_free_space_check_refuses_extraction_and_keeps_zip` |
| `prepare --audit` re-hashes canonical ZIPs; a plain run keeps the hold, the next `--audit` clears it | `test_inputs.py::test_audit_rehashes_canonical_zip` |
| A moved project is refused | `test_inputs.py::test_moved_project_refused` |
| `--session` limits both commands | `test_inputs.py::test_session_filter_limits_prepare`, `test_conversion.py::test_session_filter_limits_work` |
| `--limit N` selects work after cache checks | `test_project_configuration.py::test_limit_selects_new_work_after_cache_checks` |
| Interrupted work is retried automatically, except an interrupted explicit reconversion | `test_recovery.py::test_interrupted_conversion_is_retried_by_the_next_plain_run`, `test_interrupted_explicit_reconversion_is_not_retried` |
| Worker default is min(4, SLURM_CPUS_PER_TASK), or one; `--workers` cannot exceed SLURM_CPUS_PER_TASK | `test_project_configuration.py::test_worker_count_defaults_and_slurm_cap`, `test_invalid_settings_exit_2_before_anything_is_written` |
| Cached reruns read no scan contents | `test_pipeline.py::test_1000_cached_sessions_do_not_read_scan_contents` |
| `converted` requires every required series; non-image series are excused | `test_conversion.py::test_missing_required_series_is_partial`, `test_inputs.py::test_non_image_series_extracted_and_excused`, `test_other_sop_classes_are_extracted_and_excused`, `test_real_converter.py::test_real_converter_with_non_image_object_converts`, `test_real_converter_with_other_sop_classes_converts`, `test_real_converter_unconvertible_series_makes_session_partial` |
| A nonzero converter exit, a timeout, no valid volumes or unresolved artifacts cannot become a successful cache entry | `test_conversion.py::test_converter_timeout_is_not_success_and_is_not_retried`, `test_organizer_reports_output_it_cannot_place`, `test_pipeline.py::test_partial_outputs_retained_and_retry`, `test_zero_output_is_not_success` |
| All converted series and components are retained (echoes, phase, unstacked orientations) | `test_conversion.py::test_organizer_publishes_each_named_variant_under_its_series_key`, `test_real_converter.py::test_real_converter_series_split_into_several_volumes` |
| Published NIfTI decompresses to the converter's bytes | `test_conversion.py::test_published_nifti_is_byte_identical_to_converter_output`, `test_real_converter.py::test_real_converter_output_byte_identical` |
| A complete session is never reconverted automatically | `test_conversion.py::test_complete_session_not_reconverted_when_folder_modified`, `test_pipeline.py::test_deleted_dicom_reextracted_and_deleted_nifti_flagged` |
| A complete folder that disappeared is rebuilt by `--force-reconvert --session` | `test_conversion.py::test_force_reconvert_rebuilds_a_deleted_folder` |
| `convert --audit --session` revalidates a restored folder | `test_conversion.py::test_restored_folder_revalidated_by_audit_session` |
| An explicit reconversion refuses folders with added files | `test_conversion.py::test_force_reconvert_refuses_folder_with_extra_files`, `test_force_reconvert_after_lost_state_keeps_added_files` |
| A failed reconversion keeps the complete folder and records the attempt | `test_conversion.py::test_failed_reconversion_keeps_last_good`, `test_status_columns_and_last_attempt_reported` |
| An overlay plane's mask is published next to its image (`_ROI1`, `_ROI2` and so on) | `test_conversion.py::test_overlay_mask_published_next_to_its_volume`, `test_organizer_publishes_every_overlay_mask_with_its_volume`, `test_real_converter.py::test_real_converter_overlay_published_wit| A series with uneven slice spacing converts; its `_Eq_1` copy is published next to its image | `test_real_converter.py::test_real_converter_adds_equalized_copy_without_sidecar`, `test_real_converter_uneven_slice_spacing_session_converts` |
h_its_volume`, `test_real_converter_two_overlay_planes` |
| A StudyDate 28+ days from the ZIP-name date holds the ZIP until renamed or accepted; a smaller difference only warns | `test_inputs.py::test_date_a_month_from_zip_name_held_until_accepted`, `test_days_apart_table`, `test_date_hold_starts_at_28_days_in_either_direction`, `test_renaming_a_date_held_zip_clears_the_hold`, `test_date_hold_on_a_zip_placed_in_subjects_sorted`, `test_accept_date_mismatch_does_not_release_a_multi_study_hold`, `test_accepted_date_mismatch_lasts_for_those_zip_bytes_only`, `test_conversion.py::test_sidecar_keeps_dicom_study_date_apart_from_name_date`, `test_inputs.py::test_days_apart_skips_study_dates_that_are_not_8_digits` |
| Sidecars carry `session_date` (ZIP name) and `dicom_study_date` (DICOM headers) separately; several StudyDates are comma-separated; `null` when there is none | `test_conversion.py::test_sidecar_keeps_dicom_study_date_apart_from_name_date`, `test_sidecar_study_date_is_null_without_a_header_date`, `test_several_study_dates_are_listed_in_every_sidecar` |
| Provenance is recorded | `test_conversion.py::test_provenance_recorded_in_receipt_and_state` |
| Commands that share any storage folder exclude each other; dry runs take no lock | `test_locks.py::test_shared_report_root_excludes_different_projects`, `test_lock_failure_names_holder`, `test_dry_run_takes_no_lock`, `test_killed_holder_releases_its_locks`; across machines: the runbook's two-node check |
| No session rolls back after a crash; the index is recovered from its backup | `test_recovery.py::test_partial_journal_deletion_does_not_roll_back`, `test_corrupt_index_recovers_from_backup`, `test_missing_index_uses_backup_silently`, `test_both_index_files_corrupt_stops_run` |
| Crash recovery rolls back, redoes, or adopts after the file check | `test_recovery.py::test_crash_at_boundary_reconciles`, `test_crash_during_reconciliation_is_recoverable`, `test_adoption_refused_when_volume_deleted`, `test_unexpected_receipt_after_a_crash_waits_for_a_person`, `test_dry_run_reports_reconciliation_without_acting` |
| SIGTERM and Ctrl+C stop in order (143, 130); nothing is published after a stop | `test_recovery.py::test_stop_signal_during_conversion`, `test_stop_while_organizing_publishes_nothing`, `test_sigterm_stops_converter_group_and_exits_143` (Linux) |
| `--staging-root` can put working files on another filesystem | `test_recovery.py::test_cross_device_publish_completes` |
| Classifier revision 2 labels | `test_conversion.py::test_classifier_revision_2_table`, `test_prepare_and_convert_labels_agree` |
| Privacy channels: header text passes through; sidecars and logs are filtered; raw converter output never reaches disk; ZIP member names stay out of reports | `test_inputs.py::test_zip_member_names_stay_out_of_reports`, `test_member_with_bad_crc_fails_the_zip`, `test_conversion.py::test_header_text_passes_through_and_sidecar_stays_filtered`, `test_raw_converter_output_never_written`, `test_real_converter.py::test_real_converter_output_byte_identical` (no extensions), `test_real_converter_identifying_fields_stay_out_of_outputs` |
| `session_status.csv` and `run_history.csv` columns; every locked run writes a history row | `test_conversion.py::test_status_columns_and_last_attempt_reported`, `test_recovery.py::test_history_row_written_on_early_abort`, `test_recovered_index_noted_in_history`, `test_history_counts_reconciled_intents` |
| A report file held open does not stop a run | `test_conversion.py::test_report_file_held_open_does_not_stop_run` |
| `--dry-run` writes nothing; it hashes only with `--audit` | `test_pipeline.py::test_dry_run_writes_nothing_and_bad_names_held`, `test_inputs.py::test_dry_run_audit_hashes_but_writes_nothing`, `test_recovery.py::test_dry_run_with_corrupt_index_writes_nothing` |
| Other state versions are refused without writing | `test_project_configuration.py::test_old_state_rejected_without_writes` |
| Session directories without the ownership receipt are never overwritten | `test_inputs.py::test_extraction_folder_without_a_receipt_is_never_replaced`, `test_conversion.py::test_nifti_folder_without_a_receipt_is_never_replaced` |
| Real dcm2niix behavior (pinned release) | CI `real-converter` job running `test_real_converter.py` |
| License | `LICENSE`, `NOTICE`, `pyproject.toml` (`license = "Apache-2.0"`) |

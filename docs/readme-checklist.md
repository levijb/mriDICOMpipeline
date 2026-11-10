# README checklist (Goal 7)

Each guarantee the README makes, and the test or deployment check that
establishes it. Review this list whenever the README changes.

| README guarantee | Evidence |
|---|---|
| Python 3.11+, Linux and Windows | CI `test` job (Python 3.11-3.14, ubuntu-latest and windows-latest) |
| Case-only duplicate roster IDs are ignored with a warning | `test_identity.py::test_case_only_id_collision_ignored_with_warning` |
| Windows device names are rejected as IDs | `test_identity.py::test_reserved_device_names_rejected` |
| `_N` suffix rules | `test_identity.py::test_session_key_grammar` |
| A partly copied ZIP fails and is retried when complete | `test_inputs.py::test_truncated_upload_fails_then_prepares_when_complete` |
| ZIPs are moved (or prepared in place in SubjectsSorted), never modified or deleted; unaccepted ZIPs stay | `test_inputs.py::test_sorted_zips_are_prepared_in_place_and_never_deleted`, `test_pipeline.py::test_duplicates_and_conflicts_are_cached` |
| An accepted canonical ZIP is immutable; outputs stay unchanged | `test_inputs.py::test_changed_canonical_zip_is_held_and_outputs_unchanged`, `test_canonical_conflict_blocks_conversion` |
| A restored canonical ZIP is accepted without extracting again | `test_inputs.py::test_restored_canonical_zip_accepted_without_reextract` |
| `--force-reextract` never accepts changed bytes | `test_inputs.py::test_force_reextract_refuses_changed_bytes` |
| A missing canonical ZIP gets a nonblocking issue | `test_inputs.py::test_missing_canonical_zip_nonblocking_issue` |
| Byte-identical repeats are stored once; conflicting repeats and multi-study ZIPs are held | `test_inputs.py::test_repeated_identical_instances_written_once`, `test_conflicting_repeated_instance_held`, `test_multi_study_held_until_accepted` |
| Free-space check before extraction | `test_inputs.py::test_free_space_check_refuses_extraction_and_keeps_zip` |
| `prepare --audit` re-hashes canonical ZIPs | `test_inputs.py::test_audit_rehashes_canonical_zip` |
| A moved project is refused | `test_inputs.py::test_moved_project_refused` |
| `--session` limits both commands | `test_inputs.py::test_session_filter_limits_prepare`, `test_conversion.py::test_session_filter_limits_work` |
| Cached reruns read no scan contents | `test_pipeline.py::test_1000_cached_sessions_do_not_read_scan_contents` |
| `converted` requires every required series; non-image series are excused | `test_conversion.py::test_missing_required_series_is_partial`, `test_inputs.py::test_non_image_series_extracted_and_excused`, `test_real_converter.py::test_real_converter_with_non_image_object_converts`, `test_real_converter_unconvertible_series_makes_session_partial` |
| Published NIfTI decompresses to the converter's bytes | `test_conversion.py::test_published_nifti_is_byte_identical_to_converter_output`, `test_real_converter.py::test_real_converter_output_byte_identical` |
| A complete session is never reconverted automatically | `test_conversion.py::test_complete_session_not_reconverted_when_folder_modified`, `test_pipeline.py::test_deleted_dicom_reextracted_and_deleted_nifti_flagged` |
| `convert --audit --session` revalidates a restored folder | `test_conversion.py::test_restored_folder_revalidated_by_audit_session` |
| An explicit reconversion refuses folders with added files | `test_conversion.py::test_force_reconvert_refuses_folder_with_extra_files` |
| A failed reconversion keeps the complete folder and records the attempt | `test_conversion.py::test_failed_reconversion_keeps_last_good`, `test_status_columns_and_last_attempt_reported` |
| An overlay plane's mask is published next to its image | `test_conversion.py::test_overlay_mask_published_next_to_its_volume`, `test_real_converter.py::test_real_converter_overlay_published_with_its_volume` |
| Provenance is recorded | `test_conversion.py::test_provenance_recorded_in_receipt_and_state` |
| Commands that share any storage folder exclude each other; dry runs take no lock | `test_locks.py::test_shared_report_root_excludes_different_projects`, `test_lock_failure_names_holder`, `test_dry_run_takes_no_lock`; across machines: the runbook's two-node check |
| No session rolls back after a crash; the index is recovered from its backup | `test_recovery.py::test_partial_journal_deletion_does_not_roll_back`, `test_corrupt_index_recovers_from_backup`, `test_missing_index_uses_backup_silently`, `test_both_index_files_corrupt_stops_run` |
| Crash recovery rolls back, redoes, or adopts after the file check | `test_recovery.py::test_crash_at_boundary_reconciles`, `test_crash_during_reconciliation_is_recoverable`, `test_adoption_refused_when_volume_deleted`, `test_dry_run_reports_reconciliation_without_acting` |
| SIGTERM and Ctrl+C stop in order (143, 130); nothing is published after a stop | `test_recovery.py::test_stop_signal_during_conversion`, `test_sigterm_stops_converter_group_and_exits_143` (Linux) |
| Classifier revision 2 labels | `test_conversion.py::test_classifier_revision_2_table`, `test_prepare_and_convert_labels_agree` |
| Privacy channels: header text passes through; sidecars and logs are filtered; raw converter output never reaches disk | `test_conversion.py::test_header_text_passes_through_and_sidecar_stays_filtered`, `test_raw_converter_output_never_written`, `test_real_converter.py::test_real_converter_output_byte_identical` (no extensions) |
| `session_status.csv` and `run_history.csv` columns; every locked run writes a history row | `test_conversion.py::test_status_columns_and_last_attempt_reported`, `test_recovery.py::test_history_row_written_on_early_abort`, `test_recovered_index_noted_in_history`, `test_history_counts_reconciled_intents` |
| A report file held open does not stop a run | `test_conversion.py::test_report_file_held_open_does_not_stop_run` |
| `--dry-run` writes nothing; it hashes only with `--audit` | `test_pipeline.py::test_dry_run_writes_nothing_and_bad_names_held`, `test_inputs.py::test_dry_run_audit_hashes_but_writes_nothing`, `test_recovery.py::test_dry_run_with_corrupt_index_writes_nothing` |
| Other state versions are refused without writing | `test_project_configuration.py::test_old_state_rejected_without_writes` |
| Real dcm2niix behavior (pinned release) | CI `real-converter` job running `test_real_converter.py` |
| License | `LICENSE`, `NOTICE`, `pyproject.toml` (`license = "Apache-2.0"`) |

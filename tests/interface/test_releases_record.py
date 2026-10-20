"""The releases.json record and the publication checks Microcosm shares."""

import json

import pytest

from microcosm_emitter_interface.releases import (
    ReleaseRecordError,
    ReleasesRecord,
    add_build,
    build_folder,
    check_change,
    check_commit,
    check_record,
    check_repository,
    make_current,
    withdraw_build,
)

NATIONAL = "microcosm-uk-2024-25-national"
DENSE = "microcosm-uk-2024-25-dense"
OLDER_BUILD = "uk-frs-calibration-attempt-20261014T214443Z-d12ff9b3"
NEWER_BUILD = "uk-frs-calibration-attempt-20261019T083323Z-6cba5d71"

DOCUMENTED_EXAMPLE = {
    "schema_version": 1,
    "default": NATIONAL,
    "releases": {
        NATIONAL: {
            "role": "national_default",
            "current": NEWER_BUILD,
            "builds": {
                OLDER_BUILD: {
                    "published_at": "2026-10-14T21:44:43+00:00",
                    "withdrawn": False,
                },
                NEWER_BUILD: {
                    "published_at": "2026-10-19T08:33:23+00:00",
                    "withdrawn": False,
                },
            },
        }
    },
}


def documented_record() -> ReleasesRecord:
    return ReleasesRecord.parse(json.dumps(DOCUMENTED_EXAMPLE).encode())


def codes(violations) -> set[str]:
    return {violation.code for violation in violations}


def manifest(release: str, build: str) -> bytes:
    return json.dumps({"release": release, "build": build}).encode()


def repository_files(record: ReleasesRecord) -> dict[str, bytes]:
    files = {}
    for release, entry in record.releases.items():
        for build in entry.builds:
            folder = build_folder(release, build)
            files[f"{folder}/release_manifest.json"] = manifest(release, build)
            files[f"{folder}/microcosm_uk_2024_25.h5"] = b"rows"
    return files


def test_documented_record_round_trips_in_its_documented_shape():
    record = documented_record()
    document = json.loads(record.serialize())
    assert document == DOCUMENTED_EXAMPLE
    assert list(document) == ["schema_version", "default", "releases"]
    assert list(document["releases"][NATIONAL]) == ["role", "current", "builds"]
    assert record.serialize().endswith(b"}\n")
    assert check_record(record) == []


def test_an_empty_record_has_no_default_and_no_releases():
    assert json.loads(ReleasesRecord.empty().serialize()) == {
        "schema_version": 1,
        "default": None,
        "releases": {},
    }


@pytest.mark.parametrize(
    "edit",
    [
        lambda document: document.update(schema_version=2),
        lambda document: document.update(default="unknown-release"),
        lambda document: document.update(extra=True),
        lambda document: document["releases"][NATIONAL].update(current="unlisted"),
        lambda document: document["releases"][NATIONAL]["builds"][NEWER_BUILD].update(
            withdrawn=True
        ),
        lambda document: document["releases"][NATIONAL]["builds"][OLDER_BUILD].update(
            withdrawn="no"
        ),
        lambda document: document["releases"][NATIONAL]["builds"][OLDER_BUILD].update(
            published_at=5
        ),
        lambda document: document["releases"][NATIONAL]["builds"][OLDER_BUILD].update(
            note="extra"
        ),
        lambda document: document["releases"].update({"../escape": {}}),
        lambda document: document["releases"][NATIONAL].pop("role"),
    ],
    ids=[
        "schema-version",
        "default-not-listed",
        "extra-key",
        "current-not-listed",
        "current-withdrawn",
        "withdrawn-not-boolean",
        "published-at-not-text",
        "extra-build-key",
        "release-identifier",
        "missing-role",
    ],
)
def test_records_that_break_the_shape_are_refused(edit):
    document = json.loads(json.dumps(DOCUMENTED_EXAMPLE))
    edit(document)
    with pytest.raises(ReleaseRecordError):
        ReleasesRecord.parse(json.dumps(document).encode())


def test_publishing_adds_a_build_and_can_make_it_current():
    later = "uk-frs-calibration-attempt-20261020T000000Z-0000aaaa"
    record = add_build(
        documented_record(),
        release=NATIONAL,
        build=later,
        release_role="national_default",
        published_at="2026-10-20T00:00:00+00:00",
        make_current=True,
    )
    assert record.releases[NATIONAL].current == later
    assert record.releases[NATIONAL].builds[later].withdrawn is False
    assert check_change(documented_record(), record) == []


def test_a_build_can_be_published_without_becoming_current():
    record = add_build(
        documented_record(),
        release=DENSE,
        build=NEWER_BUILD,
        release_role="local_area_dense",
        published_at="2026-10-20T00:00:00+00:00",
        make_current=False,
    )
    assert record.releases[DENSE].current is None
    assert record.releases[DENSE].role == "local_area_dense"
    assert record.releases[NATIONAL] == documented_record().releases[NATIONAL]
    assert check_record(record) == []


def test_publishing_a_listed_build_again_is_refused():
    with pytest.raises(ReleaseRecordError) as refusal:
        add_build(
            documented_record(),
            release=NATIONAL,
            build=OLDER_BUILD,
            release_role="national_default",
            published_at="2026-10-20T00:00:00+00:00",
            make_current=False,
        )
    assert codes(refusal.value.violations) == {"BUILD_ALREADY_LISTED"}


def test_a_release_keeps_the_role_it_was_first_published_with():
    with pytest.raises(ReleaseRecordError) as refusal:
        add_build(
            documented_record(),
            release=NATIONAL,
            build="another-build",
            release_role="local_area",
            published_at="2026-10-20T00:00:00+00:00",
            make_current=False,
        )
    assert codes(refusal.value.violations) == {"RELEASE_ROLE_DIFFERS"}


def test_promoting_names_a_listed_build_that_is_not_withdrawn():
    record = make_current(documented_record(), release=NATIONAL, build=OLDER_BUILD)
    assert record.releases[NATIONAL].current == OLDER_BUILD
    with pytest.raises(ReleaseRecordError) as unlisted:
        make_current(documented_record(), release=NATIONAL, build="unlisted")
    assert codes(unlisted.value.violations) == {"BUILD_NOT_LISTED"}
    withdrawn = withdraw_build(documented_record(), release=NATIONAL, build=OLDER_BUILD)
    with pytest.raises(ReleaseRecordError) as refusal:
        make_current(withdrawn, release=NATIONAL, build=OLDER_BUILD)
    assert codes(refusal.value.violations) == {"CURRENT_WITHDRAWN"}


def test_withdrawing_sets_only_withdrawn_and_refuses_the_current_build():
    before = documented_record()
    after = withdraw_build(before, release=NATIONAL, build=OLDER_BUILD)
    assert after.releases[NATIONAL].builds[OLDER_BUILD].withdrawn is True
    assert (
        after.releases[NATIONAL].builds[OLDER_BUILD].published_at
        == before.releases[NATIONAL].builds[OLDER_BUILD].published_at
    )
    assert check_change(before, after) == []
    with pytest.raises(ReleaseRecordError) as refusal:
        withdraw_build(before, release=NATIONAL, build=NEWER_BUILD)
    assert codes(refusal.value.violations) == {"WITHDRAWING_CURRENT_BUILD"}


def test_existing_build_entries_may_change_only_their_withdrawn_flag():
    before = documented_record()
    changed = json.loads(json.dumps(DOCUMENTED_EXAMPLE))
    changed["releases"][NATIONAL]["builds"][OLDER_BUILD]["published_at"] = "later"
    assert codes(
        check_change(before, ReleasesRecord.parse(json.dumps(changed).encode()))
    ) == {"BUILD_ENTRY_CHANGED"}
    removed = json.loads(json.dumps(DOCUMENTED_EXAMPLE))
    del removed["releases"][NATIONAL]["builds"][OLDER_BUILD]
    assert codes(
        check_change(before, ReleasesRecord.parse(json.dumps(removed).encode()))
    ) == {"BUILD_ENTRY_REMOVED"}
    gone = {"schema_version": 1, "default": None, "releases": {}}
    assert codes(
        check_change(before, ReleasesRecord.parse(json.dumps(gone).encode()))
    ) == {"RELEASE_REMOVED"}
    assert check_change(None, before) == []


def test_every_listed_build_needs_its_folder_and_a_manifest_naming_it():
    record = documented_record()
    files = repository_files(record)
    assert check_repository(record, files, files.__getitem__) == []
    newer_folder = build_folder(NATIONAL, NEWER_BUILD)
    without_folder = {
        path: content
        for path, content in files.items()
        if not path.startswith(newer_folder + "/")
    }
    assert codes(
        check_repository(record, without_folder, without_folder.__getitem__)
    ) == {"BUILD_FOLDER_MISSING"}
    without_manifest = dict(files)
    del without_manifest[f"{newer_folder}/release_manifest.json"]
    assert codes(
        check_repository(record, without_manifest, without_manifest.__getitem__)
    ) == {"MANIFEST_MISSING"}
    for wrong in (manifest(NATIONAL, OLDER_BUILD), manifest(DENSE, NEWER_BUILD), b"["):
        mismatched = {**files, f"{newer_folder}/release_manifest.json": wrong}
        assert codes(check_repository(record, mismatched, mismatched.__getitem__)) == {
            "MANIFEST_MISMATCH"
        }


def test_no_file_inside_an_existing_build_folder_may_be_added_or_removed():
    record = documented_record()
    files = repository_files(record)
    new_folder = build_folder(NATIONAL, "uk-frs-calibration-attempt-new")
    assert (
        check_commit(
            before_files=files,
            added_paths=[f"{new_folder}/release_manifest.json", "releases.json"],
            deleted_paths=[],
            before=record,
            after=record,
        )
        == []
    )
    listed_folder = build_folder(NATIONAL, OLDER_BUILD)
    assert codes(
        check_commit(
            before_files=files,
            added_paths=[f"{listed_folder}/extra.json"],
            deleted_paths=[],
            before=record,
            after=record,
        )
    ) == {"EXISTING_BUILD_FOLDER_CHANGED"}
    assert codes(
        check_commit(
            before_files=files,
            added_paths=[],
            deleted_paths=[f"{listed_folder}/microcosm_uk_2024_25.h5"],
            before=record,
            after=record,
        )
    ) == {"EXISTING_BUILD_FOLDER_CHANGED"}
    unlisted_folder = build_folder(NATIONAL, "copied-but-unlisted")
    with_unlisted = {**files, f"{unlisted_folder}/release_manifest.json": b"{}"}
    assert codes(
        check_commit(
            before_files=with_unlisted,
            added_paths=[f"{unlisted_folder}/microcosm_uk_2024_25.h5"],
            deleted_paths=[],
            before=record,
            after=record,
        )
    ) == {"EXISTING_BUILD_FOLDER_CHANGED"}


@pytest.mark.parametrize(
    "pointer_path",
    ["latest.json", "latest-national.json", "latest-evidence.json", "x/latest_y.json"],
)
def test_no_write_may_create_a_pointer_file(pointer_path):
    record = documented_record()
    assert codes(
        check_commit(
            before_files=repository_files(record),
            added_paths=[pointer_path],
            deleted_paths=[],
            before=record,
            after=record,
        )
    ) == {"POINTER_FILE_WRITE"}


def test_current_cannot_change_while_pointer_files_exist():
    before = documented_record()
    files = {**repository_files(before), "latest-national.json": b"{}"}
    promoted = make_current(before, release=NATIONAL, build=OLDER_BUILD)
    assert codes(
        check_commit(
            before_files=files,
            added_paths=["releases.json"],
            deleted_paths=[],
            before=before,
            after=promoted,
        )
    ) == {"POINTER_FILES_PRESENT"}
    published = add_build(
        before,
        release=NATIONAL,
        build="uk-frs-calibration-attempt-new",
        release_role="national_default",
        published_at="2026-10-20T00:00:00+00:00",
        make_current=False,
    )
    assert (
        check_commit(
            before_files=files,
            added_paths=[
                f"{build_folder(NATIONAL, 'uk-frs-calibration-attempt-new')}/a.json",
                "releases.json",
            ],
            deleted_paths=[],
            before=before,
            after=published,
        )
        == []
    )
    first_record_with_current = add_build(
        ReleasesRecord.empty(),
        release=NATIONAL,
        build=NEWER_BUILD,
        release_role="national_default",
        published_at="2026-10-20T00:00:00+00:00",
        make_current=True,
    )
    assert codes(
        check_commit(
            before_files={"latest.json": b"{}"},
            added_paths=["releases.json"],
            deleted_paths=[],
            before=None,
            after=first_record_with_current,
        )
    ) == {"POINTER_FILES_PRESENT"}


DEEPLY_NESTED = b"[" * 100_000 + b"]" * 100_000


def test_a_deeply_nested_record_is_refused_as_invalid():
    """``json.loads`` raises ``RecursionError`` for it, not ``ValueError``."""
    with pytest.raises(ReleaseRecordError) as refusal:
        ReleasesRecord.parse(DEEPLY_NESTED)
    assert codes(refusal.value.violations) == {"INVALID_RECORD"}


def test_a_record_above_the_size_limit_is_refused_as_invalid():
    from microcosm_emitter_interface.releases import MAX_RECORD_BYTES

    padded = json.dumps(
        {**DOCUMENTED_EXAMPLE, "padding": "x" * MAX_RECORD_BYTES}
    ).encode()
    with pytest.raises(ReleaseRecordError) as refusal:
        ReleasesRecord.parse(padded)
    assert codes(refusal.value.violations) == {"INVALID_RECORD"}
    assert "bytes" in refusal.value.violations[0].message


def test_a_deeply_nested_manifest_does_not_name_its_build():
    record = documented_record()
    files = repository_files(record)
    newer_folder = build_folder(NATIONAL, NEWER_BUILD)
    nested = {**files, f"{newer_folder}/release_manifest.json": DEEPLY_NESTED}
    assert codes(check_repository(record, nested, nested.__getitem__)) == {
        "MANIFEST_MISMATCH"
    }

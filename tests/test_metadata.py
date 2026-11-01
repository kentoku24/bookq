"""Unit tests for app/metadata/ package — ComicMetadata and MetadataIndex.

Run:
    uv run pytest tests/test_metadata.py -v
"""

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so "app" package is importable
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from app.metadata import ComicMetadata, MetadataIndex, _split_aliases
from app.metadata.search import _calculate_relevance


# ── _split_aliases ──────────────────────────────────────────────────────────────


def test_split_aliases():
    """Splits ' | '-separated alias strings."""
    result = _split_aliases(["artist1 | alias1", "solo"])
    assert "artist1" in result
    assert "alias1" in result
    assert "solo" in result
    assert len(result) == 3


def test_split_aliases_no_pipe():
    """Entries without ' | ' are returned as-is."""
    result = _split_aliases(["solo", "duo"])
    assert result == ["solo", "duo"]


def test_split_aliases_empty_input():
    """Empty list returns empty list."""
    assert _split_aliases([]) == []


def test_split_aliases_whitespace_handling():
    """Leading/trailing whitespace around parts is stripped."""
    result = _split_aliases(["  foo | bar  ", "  baz  "])
    assert result == ["foo", "bar", "baz"]


# ── ComicMetadata.from_folder ────────────────────────────────────────────────────


def test_comic_metadata_generic(tmp_path):
    """Parse generic-format metadata.toml (artist aliases + multilingual tags)."""
    folder = tmp_path / "test_folder"
    folder.mkdir()
    (folder / "metadata.toml").write_text(
        '[source]\n'
        'name = "generic"\n'
        '\n'
        '[comic]\n'
        'id = "123"\n'
        'title_jp = "Test JP"\n'
        'title_en = "Test EN"\n'
        'total_pages = 25\n'
        '\n'
        '[tags]\n'
        'artist = ["artist1 | alias1"]\n'
        'group = []\n'
        'female = ["flower,花"]\n'
        'male = ["sole male"]\n'
        'mixed = ["group"]\n'
        'other = ["tankoubon"]\n'
        'tag = []\n'
    )

    meta = ComicMetadata.from_folder(folder, tmp_path)

    assert meta.has_metadata
    assert meta.title_jp == "Test JP"
    assert meta.title_en == "Test EN"
    assert meta.source == "generic"
    assert meta.total_pages == 25

    # Split aliases
    assert "artist1" in meta.artist
    assert "alias1" in meta.artist

    # Flattened tags include English and Japanese
    assert "flower" in meta.tags
    assert "花" in meta.tags
    assert "sole male" in meta.tags
    assert "tankoubon" in meta.tags


def test_comic_metadata_gallery(tmp_path):
    """Parse gallery-format metadata.toml (generic source, neutral tags)."""
    folder = tmp_path / "test_example"
    folder.mkdir()
    (folder / "metadata.toml").write_text(
        '[source]\n'
        'name = "generic"\n'
        '\n'
        '[comic]\n'
        'id = "12345"\n'
        'title_jp = "Test JP"\n'
        'title_en = "Test EN"\n'
        'total_pages = 211\n'
        '\n'
        '[tags]\n'
        'artist = ["example-artist"]\n'
        'tag = ["teacher", "school-comedy", "confession"]\n'
    )

    meta = ComicMetadata.from_folder(folder, tmp_path)

    assert meta.has_metadata
    assert meta.source == "generic"
    assert meta.total_pages == 211

    # Regular tags
    assert "teacher" in meta.tags
    assert "school-comedy" in meta.tags
    assert "confession" in meta.tags


def test_missing_metadata_file(tmp_path):
    """Folder without metadata.toml returns has_metadata=False."""
    folder = tmp_path / "no_meta"
    folder.mkdir()
    meta = ComicMetadata.from_folder(folder, tmp_path)

    assert not meta.has_metadata
    assert meta.folder_name == "no_meta"
    assert meta.folder_path == "no_meta"  # relative to downloads_base
    assert meta.title_jp == ""


def test_broken_toml_falls_back(tmp_path):
    """Invalid TOML returns has_metadata=False."""
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "metadata.toml").write_text("this is not valid toml [[[")
    meta = ComicMetadata.from_folder(folder, tmp_path)

    assert not meta.has_metadata
    assert meta.folder_name == "broken"


def test_empty_metadata_toml(tmp_path):
    """Empty metadata.toml is still valid TOML but has no content."""
    folder = tmp_path / "empty_toml"
    folder.mkdir()
    (folder / "metadata.toml").write_text("")
    meta = ComicMetadata.from_folder(folder, tmp_path)

    assert meta.has_metadata  # TOML parsed OK
    assert meta.title_jp == ""
    assert meta.artist == []
    assert meta.tags == []
    assert meta.source == ""


def test_metadata_empty_folder(tmp_path):
    """An empty folder (no metadata.toml) returns has_metadata=False."""
    folder = tmp_path / "empty"
    folder.mkdir()
    meta = ComicMetadata.from_folder(folder, tmp_path)
    assert not meta.has_metadata
    assert meta.total_pages == 0


# ── _flatten_tags ────────────────────────────────────────────────────────────────


def test_flatten_tags_includes_japanese_and_english():
    """_flatten_tags splits comma-separated translations."""
    tags_data = {
        "female": ["flower,花", "school,学校"],
        "male": ["sole male"],
        "tag": ["tankoubon"],
    }
    result = ComicMetadata._flatten_tags(tags_data)
    assert "flower" in result
    assert "花" in result
    assert "school" in result
    assert "学校" in result
    assert "sole male" in result
    assert "tankoubon" in result
    # No duplicates
    assert len(result) == len(set(result))


def test_flatten_tags_deduplicates():
    """Duplicate tags across categories are included only once."""
    tags_data = {
        "female": ["confession,告白"],
        "male": ["confession"],
        "tag": ["confession"],
    }
    result = ComicMetadata._flatten_tags(tags_data)
    assert result.count("confession") == 1


def test_flatten_tags_all_categories():
    """All tag categories (male, female, mixed, other, tag, misc) are processed."""
    tags_data = {
        "male": ["a"],
        "female": ["b"],
        "mixed": ["c"],
        "other": ["d"],
        "tag": ["e"],
        "misc": ["f"],
    }
    result = ComicMetadata._flatten_tags(tags_data)
    assert set(result) == {"a", "b", "c", "d", "e", "f"}


def test_flatten_tags_ignores_non_list_entries():
    """Non-list values in tag categories are silently skipped."""
    tags_data = {
        "female": ["good"],
        "male": "not a list",  # not a list
    }
    result = ComicMetadata._flatten_tags(tags_data)
    assert result == ["good"]


def test_flatten_tags_empty_input():
    """Empty dict returns empty list."""
    assert ComicMetadata._flatten_tags({}) == []


# ── MetadataIndex._calculate_relevance ───────────────────────────────────────────


def test_relevance_scoring():
    """_calculate_relevance scores by matched fields."""
    index = MetadataIndex()
    comic = ComicMetadata(
        folder_path="test",
        folder_name="test",
        title_jp="Test Title",
        artist=["test_artist"],
        tags=["tag1", "tag2"],
    )

    # Title match = 10
    score = _calculate_relevance("Title", comic)
    assert score >= 10

    # Artist match = 8
    score_artist = _calculate_relevance("test_artist", comic)
    assert score_artist >= 8

    # No match = 0
    score_none = _calculate_relevance("xyznonexistent", comic)
    assert score_none == 0


def test_relevance_multi_field_bonus():
    """Matching multiple fields adds a bonus (+2 per field beyond first)."""
    index = MetadataIndex()
    comic = ComicMetadata(
        folder_path="t",
        folder_name="t",
        title_jp="UniqueTitle",
        artist=["UniqueArtist"],
        tags=["UniqueTag"],
    )

    # Title (10) + artist (8) + tag (3) + bonus (2 * 2 = 4) = 25
    score = _calculate_relevance("UniqueTitle UniqueArtist UniqueTag", comic)
    assert score == 25  # 10 + 8 + 3 + 4


def test_multi_word_query_scoring():
    """Multiple words each contribute to score."""
    index = MetadataIndex()
    comic = ComicMetadata(
        folder_path="t",
        folder_name="t",
        title_jp="Full Title Here",
        artist=["artist_name"],
        tags=["tag1", "something_else"],
    )

    score = _calculate_relevance("Title artist_name", comic)
    # Title match (10) + artist match (8) = 18  (no multi-field bonus since both matched same word)
    # Actually wait: "Title" matches title → 10, "artist_name" matches artist → 8
    # Two different words matched two different fields → multi-field bonus: (2-1)*2 = 2
    # Total: 10 + 8 + 2 = 20
    assert score == 20


def test_relevance_case_insensitive():
    """Relevance scoring is case-insensitive."""
    index = MetadataIndex()
    comic = ComicMetadata(
        folder_path="t",
        folder_name="t",
        title_jp="UPPERCASE TITLE",
    )
    score = _calculate_relevance("uppercase title", comic)
    assert score >= 10


def test_relevance_empty_query_returns_zero():
    """Empty or whitespace-only query returns 0."""
    index = MetadataIndex()
    comic = ComicMetadata(folder_path="t", folder_name="t", title_jp="Title")
    assert _calculate_relevance("", comic) == 0
    assert _calculate_relevance("   ", comic) == 0


# ── MetadataIndex.search ─────────────────────────────────────────────────────────


def test_search_with_filters():
    """Filters correctly narrow search results."""
    index = MetadataIndex()

    comic1 = ComicMetadata(
        folder_path="f1", folder_name="f1",
        artist=["artist_a"], tags=["tag_x", "tag_y"],
    )
    comic2 = ComicMetadata(
        folder_path="f2", folder_name="f2",
        artist=["artist_b"], tags=["tag_z"],
    )
    comic3 = ComicMetadata(
        folder_path="f3", folder_name="f3",
        artist=["artist_a"], tags=["tag_x"],
    )

    index.comics = [comic1, comic2, comic3]
    index._build_facets()

    # Filter by artist
    results = index.search(filters={"artist": ["artist_a"]})
    assert len(results) == 2
    paths = [r["folder_path"] for r in results]
    assert "f1" in paths
    assert "f3" in paths

    # Filter by artist AND tag (OR within key, AND across keys)
    results = index.search(filters={"artist": ["artist_a"], "tags": ["tag_y"]})
    assert len(results) == 1
    assert results[0]["folder_path"] == "f1"

    # Filter with no match
    results = index.search(filters={"artist": ["nonexistent"]})
    assert len(results) == 0


def test_search_filter_or_within_key():
    """Multiple filter values for the same key use OR semantics."""
    index = MetadataIndex()
    index.comics = [
        ComicMetadata(folder_path="f1", folder_name="f1", artist=["art_a"]),
        ComicMetadata(folder_path="f2", folder_name="f2", artist=["art_b"]),
        ComicMetadata(folder_path="f3", folder_name="f3", artist=["art_c"]),
    ]
    index._build_facets()

    results = index.search(filters={"artist": ["art_a", "art_b"]})
    assert len(results) == 2


def test_search_filter_case_insensitive():
    """Filter matching is case-insensitive."""
    index = MetadataIndex()
    index.comics = [
        ComicMetadata(folder_path="f1", folder_name="f1", artist=["ARTIST NAME"]),
    ]
    index._build_facets()

    results = index.search(filters={"artist": ["artist name"]})
    assert len(results) == 1


def test_search_filter_partial_match():
    """Filter values match as substrings."""
    index = MetadataIndex()
    index.comics = [
        ComicMetadata(folder_path="f1", folder_name="f1", artist=["sample artist"]),
    ]
    index._build_facets()

    results = index.search(filters={"artist": ["sample"]})
    assert len(results) == 1


def test_search_sorting_relevance_title_first():
    """Relevance sort returns the title match before the weaker tag match."""
    index = MetadataIndex()
    comic_a = ComicMetadata(
        folder_path="a", folder_name="a",
        title_jp="Alpha",
        tags=["beta"],
    )
    comic_b = ComicMetadata(
        folder_path="b", folder_name="b",
        title_jp="Beta",
        tags=["alpha"],
    )
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="alpha", sort="relevance")
    assert results[0]["folder_path"] == "a"
    assert results[1]["folder_path"] == "b"


def test_search_sorting_relevance_artist_weight():
    """Relevance sort ranks an artist match above a tag-only match."""
    index = MetadataIndex()
    comic_a = ComicMetadata(
        folder_path="a", folder_name="a",
        artist=["matchmaker"],
        tags=["other"],
    )
    comic_b = ComicMetadata(
        folder_path="b", folder_name="b",
        artist=["other"],
        tags=["matchmaker"],
    )
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="matchmaker", sort="relevance")
    assert results[0]["folder_path"] == "a"
    assert results[1]["folder_path"] == "b"


def test_search_sorting_unknown_sort_falls_back_to_relevance():
    """An unknown sort value falls back to relevance order."""
    index = MetadataIndex()
    comic_a = ComicMetadata(folder_path="a", folder_name="a", title_jp="Match Title")
    comic_b = ComicMetadata(folder_path="b", folder_name="b", title_jp="Another")
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="Match", sort="newest")
    assert len(results) == 1
    assert results[0]["folder_path"] == "a"


def test_search_sorting_relevance_default():
    """Default sort is relevance."""
    index = MetadataIndex()
    comic_a = ComicMetadata(
        folder_path="a", folder_name="a",
        title_jp="Match Title",
        artist=["match_artist"],
    )
    comic_b = ComicMetadata(
        folder_path="b", folder_name="b",
        title_jp="Another",
    )
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="Match", sort="relevance")
    assert len(results) == 1
    assert results[0]["folder_path"] == "a"


def test_search_relevance_tiebreak_episode_order():
    """Equal relevance scores tie-break by natural folder-name order (ch-2 < ch-10)."""
    index = MetadataIndex()
    comic_a = ComicMetadata(folder_path="p/ch-10", folder_name="ch-10")
    comic_b = ComicMetadata(folder_path="p/ch-2", folder_name="ch-2")
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="", sort="relevance")
    assert [r["folder_name"] for r in results] == ["ch-2", "ch-10"]


def test_search_empty_query_natural_order():
    """Empty query returns all comics in natural folder-name order (ch-2 < ch-10)."""
    index = MetadataIndex()
    comic_a = ComicMetadata(folder_path="p/ch-10", folder_name="ch-10")
    comic_b = ComicMetadata(folder_path="p/ch-2", folder_name="ch-2")
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="", sort="relevance")
    assert [r["folder_name"] for r in results] == ["ch-2", "ch-10"]


def test_search_relevance_multiword_tiebreak_episode_order():
    """Equal multi-word relevance scores tie-break by natural folder-name order."""
    index = MetadataIndex()
    comic_a = ComicMetadata(
        folder_path="p/ch-10", folder_name="ch-10",
        title_jp="Shared Title",
    )
    comic_b = ComicMetadata(
        folder_path="p/ch-2", folder_name="ch-2",
        title_jp="Shared Title",
    )
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="Shared", sort="relevance")
    assert [r["folder_name"] for r in results] == ["ch-2", "ch-10"]


def test_search_legacy_sort_tiebreak_episode_order():
    """A legacy sort value keeps natural folder-name order (ch-2 < ch-10)."""
    index = MetadataIndex()
    comic_a = ComicMetadata(folder_path="p/ch-10", folder_name="ch-10")
    comic_b = ComicMetadata(folder_path="p/ch-2", folder_name="ch-2")
    index.comics = [comic_a, comic_b]
    index._build_facets()

    results = index.search(q="", sort="newest")
    assert [r["folder_name"] for r in results] == ["ch-2", "ch-10"]


def test_search_three_way_natural_order():
    """Three comics with no query sort by natural folder-name order.

    Natural (not lexicographic) order puts ch-2 before ch-5 before ch-10."""
    index = MetadataIndex()
    comic_a = ComicMetadata(folder_path="p/ch-10", folder_name="ch-10")
    comic_b = ComicMetadata(folder_path="p/ch-2", folder_name="ch-2")
    comic_c = ComicMetadata(folder_path="p/ch-5", folder_name="ch-5")
    index.comics = [comic_a, comic_b, comic_c]
    index._build_facets()

    results = index.search(q="", sort="relevance")
    assert [r["folder_name"] for r in results] == ["ch-2", "ch-5", "ch-10"]


def test_search_natural_order_case_insensitive_tiebreak():
    """Natural tie-break is stable regardless of insertion order."""
    index = MetadataIndex()
    comic_a = ComicMetadata(folder_path="p/ch-10", folder_name="ch-10")
    comic_b = ComicMetadata(folder_path="p/ch-2", folder_name="ch-2")
    index.comics = [comic_b, comic_a]
    index._build_facets()

    results = index.search(q="", sort="relevance")
    assert [r["folder_name"] for r in results] == ["ch-2", "ch-10"]


def test_search_before_rebuild_returns_empty():
    """Index with no comics returns empty results."""
    index = MetadataIndex()
    results = index.search(q="test")
    assert results == []


def test_search_no_query_returns_all():
    """Empty query returns all comics (subject to filters)."""
    index = MetadataIndex()
    index.comics = [
        ComicMetadata(folder_path="a", folder_name="a"),
        ComicMetadata(folder_path="b", folder_name="b"),
    ]
    results = index.search(q="")
    assert len(results) == 2


def test_search_no_query_with_filters():
    """Empty query with filters still applies filters."""
    index = MetadataIndex()
    index.comics = [
        ComicMetadata(folder_path="a", folder_name="a", artist=["art_a"]),
        ComicMetadata(folder_path="b", folder_name="b", artist=["art_b"]),
    ]
    results = index.search(q="", filters={"artist": ["art_a"]})
    assert len(results) == 1
    assert results[0]["folder_path"] == "a"


# ── MetadataIndex._build_facets ──────────────────────────────────────────────────


def test_build_facets():
    """_build_facets gathers unique values from all comics."""
    index = MetadataIndex()
    index.comics = [
        ComicMetadata(
            folder_path="a", folder_name="a",
            artist=["art_a"], tags=["t1"],
        ),
        ComicMetadata(
            folder_path="b", folder_name="b",
            artist=["art_b"], tags=["t1", "t2"],
        ),
    ]
    index._build_facets()

    assert "art_a" in index.facets["artists"]
    assert "art_b" in index.facets["artists"]
    assert "t1" in index.facets["tags"]
    assert "t2" in index.facets["tags"]


def test_build_facets_empty_index():
    """Empty index produces empty facets."""
    index = MetadataIndex()
    index._build_facets()
    assert index.facets["artists"] == []
    assert index.facets["tags"] == []


# ── MetadataIndex.rebuild ────────────────────────────────────────────────────────


def test_rebuild_scans_downloads_directory(tmp_path):
    """rebuild scans subdirectories and populates comics and facets."""
    # Create a downloads tree with metadata.toml in one folder
    d1 = tmp_path / "dl" / "comic_a"
    d1.mkdir(parents=True)
    (d1 / "metadata.toml").write_text(
        '[source]\nname = "generic"\n\n[comic]\ntitle_jp = "A"\n\n[tags]\nartist = ["x"]\n'
    )

    d2 = tmp_path / "dl" / "comic_b"
    d2.mkdir(parents=True)
    (d2 / "metadata.toml").write_text(
        '[source]\nname = "generic-archive"\n\n[comic]\ntitle_jp = "B"\n\n[tags]\nartist = ["y"]\n'
    )

    index = MetadataIndex()
    index.rebuild(tmp_path / "dl")

    assert len(index.comics) == 2
    assert index.comics[0].title_jp == "A"
    assert index.comics[1].title_jp == "B"
    assert "x" in index.facets["artists"]
    assert "y" in index.facets["artists"]


def test_rebuild_non_existent_directory(tmp_path):
    """rebuild on a non-existent path logs a warning and does nothing."""
    index = MetadataIndex()
    index.rebuild(tmp_path / "does_not_exist")

    assert index.comics == []
    assert index.facets == {}


def test_rebuild_skips_dot_directories(tmp_path):
    """rebuild skips directories starting with a dot."""
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / ".hidden").mkdir()
    (dl / "visible").mkdir()

    index = MetadataIndex()
    index.rebuild(dl)

    # .hidden should be skipped; visible folder is added (even without metadata)
    assert len(index.comics) == 1
    assert index.comics[0].folder_name == "visible"


def test_rebuild_recursive_scans_nested_metadata(tmp_path):
    """rebuild indexes metadata.toml at every depth, not just depth 1."""
    dl = tmp_path / "dl"

    gallery = dl / "COMEDY" / "atelier-sun" / "gallery"
    gallery.mkdir(parents=True)
    (gallery / "metadata.toml").write_text(
        '[source]\nname = "generic"\n\n[comic]\ntitle_jp = "G"\n\n[tags]\nartist = ["atelier-sun"]\n'
    )

    chapter = dl / "sample_library" / "series" / "ch-1"
    chapter.mkdir(parents=True)
    (chapter / "metadata.toml").write_text(
        '[source]\nname = "example-source"\n\n[comic]\ntitle_jp = "S"\n'  # no [tags]
    )

    index = MetadataIndex()
    index.rebuild(dl)

    paths = [c.folder_path for c in index.comics]
    assert "COMEDY/atelier-sun/gallery" in paths
    assert "sample_library/series/ch-1" in paths

    gallery_comic = next(
        c for c in index.comics if c.folder_path == "COMEDY/atelier-sun/gallery"
    )
    assert gallery_comic.has_metadata is True
    assert gallery_comic.artist == ["atelier-sun"]

    assert index.facets["artists"] == ["atelier-sun"]


def test_rebuild_recursive_skips_dot_dirs_at_depth(tmp_path):
    """rebuild skips dot-directories at every depth of the recursive walk."""
    dl = tmp_path / "dl"
    hidden_g = dl / ".hidden" / "g"
    hidden_g.mkdir(parents=True)
    (hidden_g / "metadata.toml").write_text('[tags]\nartist = ["x"]\n')

    index = MetadataIndex()
    index.rebuild(dl)

    assert all(".hidden" not in c.folder_path for c in index.comics)
    assert index.facets["artists"] == []


def test_rebuild_follows_symlinked_directories(tmp_path):
    """rebuild indexes a symlinked directory (e.g. an external-drive collection)."""
    external = tmp_path / "external_lib"
    gallery = external / "artist_a"
    gallery.mkdir(parents=True)
    (gallery / "metadata.toml").write_text('[tags]\nartist = ["a"]\n')

    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "linked_lib").symlink_to(external, target_is_directory=True)

    index = MetadataIndex()
    index.rebuild(dl)

    assert "linked_lib/artist_a" in [c.folder_path for c in index.comics]
    assert "a" in index.facets["artists"]


def test_rebuild_breaks_symlink_cycles(tmp_path):
    """rebuild terminates on a symlink cycle instead of recursing forever."""
    dl = tmp_path / "dl"
    dl.mkdir()
    (dl / "a").mkdir()
    (dl / "a" / "b").mkdir()
    (dl / "a" / "b" / "c").symlink_to(dl, target_is_directory=True)  # cycle back to dl
    (dl / "a" / "b" / "metadata.toml").write_text('[tags]\nartist = ["x"]\n')

    index = MetadataIndex()
    index.rebuild(dl)

    # The cycle is cut; the metadata under it is still found exactly once.
    assert [c.folder_path for c in index.comics].count("a/b") == 1
    assert index.facets["artists"] == ["x"]


def test_facets_for_subtree_and_prefix_boundary(tmp_path):
    """facets_for scopes facets to a path prefix without leaking siblings."""
    dl = tmp_path / "dl"
    for rel, artist in (
        ("COMEDY/a", "artist_a"),
        ("COMEDY2/b", "artist_b"),
        ("sample_library/c", "artist_c"),
    ):
        folder = dl / rel
        folder.mkdir(parents=True)
        (folder / "metadata.toml").write_text(
            f'[tags]\nartist = ["{artist}"]\n'
        )

    index = MetadataIndex()
    index.rebuild(dl)

    # COMEDY must not leak COMEDY2 (prefix boundary: "COMEDY2" shares the "COMEDY" prefix)
    assert index.facets_for("COMEDY")["artists"] == ["artist_a"]
    assert index.facets_for("COMEDY2")["artists"] == ["artist_b"]
    assert index.facets_for("")["artists"] == ["artist_a", "artist_b", "artist_c"]
    assert index.facets_for("nope")["artists"] == []


def test_subtree_facets_aggregates_descendants(tmp_path):
    """subtree_facets aggregates every descendant comic's facets per ancestor path."""
    dl = tmp_path / "dl"

    g1 = dl / "COMEDY" / "atelier-sun" / "gallery"
    g1.mkdir(parents=True)
    (g1 / "metadata.toml").write_text('[tags]\nartist = ["atelier-sun"]\n')

    g2 = dl / "COMEDY" / "atelier-sun" / "gallery2"
    g2.mkdir(parents=True)
    (g2 / "metadata.toml").write_text(
        '[tags]\nartist = ["atelier-sun"]\ntag = ["full color"]\n'
    )

    lib = dl / "sample_library" / "series" / "ch-1"
    lib.mkdir(parents=True)
    (lib / "metadata.toml").write_text('[tags]\nartist = ["sample artist"]\n')

    index = MetadataIndex()
    index.rebuild(dl)

    sf = index.subtree_facets()
    assert sf["COMEDY/atelier-sun"]["artists"] == ["atelier-sun"]
    assert sf["COMEDY"]["artists"] == ["atelier-sun"]
    assert sf["COMEDY"]["tags"] == ["full color"]
    assert sf["sample_library"]["artists"] == ["sample artist"]
    assert "sample artist" not in sf["COMEDY"]["artists"]  # subtree boundary


# ── ComicMetadata.to_dict ────────────────────────────────────────────────────────


def test_to_dict_roundtrip():
    """to_dict returns all fields as a plain dict."""
    meta = ComicMetadata(
        folder_path="test",
        folder_name="test",
        title_jp="JP",
        title_en="EN",
        artist=["a"],
        tags=["t"],
        source="generic",
        has_metadata=True,
    )
    d = meta.to_dict()
    assert d["title_jp"] == "JP"
    assert d["artist"] == ["a"]
    assert d["has_metadata"] is True


# ── #108: folders omit-empty contract ─────────────────────────────────────────


def test_folders_omits_empty_metadata_and_facets(tmp_path):
    """#108: nodes without metadata carry no `metadata` key; empty facets add no key.

    Client guards (`f.metadata||{}` in render.js, `node && node.subtree_facets`
    in app.js) treat absence the same as empty, so the keys can be omitted.
    """
    from app.routes import _attach_metadata, _attach_subtree_facets
    from app.scanner import scan_tree

    dl = tmp_path / "dl"
    plain = dl / "plain"  # no metadata.toml
    plain.mkdir(parents=True)
    withmeta = dl / "withmeta"  # metadata.toml with an artist (non-empty facets)
    withmeta.mkdir(parents=True)
    (withmeta / "metadata.toml").write_text('[tags]\nartist = ["a"]\n')

    tree = scan_tree(dl)
    index = MetadataIndex()
    index.rebuild(dl)
    _attach_metadata(tree, {c.folder_path: c for c in index.comics}, "")
    _attach_subtree_facets(tree, index.subtree_facets(), "")

    by_name = {n["name"]: n for n in tree}
    assert by_name["plain"]["has_metadata"] is False
    assert "metadata" not in by_name["plain"]
    assert "subtree_facets" not in by_name["plain"]
    assert by_name["withmeta"]["has_metadata"] is True
    assert "metadata" in by_name["withmeta"]
    assert "subtree_facets" in by_name["withmeta"]


# ── #108: single-walk parity + shared cache ──────────────────────────────────


def _build_parity_tree(dl):
    """Mixed tree: metadata dirs, single-child image chains, symlinks, a cycle."""
    from pathlib import Path as _P  # noqa: F401  (kept local to this section)

    g = dl / "gallery"
    g.mkdir(parents=True)
    (g / "1.png").write_bytes(b"")
    (g / "metadata.toml").write_text('[tags]\nartist = ["a"]\n')

    leaf = dl / "chain" / "only" / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "1.png").write_bytes(b"")
    (leaf / "metadata.toml").write_text('[tags]\nartist = ["b"]\n')

    ext = dl.parent / "ext"
    ab = ext / "artist_b"
    ab.mkdir(parents=True)
    (ab / "1.png").write_bytes(b"")
    (dl / "linked").symlink_to(ext, target_is_directory=True)

    loopx = dl / "loop" / "x"
    loopx.mkdir(parents=True)
    (loopx / "1.png").write_bytes(b"")
    (loopx / "back").symlink_to(dl, target_is_directory=True)
    return dl


def test_rebuild_from_tree_parity(tmp_path):
    """#108: rebuild_from_tree yields the same index set as _walk.

    Covers single-child flatten chains (folder- and image-carried names),
    a symlinked dir, and a symlink cycle. Gate for adopting the single walk:
    any mismatch here means falling back to shared TTL+lock only.
    """
    from app.scanner import scan_tree

    dl = _build_parity_tree(tmp_path / "dl")

    baseline = MetadataIndex()
    baseline.rebuild(dl)

    tree = scan_tree(dl)
    single = MetadataIndex()
    single.rebuild_from_tree(tree, dl)

    assert sorted(c.folder_path for c in single.comics) == sorted(
        c.folder_path for c in baseline.comics
    )
    assert single.facets == baseline.facets
    assert single.subtree_facets() == baseline.subtree_facets()


def _run_coro_in_fresh_loop(coro):
    """Run *coro* on a brand-new thread/loop (immune to ambient event loops)."""
    import asyncio
    import threading

    box = {}

    def _target():
        box["result"] = asyncio.run(coro)

    worker = threading.Thread(target=_target)
    worker.start()
    worker.join()
    return box["result"]


def test_folders_cache_reuses_tree_within_ttl(tmp_path):
    """#108: a fresh cache entry means no rescan — api_folders reuses the tree.

    The cached sentinel tree is returned even though downloads_path points at
    a nonexistent dir (a fresh scan would yield []). The cache timestamp is
    untouched, proving the TTL path was taken.
    """
    import time
    from types import SimpleNamespace

    from app.routes import api_folders

    dl = tmp_path / "dl"
    dl.mkdir()
    index = MetadataIndex()
    index.rebuild(dl)
    sentinel = [{"name": "cached", "type": "folder", "children": [], "thumb": None}]
    stamp = time.time()
    fake = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                metadata_index_cache={"tree": sentinel, "index": index, "timestamp": stamp},
                settings=SimpleNamespace(downloads_path=tmp_path / "nonexistent"),
            )
        )
    )

    result = _run_coro_in_fresh_loop(api_folders(fake))

    # api_folders now returns a Response with the serialized body (#196:
    # /api/folders caches and returns pre-serialized bytes instead of letting
    # FastAPI re-serialize the payload).
    import json as _json

    body = _json.loads(result.body)
    assert body == sentinel
    assert body[0]["name"] == "cached"
    assert fake.app.state.metadata_index_cache["timestamp"] == stamp


def test_rebuild_from_tree_imageless_chain_follows_tree(tmp_path):
    """#108 residual: imageless only-child chains are absent from the tree (a
    pre-existing scan_tree shape fact), so the from-tree index follows suit.

    ``solo`` survives as an (imageless) node because it has a sibling, while
    ``solo/deep`` — the only child of imageless ``solo`` — is dropped from the
    tree and hence from the from-tree index. Every subtree_facets key the two
    indexes disagree on holds empty lists, so the /api/folders response
    (which omits empty facets) is identical either way.
    """
    from app.scanner import scan_tree

    dl = tmp_path / "dl"
    (dl / "solo" / "deep").mkdir(parents=True)
    g = dl / "gallery"
    g.mkdir(parents=True)
    (g / "1.png").write_bytes(b"")

    baseline = MetadataIndex()
    baseline.rebuild(dl)
    assert sorted(c.folder_path for c in baseline.comics) == [
        "gallery",
        "solo",
        "solo/deep",
    ]

    single = MetadataIndex()
    single.rebuild_from_tree(scan_tree(dl), dl)
    assert sorted(c.folder_path for c in single.comics) == ["gallery", "solo"]

    base_sf, single_sf = baseline.subtree_facets(), single.subtree_facets()
    differing = set(base_sf) ^ set(single_sf)
    assert differing, "expected at least the dropped chain to differ"
    for key in differing:
        assert base_sf.get(key, {"artists": [], "tags": []}) == {
            "artists": [],
            "tags": [],
        }


# ── #245: per-file metadata.toml parse cache ──────────────────────────────────


def _toml_parse_spy(monkeypatch):
    """Wrap tomllib.load with a counter; return the list of parsed file names."""
    from app.metadata import models as _models

    calls: list[str] = []
    real_load = _models.tomllib.load

    def _spy(fh, *args, **kwargs):
        calls.append(str(getattr(fh, "name", fh)))
        return real_load(fh, *args, **kwargs)

    monkeypatch.setattr(_models.tomllib, "load", _spy)
    return calls


def test_parse_cache_second_tree_rebuild_does_not_reparse(tmp_path, monkeypatch):
    """#245: a second rebuild_from_tree over unchanged files performs zero parses."""
    from app.scanner import scan_tree

    dl = tmp_path / "dl"
    for name in ("a", "b"):
        d = dl / name
        d.mkdir(parents=True)
        (d / "1.png").write_bytes(b"")
        (d / "metadata.toml").write_text(f'[comic]\ntitle_jp = "{name}"\n')

    calls = _toml_parse_spy(monkeypatch)

    first = MetadataIndex()
    first.rebuild_from_tree(scan_tree(dl), dl)
    assert len(calls) == 2  # one open+parse per existing toml

    calls.clear()
    second = MetadataIndex()
    second.rebuild_from_tree(scan_tree(dl), dl)

    assert calls == []
    assert [c.to_dict() for c in second.comics] == [
        c.to_dict() for c in first.comics
    ]
    assert second.facets == first.facets


def test_parse_cache_negative_entries_also_cached(tmp_path, monkeypatch):
    """#245: folders without metadata.toml never reach tomllib.load at all."""
    dl = tmp_path / "dl"
    for name in ("p1", "p2"):
        d = dl / name
        d.mkdir(parents=True)
        (d / "1.png").write_bytes(b"")

    calls = _toml_parse_spy(monkeypatch)
    MetadataIndex().rebuild(dl)
    MetadataIndex().rebuild(dl)
    assert calls == []


def test_parse_cache_reparses_only_changed_toml(tmp_path, monkeypatch):
    """#245: editing one toml (new mtime) re-parses only that file."""
    import os

    dl = tmp_path / "dl"
    pa = dl / "a" / "metadata.toml"
    pb = dl / "b" / "metadata.toml"
    for p, title in ((pa, "A"), (pb, "B")):
        p.parent.mkdir(parents=True)
        p.write_text(f'[comic]\ntitle_jp = "{title}"\n')

    calls = _toml_parse_spy(monkeypatch)
    MetadataIndex().rebuild(dl)
    assert len(calls) == 2

    calls.clear()
    pb.write_text('[comic]\ntitle_jp = "B-edited"\n')
    # Deterministic identity change: bump mtime explicitly so the test is
    # immune to coarse filesystem mtime granularity and equal file sizes.
    st = pb.stat()
    os.utime(pb, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    second = MetadataIndex()
    second.rebuild(dl)

    assert calls == [str(pb)]
    assert next(c for c in second.comics if c.folder_path == "b").title_jp == "B-edited"
    assert next(c for c in second.comics if c.folder_path == "a").title_jp == "A"


def test_parse_cache_output_identical_warm_vs_cold(tmp_path):
    """#245: warm-cache and cold-cache rebuilds produce identical index output."""
    from app.metadata.models import _clear_parse_cache

    dl = tmp_path / "dl"
    good = dl / "good"
    good.mkdir(parents=True)
    (good / "metadata.toml").write_text(
        '[tags]\nartist = ["x"]\n\n[comic]\ntitle_jp = "G"\ntotal_pages = 12\n'
    )
    broken = dl / "broken"
    broken.mkdir()
    (broken / "metadata.toml").write_text("this is not valid toml [[[")
    (dl / "plain").mkdir()

    _clear_parse_cache()
    cold = MetadataIndex()
    cold.rebuild(dl)

    warm = MetadataIndex()
    warm.rebuild(dl)  # every entry is a cache hit, incl. the broken-toml negative

    assert [c.to_dict() for c in warm.comics] == [c.to_dict() for c in cold.comics]
    assert warm.facets == cold.facets
    assert warm.subtree_facets() == cold.subtree_facets()

    _clear_parse_cache()
    cold2 = MetadataIndex()
    cold2.rebuild(dl)
    assert [c.to_dict() for c in cold2.comics] == [c.to_dict() for c in cold.comics]
    assert cold2.facets == cold.facets


def test_parse_cache_records_not_shared(tmp_path):
    """#245: mutating a record built on the cache-hit path leaks into no other.

    The cache holds the raw parsed TOML dict; each from_folder call derives
    fresh artist/tags lists from it, so per-record mutation is safe.
    """
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "metadata.toml").write_text('[tags]\nartist = ["a"]\ntag = ["t"]\n')

    first = ComicMetadata.from_folder(folder, tmp_path)
    first.artist.append("MUT")
    first.tags.append("MUT")

    second = ComicMetadata.from_folder(folder, tmp_path)  # cache-hit path
    assert second is not first
    assert second.artist == ["a"]
    assert second.tags == ["t"]

"""Refuse an image whose running SQLite lacks the WAL-reset fix or differs from the pinned build.

Run with -I -S at image build and by the candidate gate inside the running container, so it
checks the library Python really loaded. Standard library only.
"""
import sqlite3
import sys

# https://sqlite.org/wal.html, section 11: fixed in 3.51.3, backported to 3.44.6 and 3.50.7.
# 3.52.0 was withdrawn before release notes named the fix, so it doesn't count.
FIRST_FIXED = (3, 51, 3)
BACKPORTS = {(3, 44): 6, (3, 50): 7}
WITHDRAWN = {(3, 52)}

# PRAGMA compile_options of Debian's libsqlite3 3.46.1-7, which ODIN ran before this build,
# without the COMPILER= entry. Two differences: ENABLE_PERCENTILE, which the 3.51 bundle's
# configure turns on with no switch for it, and no ENABLE_FTS5, which is left out on purpose
# (see the Dockerfile). An upgrade reviews this list on purpose.
EXPECTED_OPTIONS = frozenset({
    "ALLOW_ROWID_IN_VIEW", "ATOMIC_INTRINSICS=1", "DEFAULT_AUTOVACUUM",
    "DEFAULT_CACHE_SIZE=-2000", "DEFAULT_FILE_FORMAT=4", "DEFAULT_JOURNAL_SIZE_LIMIT=-1",
    "DEFAULT_MMAP_SIZE=0", "DEFAULT_PAGE_SIZE=4096", "DEFAULT_PCACHE_INITSZ=20",
    "DEFAULT_RECURSIVE_TRIGGERS", "DEFAULT_SECTOR_SIZE=4096", "DEFAULT_SYNCHRONOUS=2",
    "DEFAULT_WAL_AUTOCHECKPOINT=1000", "DEFAULT_WAL_SYNCHRONOUS=2", "DEFAULT_WORKER_THREADS=0",
    "DIRECT_OVERFLOW_READ", "ENABLE_COLUMN_METADATA", "ENABLE_DBPAGE_VTAB", "ENABLE_DBSTAT_VTAB",
    "ENABLE_FTS3", "ENABLE_FTS3_PARENTHESIS", "ENABLE_FTS3_TOKENIZER", "ENABLE_FTS4",
    "ENABLE_LOAD_EXTENSION", "ENABLE_MATH_FUNCTIONS", "ENABLE_PERCENTILE",
    "ENABLE_PREUPDATE_HOOK", "ENABLE_RTREE", "ENABLE_SESSION", "ENABLE_STMTVTAB",
    "ENABLE_UNLOCK_NOTIFY", "ENABLE_UPDATE_DELETE_LIMIT", "HAVE_ISNAN", "LIKE_DOESNT_MATCH_BLOBS",
    "MALLOC_SOFT_LIMIT=1024", "MAX_ATTACHED=10", "MAX_COLUMN=2000", "MAX_COMPOUND_SELECT=500",
    "MAX_DEFAULT_PAGE_SIZE=32768", "MAX_EXPR_DEPTH=1000", "MAX_FUNCTION_ARG=127",
    "MAX_LENGTH=1000000000", "MAX_LIKE_PATTERN_LENGTH=50000", "MAX_MMAP_SIZE=0x7fff0000",
    "MAX_PAGE_COUNT=0xfffffffe", "MAX_PAGE_SIZE=65536", "MAX_SCHEMA_RETRY=25",
    "MAX_SQL_LENGTH=1000000000", "MAX_TRIGGER_DEPTH=1000", "MAX_VARIABLE_NUMBER=250000",
    "MAX_VDBE_OP=250000000", "MAX_WORKER_THREADS=8", "MUTEX_PTHREADS", "SECURE_DELETE", "SOUNDEX",
    "SYSTEM_MALLOC", "TEMP_STORE=1", "THREADSAFE=1", "USE_URI",
})
EXPECTED_LIBRARY = "/usr/local/lib/odin-sqlite/libsqlite3.so.3.51.3"


def has_wal_reset_fix(version):
    series = tuple(version[:2])
    if series in WITHDRAWN:
        return False
    if series in BACKPORTS:
        return version[2] >= BACKPORTS[series]
    return tuple(version) >= FIRST_FIXED


def problems(version, options, library_paths):
    found = []
    if not has_wal_reset_fix(version):
        found.append("SQLite %s lacks the WAL-reset fix" % ".".join(map(str, version)))
    options = {option for option in options if not option.startswith("COMPILER=")}
    if options != EXPECTED_OPTIONS:
        found.append("compile options differ: missing %s, extra %s" % (
            sorted(EXPECTED_OPTIONS - options), sorted(options - EXPECTED_OPTIONS)))
    if set(library_paths) != {EXPECTED_LIBRARY}:
        found.append("loaded %s, expected %s" % (sorted(set(library_paths)), EXPECTED_LIBRARY))
    return found


def _loaded_libraries():
    with open("/proc/self/maps", encoding="utf-8") as maps:
        return [line.split()[-1] for line in maps if "libsqlite3" in line]


def main():
    connection = sqlite3.connect(":memory:")
    options = [row[0] for row in connection.execute("PRAGMA compile_options")]
    connection.close()
    found = problems(sqlite3.sqlite_version_info, options, _loaded_libraries())
    if found:
        for problem in found:
            print("SQLite image check failed: " + problem, file=sys.stderr)
        return 1
    print("SQLite %s: WAL-reset fix present, options as pinned" % sqlite3.sqlite_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

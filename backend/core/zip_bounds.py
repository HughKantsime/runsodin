"""Cheap bounds for untrusted ZIP/3MF packages, checked before zipfile builds its member list.

``zipfile.ZipFile()`` reads the whole central directory into a list as soon as it
opens a package, so a member-count limit checked on ``infolist()`` comes too
late. ``open_bounded`` reads only the end-of-central-directory record (zip64
aware) and refuses absurd member counts or directory sizes first.

Real Bambu Studio / Orca Slicer exports hold a few dozen members (the largest
fixture here has 17; a 36-plate project stays well under 300), so 512 is the
limit already used by the AMS inspection path.
"""
import zipfile

MAX_MEMBERS = 512
MAX_CENTRAL_DIRECTORY_BYTES = 4 * 2**20
MAX_MEMBER_BYTES = 400 * 2**20       # one decompressed member (sliced G-code is the largest)
MAX_TOTAL_BYTES = 500 * 2**20        # the whole package, decompressed


class ArchiveRefused(zipfile.BadZipFile):
    """The package is outside the bounds ODIN opens; ``reason`` says which."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def directory_bounds(fileobj) -> tuple[int, int]:
    """(member count, central directory bytes) from the end record; raises BadZipFile."""
    end = zipfile._EndRecData(fileobj)  # stdlib helper used by ZipFile itself; zip64 aware
    if not end:
        raise zipfile.BadZipFile("File is not a zip file")
    return end[zipfile._ECD_ENTRIES_TOTAL], end[zipfile._ECD_SIZE]


def _check_directory(handle, max_members):
    count, size = directory_bounds(handle)
    if count > max_members:
        raise ArchiveRefused("too_many_members")
    if size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise ArchiveRefused("directory_too_large")


def open_bounded(source, *, max_members: int = MAX_MEMBERS) -> zipfile.ZipFile:
    """ZipFile for a path or seekable binary file object, refused before its directory
    is listed. Raises ArchiveRefused (a BadZipFile) or BadZipFile/OSError."""
    if isinstance(source, (str, bytes)) or hasattr(source, "__fspath__"):
        with open(source, "rb") as handle:
            _check_directory(handle, max_members)
    else:
        _check_directory(source, max_members)
        source.seek(0)
    package = zipfile.ZipFile(source)
    if len(package.infolist()) > max_members:  # a directory that disagrees with its end record
        package.close()
        raise ArchiveRefused("too_many_members")
    return package


def check_expansion(package: zipfile.ZipFile) -> None:
    """Per-member and total decompressed caps (declared sizes bind what zipfile will inflate)."""
    total = 0
    for member in package.infolist():
        if member.file_size > MAX_MEMBER_BYTES:
            raise ArchiveRefused("member_too_large")
        total += member.file_size
    if total > MAX_TOTAL_BYTES:
        raise ArchiveRefused("too_large")

"""A File or a Directory as a value: read at a door into one `FileValue`, written back by the CWL target."""
import pytest

from sophios.lang.values import FileKind, FileValue, Location, read_value, write_value


@pytest.mark.fast
def test_a_file_mapping_is_read_into_its_kind_location_companions_and_the_rest() -> None:
    """A file's companions are files of their own, a `path` is a location that is no URI, and the rest stays."""
    index = {'class': 'File', 'path': 'a.idx'}
    read = read_value({'in': [{'class': 'File', 'location': 'a.txt', 'format': 'f', 'secondaryFiles': [index]}]})
    assert read == {'in': [FileValue(FileKind.FILE, Location('a.txt', True), (
        FileValue(FileKind.FILE, Location('a.idx', False)),), (), 'f', True)]}
    assert write_value(read) == {'in': [{'class': 'File', 'location': 'a.txt', 'secondaryFiles': [index],
                                         'format': 'f'}]}


@pytest.mark.fast
@pytest.mark.parametrize('value', [
    {'path': 'a.txt'},
    {'class': 'Other', 'location': 'a.txt'},
    ['a.txt', 3, None],
    {'class': 'File', 'path': 'a.txt', 'listing': []},
])
def test_what_is_not_a_file_or_holds_no_other_file_is_read_as_it_is_and_written_back(value: object) -> None:
    """No class, no file; an empty listing and a class no door reads are fields like any other."""
    assert write_value(read_value(value)) == value


@pytest.mark.fast
def test_an_empty_listing_stays_a_field_so_a_directory_literal_is_written_as_it_was_written() -> None:
    """The empty directory a runner creates is a Directory with no location."""
    literal = {'class': 'Directory', 'basename': 'out', 'listing': []}
    assert read_value(literal) == FileValue(FileKind.DIRECTORY, None, (), (('basename', 'out'), ('listing', [])))
    assert write_value(read_value(literal)) == literal

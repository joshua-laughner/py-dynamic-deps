import json
from abc import ABC, abstractmethod
from enum import Enum
from glob import glob
from pathlib import Path
import re

from typing import Optional, Tuple, Sequence
from .types import pathlike, strseq


class DependencyError(Exception):
    pass


class UpdateCheckMethod(ABC):
    def __init__(self, cache_file=None):
        self.cache_file = cache_file

    def __repr__(self):
        stem = self.__class__.__name__
        if self.cache_file is not None:
            return '<{} (cache_file={})>'.format(stem, self.cache_file)
        else:
            return '<{}>'.format(stem)

    @abstractmethod
    def is_update_required(self, input_files: strseq, output_file: str, output_alt_path_key: Optional[str] = None) -> bool:
        pass

    @abstractmethod
    def update_cache(self, input_files: strseq, output_file: str, output_alt_path_key: Optional[str] = None):
        pass


class UpdateByMtime(UpdateCheckMethod):
    def is_update_required(self, input_files: strseq, output_file: str, output_alt_path_key: Optional[str] = None) -> bool:
        return _compare_mtimes(input_files, output_file)

    def update_cache(self, input_files: strseq, output_file: str, output_alt_path_key: Optional[str] = None):
        pass


class UpdateByMtimeAndExistance(UpdateCheckMethod):
    def __init__(self, cache_file=None):
        if cache_file is None:
            raise TypeError('cache_file cannot be None for this class')
        super().__init__(cache_file=cache_file)

    def is_update_required(self, input_files: strseq, output_file: str, output_alt_path_key: Optional[str] = None) -> bool:
        cached_list = self._get_json()

        # First test that which input files are required has not changed
        key = self._get_key(output_file, output_alt_path_key)
        previous_files = set(cached_list.get(key, tuple()))
        current_files = set(str(Path(f).resolve()) for f in input_files)
        if previous_files != current_files:
            return True

        # Then if the list of prequisite files is the same, check their modification times
        return _compare_mtimes(input_files, output_file)

    def update_cache(self, input_files: strseq, output_file: str, output_alt_path_key: Optional[str] = None):
        cached_list = self._get_json()

        key = self._get_key(output_file, output_alt_path_key)
        cached_list[key] = sorted(str(Path(i).resolve()) for i in input_files)
        with open(self.cache_file, 'w') as f:
            json.dump(cached_list, f, indent=2)

    def _get_json(self):
        if not Path(self.cache_file).exists():
            return dict()

        with open(self.cache_file) as f:
            return json.load(f)

    @staticmethod
    def _get_key(output_file: str, output_alt_path_key: Optional[str] = None) -> str:
        if output_alt_path_key is None:
            return str(Path(output_file).resolve())
        else:
            return str(Path(output_alt_path_key).resolve())


def _compare_mtimes(input_files: strseq, output_file: str) -> bool:
    output_mtime = Path(output_file).stat().st_mtime
    for input_file in input_files:
        input_mtime = Path(input_file).stat().st_mtime
        if input_mtime > output_mtime:
            return True

    return False


class DependencyTree:
    def __init__(self, how: UpdateCheckMethod):
        self.files = dict()
        self._how = how


class TreeNode:
    def __init__(self, file: pathlike, depends_on=None, dependent_to=None) -> None:
        self.file = str(file)
        self._depends_on = set() if depends_on is None else set(depends_on)
        self._dependent_to = set() if dependent_to is None else set(dependent_to)

    @property
    def key(self):
        return str(Path(self.file).resolve())

    def add_dependency(self, input_file):
        if not isinstance(input_file, self.__class__):
            raise TypeError('input_file must be another TreeNode')
        self._depends_on.add(input_file.key)
        input_file._dependent_to.add(self.key)


class Dependency(ABC):
    @abstractmethod
    def get_files_to_update(self) -> strseq:
        pass

    @abstractmethod
    def get_dependency_pairs(self) -> Tuple[Tuple[str, str]]:
        pass

    @abstractmethod
    def set_files_updated(self):
        pass

    @abstractmethod
    def iter_files(self):
        pass

    @classmethod
    def is_out_of_date(cls, input_files, output_file, how: UpdateCheckMethod = UpdateByMtime(),
                       output_alt_path_key: Optional[str] = None):
        output_file = Path(output_file)
        if not output_file.exists():
            return True

        return how.is_update_required(input_files, str(output_file), output_alt_path_key=output_alt_path_key)

    @classmethod
    def can_be_removed(cls, input_files: strseq, output_file: str):
        return len(input_files) == 0

    @classmethod
    def mark_updated(cls, input_files: strseq, output_file: str, how: UpdateCheckMethod = UpdateByMtime(),
                     output_alt_path_key: Optional[str] = None):
        how.update_cache(input_files, output_file, output_alt_path_key=output_alt_path_key)

    def get_input_output_files(self):
        input_files = set()
        output_files = set()
        for inp, outp in self.iter_files():
            input_files.add(inp)
            output_files.add(outp)

        return sorted(input_files), sorted(output_files)


class OneToOnePatternDependency(Dependency):
    """Represents a 1:1 dependency relation between arbitrary files

    This class allows you to define an abstract dependency relationship
    between one set of files and another by pattern. For example, if you
    were compiling `.c` files to `.o` files, you may not know how many
    `.c` files you have, but you know that for every one it should have
    exactly one corresponding `.o` file.

    As all :class:`Dependency` subclasses, this class has a
    :meth:`get_files_to_update` method that returns a tuple of which output
    files need updated. A file is included in that tuple if its corresponding
    input file is newer than it.

    Parameters
    ----------
    from_pattern
        This is a glob-style pattern used to match input files. It can be
        any valid pattern for :func:`glob.glob`. Note that to enable recursive
        globbing (using the `**` wildcard), `glob_recursive` must be `True`.

    to_pattern
        A pattern that defines the relationship between input file names and
        output file names. As an example, `data/%.{private,public}.dat` means
        that any files with the extension `.private.dat` under `data/` need to
        be converted to files with the extension `.public.dat`. The special
        characters are:

            * `%` matches 0 or more characters (similar to GNU Make)
            * `{ , }` give how the file name changes; anything inside a pair of
              brackets and before the comma will be in the input file name, anything
              after the comma is in the output file name. There can only be one
              comma inside these brackets. If a literal comma needs to be included,
              escape it with a backslash. Curly braces within are treated as normal
              characters.
            * `^` or `$` match the start or end of the filename, respectively.
            * Anything else is treated as a regular character.

    glob_recursive
        Passed to the `recursive` parameter of :func:`glob.glob` when finding all
        the input files.
    """
    def __init__(self, from_pattern: str, to_pattern: str, glob_recursive: bool = False,
                 how: UpdateCheckMethod = UpdateByMtime()) -> None:
        self._from_pattern = from_pattern
        self._regex, self._subst, _ = _PatternParser(to_pattern).parse()
        self._glob_recursive = glob_recursive
        self._how = how

    def get_files_to_update(self) -> Sequence[TreeNode]:
        to_update = []
        for input_file, output_file in self.iter_files():
            if self.is_out_of_date(input_file, output_file, how=self._how):
                to_update.append(TreeNode(output_file, [input_file]))

        return tuple(to_update)

    def get_dependency_pairs(self) -> Tuple[Tuple[str, str]]:
        pairs = []
        for input_file, output_file in self.iter_files():
            pairs.append((str(input_file), str(output_file)))
        return tuple(pairs)

    def set_files_updated(self):
        for input_file, output_file in self.iter_files():
            self.mark_updated([input_file], output_file, how=self._how)

    def iter_files(self) -> Tuple[strseq, str]:
        input_files = glob(self._from_pattern, recursive=self._glob_recursive)
        for input_file in input_files:
            output_file = re.sub(self._regex, self._subst, input_file)
            yield input_file, output_file


class ManyToOnePatternDependency(Dependency):
    """Represents an abstract many to one dependency relationship

    This class allows you to define a relationship where a single output
    file depends on many input files according to a pattern. That is, only
    when asked whether its output file needs updated will it resolve which
    input files it requires, based on which ones are present. For example,
    if you are compiling the executable `cool-program` that depends on
    all `.c` files under `src/`, this class can define that relationship.

    As all :class:`Dependency` subclasses, this class has a
    :meth:`get_files_to_update` method that returns a tuple of which output
    files need updated. In this case, there will only every be zero or
    one files in that tuple. If any of the input files has changed since
    the output file was last updated, it will be listed in the update tuple.

    Parameters
    ----------
    from_pattern
        This is a glob-style pattern used to match input files. It can be
        any valid pattern for :func:`glob.glob`. Note that to enable recursive
        globbing (using the `**` wildcard), `glob_recursive` must be `True`.

    to_file
        Path to the output file.

    glob_recursive
        Passed to the `recursive` parameter of :func:`glob.glob` when finding all
        the input files.
    """
    def __init__(self, from_pattern: str, to_file: pathlike, glob_recursive: bool = False,
                 how: UpdateCheckMethod = UpdateByMtime()) -> None:
        self._from_pattern = from_pattern
        self._to_file = to_file
        self._glob_recursive = glob_recursive
        self._how = how

    def get_files_to_update(self) -> Sequence[TreeNode]:
        input_files = glob(self._from_pattern, recursive=self._glob_recursive)
        if self.is_out_of_date(input_files, self._to_file, how=self._how):
            return (TreeNode(self._to_file, input_files), )
        else:
            return tuple()

    def get_dependency_pairs(self) -> Tuple[Tuple[str, str]]:
        return tuple(p for p in self.iter_files())

    def set_files_updated(self):
        input_files, output_files = self.get_input_output_files()
        self.mark_updated(input_files, output_files[0], how=self._how)

    def iter_files(self):
        input_files = glob(self._from_pattern, recursive=self._glob_recursive)
        output_file = str(self._to_file)
        for input_file in input_files:
            yield str(input_file), output_file


class ManyToVariableOnePatternDependency(Dependency):
    """Represents an abstract many to one dependency relationship where the output file name is not known

    This class allows you to define a relationship where a single output
    file depends on many input files according to a pattern. That is, only
    when asked whether its output file needs updated will it resolve which
    input files it requires, based on which ones are present. For example,
    if you are compiling the executable `cool-program` that depends on
    all `.c` files under `src/`, this class can define that relationship.

    Additionally, this class waits to resolve the path to the output file
    until queried for whether an update is required. The path to the output
    file can be a glob pattern, but it must only ever match 0 or 1 files.
    This is useful if the name of the output file is not specifically known
    but follows some general pattern. Note that this makes it important to
    clean up old versions of the output file!

    As all :class:`Dependency` subclasses, this class has a
    :meth:`get_files_to_update` method that returns a tuple of which output
    files need updated. In this case, there will only every be zero or
    one files in that tuple. If any of the input files has changed since
    the output file was last updated, it will be listed in the update tuple.

    Parameters
    ----------
    from_pattern
        This is a glob-style pattern used to match input files. It can be
        any valid pattern for :func:`glob.glob`. Note that to enable recursive
        globbing (using the `**` wildcard), `glob_recursive` must be `True`.

    to_file
        Path to the output file, or a glob pattern that will match 0 or 1 files.

    glob_recursive
        Passed to the `recursive` parameter of :func:`glob.glob` when finding all
        the input files and the output file.
    """
    def __init__(self, from_pattern: str, to_file: pathlike, glob_recursive: bool = False,
                 how: UpdateCheckMethod = UpdateByMtime()) -> None:
        to_pattern = str(to_file)
        to_file = Path(to_file)

        if not to_file.exists():
            possible_files = glob(to_pattern, recursive=glob_recursive)
            if len(possible_files) == 0:
                to_file = to_pattern
            elif len(possible_files) == 1:
                to_file = possible_files[0]
            else:
                raise DependencyError('Multiple files matched the pattern "{}"'.format(to_file))

        self._from_pattern = from_pattern
        self._to_file = to_file
        self._to_pattern = to_pattern
        self._glob_recursive = glob_recursive
        self._how = how

    def get_files_to_update(self) -> Sequence[TreeNode]:
        input_files = glob(self._from_pattern, recursive=self._glob_recursive)
        if self.is_out_of_date(input_files, self._to_file, how=self._how, output_alt_path_key=self._to_pattern):
            return (TreeNode(self._to_file, input_files), )
        else:
            return tuple()

    def get_dependency_pairs(self, how: UpdateCheckMethod = UpdateByMtime()) -> Tuple[Tuple[str, str]]:
        input_files = glob(self._from_pattern, recursive=self._glob_recursive)
        return tuple((str(i), str(self._to_file)) for i in input_files)

    def set_files_updated(self):
        input_files, output_files = self.get_input_output_files()
        self.mark_updated(input_files, output_files[0], how=self._how, output_alt_path_key=self._to_pattern)

    def iter_files(self):
        input_files = glob(self._from_pattern, recursive=self._glob_recursive)
        output_file = str(self._to_file)
        for input_file in input_files:
            yield str(input_file), output_file


class ParsingError(Exception):
    def __init__(self, s, index, msg):
        msg = 'At index {index} of "{s}": {msg}'.format(index=index, s=s, msg=msg)
        super().__init__(msg)


class _TokenType(Enum):
    STRING = 'string'
    GROUP = 'group'
    SUB = 'sub'
    RE_ONLY = 're only'
    EOF = 'eof'


class _Token:
    def __init__(self, token_type: _TokenType, re_value: str, sub_value: str, glob_value: Optional[str] = None):
        self.type = token_type
        self.re_value = re_value
        self.sub_value = sub_value
        self.glob_value = sub_value if glob_value is None else glob_value

    def __repr__(self):
        if self.type == _TokenType.EOF:
            return '<EOF Token>'
        else:
            return '<{} Token: {}  -->  {}>'.format(self.type.name, self.re_value, self.sub_value)


class _PatternTokenizer:
    _regex_only_chars = {'$', '^'}

    def __init__(self, pattern):
        self.pattern = pattern
        self.index = 0
        self.group = 1

    def __iter__(self):
        self.index = 0
        self.group = 1
        return self

    def __next__(self) -> _Token:
        token = self.next_token()
        if token.type == _TokenType.EOF:
            raise StopIteration
        else:
            return token

    def eat(self, expected=None):
        c = self.curr_char
        if expected is not None and expected != c:
            self._raise_parsing_error('Expected a "{}", found a "{}"'.format(expected, c))
        if c is not None:
            self.index += 1
        return c

    def _raise_parsing_error(self, msg):
        raise ParsingError(index=self.index, s=self.pattern, msg=msg)

    @property
    def curr_char(self):
        return self.pattern[self.index] if self.index < len(self.pattern) else None

    @property
    def special_chars(self):
        return self._regex_only_chars.union({'{', '%'})

    def peek(self):
        i = self.index + 1
        if i < len(self.pattern):
            return self.pattern[i]
        else:
            return None

    def next_token(self) -> _Token:
        if self.curr_char is None:
            return _Token(_TokenType.EOF, '', '')

        if self.curr_char == '{':
            return self._get_sub_token()

        if self.curr_char == '%':
            return self._get_group_token()

        if self.curr_char in self._regex_only_chars:
            return self._get_regex_token()

        return self._get_string_token()

    def _get_sub_token(self) -> _Token:
        start = self.index
        self.eat('{')
        nopen = 1
        comma = None
        while nopen > 0:
            c = self.eat()
            if c is None:
                self._raise_parsing_error('Pattern ends with unterminated {{ from index {}'.format(start))
            elif c == '{':
                nopen += 1
            elif c == '}':
                nopen -= 1
            elif c == ',' and comma is None:
                comma = self.index - 1
            elif c == ',' and comma is not None:
                self._raise_parsing_error('Found a second comma inside a substitution. Escape with a backslash if needed.')
            elif c == '\\' and self.curr_char == ',':
                self.eat()

        end = self.index
        if comma is None:
            self._raise_parsing_error('No comma found inside substitution pattern brackets')

        r = self.pattern[start+1:comma]
        s = self.pattern[comma+1:end-1]
        return _Token(_TokenType.SUB, r, s)

    def _get_group_token(self) -> _Token:
        self.eat('%')
        token = _Token(_TokenType.GROUP, '(.*)', r'\{}'.format(self.group), glob_value='*')
        self.group += 1
        return token

    def _get_regex_token(self) -> _Token:
        c = self.eat()
        if c not in self._regex_only_chars:
            chars = ', '.join(self._regex_only_chars)
            self._raise_parsing_error('Expected one of {}, found {}'.format(chars, c))
        return _Token(_TokenType.RE_ONLY, c, '')

    def _get_string_token(self) -> _Token:
        start = self.index
        c = self.eat()
        specials = self.special_chars
        while c is not None and self.curr_char not in specials:
            c = self.eat()

        s = self.pattern[start:self.index]
        return _Token(_TokenType.STRING, re.escape(s), s)


class _PatternParser:
    def __init__(self, pattern):
        self.tokenizer = _PatternTokenizer(pattern)

    def parse(self):
        re_parts = []
        sub_parts = []
        glob_parts = []

        for token in self.tokenizer:
            re_parts.append(token.re_value)
            sub_parts.append(token.sub_value)
            glob_parts.append(token.glob_value)

        return ''.join(re_parts), ''.join(sub_parts), ''.join(glob_parts)


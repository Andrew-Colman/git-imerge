# -*- coding: utf-8 -*-

# Copyright 2012-2013 Michael Haggerty <mhagger@alum.mit.edu>
#
# This file is part of git-imerge.
#
# git-imerge is free software: you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation, either version 2 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see
# <http://www.gnu.org/licenses/>.

r"""Git incremental merge

Perform the merge between two branches incrementally.  If conflicts
are encountered, figure out exactly which pairs of commits conflict,
and present the user with one pairwise conflict at a time for
resolution.

Multiple incremental merges can be in progress at the same time.  Each
incremental merge has a name, and its progress is recorded in the Git
repository as references under 'refs/imerge/NAME'.

An incremental merge can be interrupted and resumed arbitrarily, or
even pushed to a server to allow somebody else to work on it.


Instructions:

To start an incremental merge or rebase, use one of the following
commands:

    git-imerge merge BRANCH
        Analogous to "git merge BRANCH"

    git-imerge rebase BRANCH
        Analogous to "git rebase BRANCH"

    git-imerge drop [commit | commit1..commit2]
        Drop the specified commit(s) from the current branch

    git-imerge revert [commit | commit1..commit2]
        Revert the specified commits by adding new commits that
        reverse their effects

    git-imerge start --name=NAME --goal=GOAL BRANCH
        Start a general imerge

Then the tool will present conflicts to you one at a time, similar to
"git rebase --incremental".  Resolve each conflict, and then

    git add FILE...
    git-imerge continue

You can view your progress at any time with

    git-imerge diagram

When you have resolved all of the conflicts, simplify and record the
result by typing

    git-imerge finish

To get more help about any git-imerge subcommand, type

    git-imerge SUBCOMMAND --help

"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import locale
import sys
import re
import subprocess
from subprocess import CalledProcessError
from subprocess import check_call
import itertools
import argparse
from io import StringIO
import json
import os


PREFERRED_ENCODING = locale.getpreferredencoding()


# Define check_output() for ourselves, including decoding of the
# output into PREFERRED_ENCODING:
def check_output(*popenargs, **kwargs):
    if 'stdout' in kwargs:
        raise ValueError('stdout argument not allowed, it will be overridden.')
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output = communicate(process)[0]
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        # We don't store output in the CalledProcessError because
        # the "output" keyword parameter was not supported in
        # Python 2.6:
        raise CalledProcessError(retcode, cmd)
    return output


STATE_VERSION = (1, 3, 0)

ZEROS = '0' * 40

ALLOWED_GOALS = [
    'full',
    'rebase',
    'rebase-with-history',
    'border',
    'border-with-history',
    'border-with-history2',
    'merge',
    'drop',
    'revert',
    ]
DEFAULT_GOAL = 'merge'


class Failure(Exception):
    """An exception that indicates a normal failure of the script.

    Failures are reported at top level via sys.exit(str(e)) rather
    than via a Python stack dump."""

    pass


class AnsiColor:
    BLACK = '\033[0;30m'
    RED = '\033[0;31m'
    GREEN = '\033[0;32m'
    YELLOW = '\033[0;33m'
    BLUE = '\033[0;34m'
    MAGENTA = '\033[0;35m'
    CYAN = '\033[0;36m'
    B_GRAY = '\033[0;37m'
    D_GRAY = '\033[1;30m'
    B_RED = '\033[1;31m'
    B_GREEN = '\033[1;32m'
    B_YELLOW = '\033[1;33m'
    B_BLUE = '\033[1;34m'
    B_MAGENTA = '\033[1;35m'
    B_CYAN = '\033[1;36m'
    WHITE = '\033[1;37m'
    END = '\033[0m'

    @classmethod
    def disable(cls):
        cls.BLACK = ''
        cls.RED = ''
        cls.GREEN = ''
        cls.YELLOW = ''
        cls.BLUE = ''
        cls.MAGENTA = ''
        cls.CYAN = ''
        cls.B_GRAY = ''
        cls.D_GRAY = ''
        cls.B_RED = ''
        cls.B_GREEN = ''
        cls.B_YELLOW = ''
        cls.B_BLUE = ''
        cls.B_MAGENTA = ''
        cls.B_CYAN = ''
        cls.WHITE = ''
        cls.END = ''


def iter_neighbors(iterable):
    """For an iterable (x0, x1, x2, ...) generate [(x0,x1), (x1,x2), ...]."""

    i = iter(iterable)

    try:
        last = next(i)
    except StopIteration:
        return

    for x in i:
        yield (last, x)
        last = x


def find_first_false(f, lo, hi):
    """Return the smallest i in lo <= i < hi for which f(i) returns False using bisection.

    If there is no such i, return hi.

    """

    # Loop invariant: f(i) returns True for i < lo; f(i) returns False
    # for i >= hi.

    while lo < hi:
        mid = (lo + hi) // 2
        if f(mid):
            lo = mid + 1
        else:
            hi = mid

    return lo


def call_silently(cmd):
    try:
        NULL = open(os.devnull, 'w')
    except (IOError, AttributeError):
        NULL = subprocess.PIPE

    p = subprocess.Popen(cmd, stdout=NULL, stderr=NULL)
    p.communicate()
    retcode = p.wait()
    if retcode:
        raise CalledProcessError(retcode, cmd)


def communicate(process, input=None):
    """Return decoded output from process."""
    if input is not None:
        input = input.encode(PREFERRED_ENCODING)

    output, error = process.communicate(input)

    output = None if output is None else output.decode(PREFERRED_ENCODING)
    error = None if error is None else error.decode(PREFERRED_ENCODING)

    return (output, error)


if sys.hexversion >= 0x03000000:
    # In Python 3.x, os.environ keys and values must be unicode
    # strings:
    def env_encode(s):
        """Use unicode keys or values unchanged in os.environ."""

        return s

else:
    # In Python 2.x, os.environ keys and values must be byte
    # strings:
    def env_encode(s):
        """Encode unicode keys or values for use in os.environ."""

        return s.encode(PREFERRED_ENCODING)


class UncleanWorkTreeError(Failure):
    pass


class AutomaticMergeFailed(Exception):
    def __init__(self, commit1, commit2):
        Exception.__init__(
            self, 'Automatic merge of %s and %s failed' % (commit1, commit2,)
            )
        self.commit1, self.commit2 = commit1, commit2


class InvalidBranchNameError(Failure):
    pass


class NotFirstParentAncestorError(Failure):
    def __init__(self, commit1, commit2):
        Failure.__init__(
            self,
            'Commit "%s" is not a first-parent ancestor of "%s"'
            % (commit1, commit2),
            )


class NonlinearAncestryError(Failure):
    def __init__(self, commit1, commit2):
        Failure.__init__(
            self,
            'The history "%s..%s" is not linear'
            % (commit1, commit2),
            )


class NothingToDoError(Failure):
    def __init__(self, src_tip, dst_tip):
        Failure.__init__(
            self,
            'There are no commits on "%s" that are not already in "%s"'
            % (src_tip, dst_tip),
            )


class GitTemporaryHead(object):
    """A context manager that records the current HEAD state then restores it.

    This should only be used when the working copy is clean. message
    is used for the reflog.

    """

    def __init__(self, git, message):
        self.git = git
        self.message = message

    def __enter__(self):
        self.head_name = self.git.get_head_refname()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.head_name:
            try:
                self.git.restore_head(self.head_name, self.message)
            except CalledProcessError as e:
                raise Failure(
                    'Could not restore HEAD to %r!:    %s\n'
                    % (self.head_name, e.message,)
                    )

        return False


class GitRepository(object):
    BRANCH_PREFIX = 'refs/heads/'

    MERGE_STATE_REFNAME_RE = re.compile(
        r"""
        ^
        refs\/imerge\/
        (?P<name>.+)
        \/state
        $
        """,
        re.VERBOSE,
        )

    def __init__(self):
        self.git_dir_cache = None

    def git_dir(self):
        if self.git_dir_cache is None:
            self.git_dir_cache = check_output(
                ['git', 'rev-parse', '--git-dir']
                ).rstrip('\n')

        return self.git_dir_cache

    def check_imerge_name_format(self, name):
        """Check that name is a valid imerge name."""

        try:
            call_silently(
                ['git', 'check-ref-format', 'refs/imerge/%s' % (name,)]
                )
        except CalledProcessError:
            raise Failure('Name %r is not a valid refname component!' % (name,))

    def check_branch_name_format(self, name):
        """Check that name is a valid branch name."""

        try:
            call_silently(
                ['git', 'check-ref-format', 'refs/heads/%s' % (name,)]
                )
        except CalledProcessError:
            raise InvalidBranchNameError('Name %r is not a valid branch name!' % (name,))

    def iter_existing_imerge_names(self):
        """Iterate over the names of existing MergeStates in this repo."""

        for line in check_output(['git', 'for-each-ref', 'refs/imerge']).splitlines():
            (sha1, type, refname) = line.split()
            if type == 'blob':
                m = GitRepository.MERGE_STATE_REFNAME_RE.match(refname)
                if m:
                    yield m.group('name')

    def set_default_imerge_name(self, name):
        """Set the default merge to the specified one.

        name can be None to cause the default to be cleared."""

        if name is None:
            try:
                check_call(['git', 'config', '--unset', 'imerge.default'])
            except CalledProcessError as e:
                if e.returncode == 5:
                    # Value was not set
                    pass
                else:
                    raise
        else:
            check_call(['git', 'config', 'imerge.default', name])

    def get_default_imerge_name(self):
        """Get the name of the default merge, or None if it is currently unset."""

        try:
            return check_output(['git', 'config', 'imerge.default']).rstrip()
        except CalledProcessError:
            return None

    def get_default_edit(self):
        """Should '--edit' be used when committing intermediate user merges?

        When 'git imerge continue' or 'git imerge record' finds a user
        merge that can be committed, should it (by default) ask the user
        to edit the commit message? This behavior can be configured via
        'imerge.editmergemessages'. If it is not configured, return False.

        Please note that this function is only used to choose the default
        value. It can be overridden on the command line using '--edit' or
        '--no-edit'.

        """

        try:
            return {'true' : True, 'false' : False}[
                check_output(
                    ['git', 'config', '--bool', 'imerge.editmergemessages']
                    ).rstrip()
                ]
        except CalledProcessError:
            return False

    def unstaged_changes(self):
        """Return True iff there are unstaged changes in the working copy"""

        try:
            check_call(['git', 'diff-files', '--quiet', '--ignore-submodules'])
            return False
        except CalledProcessError:
            return True

    def uncommitted_changes(self):
        """Return True iff the index contains uncommitted changes."""

        try:
            check_call([
                'git', 'diff-index', '--cached', '--quiet',
                '--ignore-submodules', 'HEAD', '--',
                ])
            return False
        except CalledProcessError:
            return True

    def get_commit_sha1(self, arg):
        """Convert arg into a SHA1 and verify that it refers to a commit.

        If not, raise ValueError."""

        try:
            return self.rev_parse('%s^{commit}' % (arg,))
        except CalledProcessError:
            raise ValueError('%r does not refer to a valid git commit' % (arg,))

    def refresh_index(self):
        process = subprocess.Popen(
            ['git', 'update-index', '-q', '--ignore-submodules', '--refresh'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        out, err = communicate(process)
        retcode = process.poll()
        if retcode:
            raise UncleanWorkTreeError(err.rstrip() or out.rstrip())

    def verify_imerge_name_available(self, name):
        self.check_imerge_name_format(name)
        if check_output(['git', 'for-each-ref', 'refs/imerge/%s' % (name,)]):
            raise Failure('Name %r is already in use!' % (name,))

    def check_imerge_exists(self, name):
        """Verify that a MergeState with the given name exists.

        Just check for the existence, readability, and compatible
        version of the 'state' reference.  If the reference doesn't
        exist, just return False.  If it exists but is unusable for
        some other reason, raise an exception."""

        self.check_imerge_name_format(name)
        state_refname = 'refs/imerge/%s/state' % (name,)
        for line in check_output(['git', 'for-each-ref', state_refname]).splitlines():
            (sha1, type, refname) = line.split()
            if refname == state_refname and type == 'blob':
                self.read_imerge_state_dict(name)
                # If that didn't throw an exception:
                return True
        else:
            return False

    def read_imerge_state_dict(self, name):
        state_string = check_output(
            ['git', 'cat-file', 'blob', 'refs/imerge/%s/state' % (name,)],
            )
        state = json.loads(state_string)

        # Convert state['version'] to a tuple of integers, and verify
        # that it is compatible with this version of the script:
        version = tuple(int(i) for i in state['version'].split('.'))
        if version[0] != STATE_VERSION[0] or version[1] > STATE_VERSION[1]:
            raise Failure(
                'The format of imerge %s (%s) is not compatible with this script version.'
                % (name, state['version'],)
                )
        state['version'] = version

        return state

    def read_imerge_state(self, name):
        """Read the state associated with the specified imerge.

        Return the tuple

            (state_dict, {(i1, i2) : (sha1, source), ...})

        , where source is 'auto' or 'manual'. Validity is checked only
        lightly.

        """

        merge_ref_re = re.compile(
            r"""
            ^
            refs\/imerge\/
            """ + re.escape(name) + r"""
            \/(?P<source>auto|manual)\/
            (?P<i1>0|[1-9][0-9]*)
            \-
            (?P<i2>0|[1-9][0-9]*)
            $
            """,
            re.VERBOSE,
            )

        state_ref_re = re.compile(
            r"""
            ^
            refs\/imerge\/
            """ + re.escape(name) + r"""
            \/state
            $
            """,
            re.VERBOSE,
            )

        state = None

        # A map {(i1, i2) : (sha1, source)}:
        merges = {}

        # refnames that were found but not understood:
        unexpected = []

        for line in check_output([
                'git', 'for-each-ref', 'refs/imerge/%s' % (name,)
                ]).splitlines():
            (sha1, type, refname) = line.split()
            m = merge_ref_re.match(refname)
            if m:
                if type != 'commit':
                    raise Failure('Reference %r is not a commit!' % (refname,))
                i1, i2 = int(m.group('i1')), int(m.group('i2'))
                source = m.group('source')
                merges[i1, i2] = (sha1, source)
                continue

            m = state_ref_re.match(refname)
            if m:
                if type != 'blob':
                    raise Failure('Reference %r is not a blob!' % (refname,))
                state = self.read_imerge_state_dict(name)
                continue

            unexpected.append(refname)

        if state is None:
            raise Failure(
                'No state found; it should have been a blob reference at '
                '"refs/imerge/%s/state"' % (name,)
                )

        if unexpected:
            raise Failure(
                'Unexpected reference(s) found in "refs/imerge/%s" namespace:\n    %s\n'
                % (name, '\n    '.join(unexpected),)
                )

        return (state, merges)

    def write_imerge_state_dict(self, name, state):
        state_string = json.dumps(state, sort_keys=True) + '\n'

        cmd = ['git', 'hash-object', '-t', 'blob', '-w', '--stdin']
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        out = communicate(p, input=state_string)[0]
        retcode = p.poll()
        if retcode:
            raise CalledProcessError(retcode, cmd)
        sha1 = out.strip()
        check_call([
            'git', 'update-ref',
            '-m', 'imerge %r: Record state' % (name,),
            'refs/imerge/%s/state' % (name,),
            sha1,
            ])

    def is_ancestor(self, commit1, commit2):
        """Return True iff commit1 is an ancestor (or equal to) commit2."""

        if commit1 == commit2:
            return True
        else:
            return int(
                check_output([
                    'git', 'rev-list', '--count', '--ancestry-path',
                    '%s..%s' % (commit1, commit2,),
                    ]).strip()
                ) != 0

    def is_ff(self, refname, commit):
        """Would updating refname to commit be a fast-forward update?

        Return True iff refname is not currently set or it points to an
        ancestor of commit.

        """

        try:
            ref_oldval = self.get_commit_sha1(refname)
        except ValueError:
            # refname doesn't already exist; no problem.
            return True
        else:
            return self.is_ancestor(ref_oldval, commit)

    def automerge(self, commit1, commit2, msg=None):
        """Attempt an automatic merge of commit1 and commit2.

        Return the SHA1 of the resulting commit, or raise
        AutomaticMergeFailed on error.  This must be called with a clean
        worktree."""

        call_silently(['git', 'checkout', '-f', commit1])
        cmd = ['git', '-c', 'rerere.enabled=false', 'merge']
        if msg is not None:
            cmd += ['-m', msg]
        cmd += [commit2]
        try:
            call_silently(cmd)
        except CalledProcessError:
            self.abort_merge()
            raise AutomaticMergeFailed(commit1, commit2)
        else:
            return self.get_commit_sha1('HEAD')

    def manualmerge(self, commit, msg):
        """Initiate a merge of commit into the current HEAD."""

        check_call(['git', 'merge', '--no-commit', '-m', msg, commit,])

    def require_clean_work_tree(self, action):
        """Verify that the current tree is clean.

        The code is a Python translation of the git-sh-setup(1) function
        of the same name."""

        process = subprocess.Popen(
            ['git', 'rev-parse', '--verify', 'HEAD'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        err = communicate(process)[1]
        retcode = process.poll()
        if retcode:
            raise UncleanWorkTreeError(err.rstrip())

        self.refresh_index()

        error = []
        if self.unstaged_changes():
            error.append('Cannot %s: You have unstaged changes.' % (action,))

        if self.uncommitted_changes():
            if not error:
                error.append('Cannot %s: Your index contains uncommitted changes.' % (action,))
            else:
                error.append('Additionally, your index contains uncommitted changes.')

        if error:
            raise UncleanWorkTreeError('\n'.join(error))

    def simple_merge_in_progress(self):
        """Return True iff a merge (of a single branch) is in progress."""

        try:
            with open(os.path.join(self.git_dir(), 'MERGE_HEAD')) as f:
                heads = [line.rstrip() for line in f]
        except IOError:
            return False

        return len(heads) == 1

    def commit_user_merge(self, edit_log_msg=None):
        """If a merge is in progress and ready to be committed, commit it.

        If a simple merge is in progress and any changes in the working
        tree are staged, commit the merge commit and return True.
        Otherwise, return False.

        """

        if not self.simple_merge_in_progress():
            return False

        # Check if all conflicts are resolved and everything in the
        # working tree is staged:
        self.refresh_index()
        if self.unstaged_changes():
            raise UncleanWorkTreeError(
                'Cannot proceed: You have unstaged changes.'
                )

        # A merge is in progress, and either all changes have been staged
        # or no changes are necessary. Create a merge commit.
        cmd = ['git', 'commit', '--no-verify']

        if edit_log_msg is None:
            edit_log_msg = self.get_default_edit()

        if edit_log_msg:
            cmd += ['--edit']
        else:
            cmd += ['--no-edit']

        try:
            check_call(cmd)
        except CalledProcessError:
            raise Failure('Could not commit staged changes.')

        return True

    def create_commit_chain(self, base, path):
        """Point refname at the chain of commits indicated by path.

        path is a list [(commit, metadata), ...]. Create a series of
        commits corresponding to the entries in path. Each commit's tree
        is taken from the corresponding old commit, and each commit's
        metadata is taken from the corresponding metadata commit. Use base
        as the parent of the first commit, or make the first commit a root
        commit if base is None. Reuse existing commits from the list
        whenever possible.

        Return a commit object corresponding to the last commit in the
        chain.

        """

        reusing = True
        if base is None:
            if not path:
                raise ValueError('neither base nor path specified')
            parents = []
        else:
            parents = [base]

        for (commit, metadata) in path:
            if reusing:
                if commit == metadata and self.get_commit_parents(commit) == parents:
                    # We can reuse this commit, too.
                    parents = [commit]
                    continue
                else:
                    reusing = False

            # Create a commit, copying the old log message and author info
            # from the metadata commit:
            tree = self.get_tree(commit)
            new_commit = self.commit_tree(
                tree, parents,
                msg=self.get_log_message(metadata),
                metadata=self.get_author_info(metadata),
                )
            parents = [new_commit]

        [commit] = parents
        return commit

    def rev_parse(self, arg):
        return check_output(['git', 'rev-parse', '--verify', '--quiet', arg]).strip()

    def rev_list_with_parents(self, *args):
        """Iterate over (commit, [parent,...])."""

        cmd = ['git', 'log', '--format=%H %P'] + list(args)
        for line in check_output(cmd).splitlines():
            commits = line.strip().split()
            yield (commits[0], commits[1:])

    def summarize_commit(self, commit):
        """Summarize `commit` to stdout."""

        check_call(['git', '--no-pager', 'log', '--no-walk', commit])

    def get_author_info(self, commit):
        """Return environment settings to set author metadata.

        Return a map {str : str}."""

        # We use newlines as separators here because msysgit has problems
        # with NUL characters; see
        #
        #     https://github.com/mhagger/git-imerge/pull/71
        a = check_output([
            'git', '--no-pager', 'log', '-n1',
            '--format=%an%n%ae%n%ai', commit
            ]).strip().splitlines()

        return {
            'GIT_AUTHOR_NAME': env_encode(a[0]),
            'GIT_AUTHOR_EMAIL': env_encode(a[1]),
            'GIT_AUTHOR_DATE': env_encode(a[2]),
            }

    def get_log_message(self, commit):
        contents = check_output([
            'git', 'cat-file', 'commit', commit,
            ]).splitlines(True)
        contents = contents[contents.index('\n') + 1:]
        if contents and contents[-1][-1:] != '\n':
            contents.append('\n')
        return ''.join(contents)

    def get_commit_parents(self, commit):
        """Return a list containing the parents of commit."""

        return check_output(
            ['git', '--no-pager', 'log', '--no-walk', '--pretty=format:%P', commit]
            ).strip().split()

    def get_tree(self, arg):
        return self.rev_parse('%s^{tree}' % (arg,))

    def update_ref(self, refname, value, msg, deref=True):
        if deref:
            opt = []
        else:
            opt = ['--no-deref']

        check_call(['git', 'update-ref'] + opt + ['-m', msg, refname, value])

    def delete_ref(self, refname, msg, deref=True):
        if deref:
            opt = []
        else:
            opt = ['--no-deref']

        check_call(['git', 'update-ref'] + opt + ['-m', msg, '-d', refname])

    def delete_imerge_refs(self, name):
        stdin = ''.join(
            'delete %s\n' % (refname,)
            for refname in check_output([
                    'git', 'for-each-ref',
                    '--format=%(refname)',
                    'refs/imerge/%s' % (name,)
                    ]).splitlines()
            )

        process = subprocess.Popen(
            [
                'git', 'update-ref',
                '-m', 'imerge: remove merge %r' % (name,),
                '--stdin',
                ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        out = communicate(process, input=stdin)[0]
        retcode = process.poll()
        if retcode:
            sys.stderr.write(
                'Warning: error removing references:\n%s' % (out,)
                )

    def detach(self, msg):
        """Detach HEAD. msg is used for the reflog."""

        self.update_ref('HEAD', 'HEAD^0', msg, deref=False)

    def reset_hard(self, commit):
        check_call(['git', 'reset', '--hard', commit])

    def amend(self):
        check_call(['git', 'commit', '--amend'])

    def abort_merge(self):
        # We don't use "git merge --abort" here because it was
        # only added in git version 1.7.4.
        check_call(['git', 'reset', '--merge'])

    def compute_best_merge_base(self, tip1, tip2):
        try:
            merge_bases = check_output(['git', 'merge-base', '--all', tip1, tip2]).splitlines()
        except CalledProcessError:
            raise Failure('Cannot compute merge base for %r and %r' % (tip1, tip2))
        if not merge_bases:
            raise Failure('%r and %r do not have a common merge base' % (tip1, tip2))
        if len(merge_bases) == 1:
            return merge_bases[0]

        # There are multiple merge bases. The "best" one is the one that
        # is the "closest" to the tips, which we define to be the one with
        # the fewest non-merge commits in "merge_base..tip". (It can be
        # shown that the result is independent of which tip is used in the
        # computation.)
        best_base = best_count = None
        for merge_base in merge_bases:
            cmd = ['git', 'rev-list', '--no-merges', '--count', '%s..%s' % (merge_base, tip1)]
            count = int(check_output(cmd).strip())
            if best_base is None or count < best_count:
                best_base = merge_base
                best_count = count

        return best_base

    def linear_ancestry(self, commit1, commit2, first_parent):
        """Compute a linear ancestry between commit1 and commit2.

        Our goal is to find a linear series of commits connecting
        `commit1` and `commit2`. We do so as follows:

        * If all of the commits in

              git rev-list --ancestry-path commit1..commit2

          are on a linear chain, return that.

        * If there are multiple paths between `commit1` and `commit2` in
          that list of commits, then

          * If `first_parent` is not set, then raise an
            `NonlinearAncestryError` exception.

          * If `first_parent` is set, then, at each merge commit, follow
            the first parent that is in that list of commits.

        Return a list of SHA-1s in 'chronological' order.

        Raise NotFirstParentAncestorError if commit1 is not an ancestor of
        commit2.

        """

        oid1 = self.rev_parse(commit1)
        oid2 = self.rev_parse(commit2)

        parentage = {oid1 : []}
        for (commit, parents) in self.rev_list_with_parents(
                '--ancestry-path', '--topo-order', '%s..%s' % (oid1, oid2)
                ):
            parentage[commit] = parents

        commits = []

        commit = oid2
        while commit != oid1:
            parents = parentage.get(commit, [])

            # Only consider parents that are in the ancestry path:
            included_parents = [
                parent
                for parent in parents
                if parent in parentage
            ]

            if not included_parents:
                raise NotFirstParentAncestorError(commit1, commit2)
            elif len(included_parents) == 1 or first_parent:
                parent = included_parents[0]
            else:
                raise NonlinearAncestryError(commit1, commit2)

            commits.append(commit)
            commit = parent

        commits.reverse()

        return commits

    def get_boundaries(self, tip1, tip2, first_parent):
        """Get the boundaries of an incremental merge.

        Given the tips of two branches that should be merged, return
        (merge_base, commits1, commits2) describing the edges of the
        imerge.  Raise Failure if there are any problems."""

        merge_base = self.compute_best_merge_base(tip1, tip2)

        commits1 = self.linear_ancestry(merge_base, tip1, first_parent)
        if not commits1:
            raise NothingToDoError(tip1, tip2)

        commits2 = self.linear_ancestry(merge_base, tip2, first_parent)
        if not commits2:
            raise NothingToDoError(tip2, tip1)

        return (merge_base, commits1, commits2)

    def get_head_refname(self, short=False):
        """Return the name of the reference that is currently checked out.

        If `short` is set, return it as a branch name. If HEAD is
        currently detached, return None."""

        cmd = ['git', 'symbolic-ref', '--quiet']
        if short:
            cmd += ['--short']
        cmd += ['HEAD']
        try:
            return check_output(cmd).strip()
        except CalledProcessError:
            return None

    def restore_head(self, refname, message):
        check_call(['git', 'symbolic-ref', '-m', message, 'HEAD', refname])
        check_call(['git', 'reset', '--hard'])

    def checkout(self, refname, quiet=False):
        cmd = ['git', 'checkout']
        if quiet:
            cmd += ['--quiet']
        if refname.startswith(GitRepository.BRANCH_PREFIX):
            target = refname[len(GitRepository.BRANCH_PREFIX):]
        else:
            target = '%s^0' % (refname,)
        cmd += [target]
        check_call(cmd)

    def commit_tree(self, tree, parents, msg, metadata=None):
        """Create a commit containing the specified tree.

        metadata can be author or committer information to be added to the
        environment, as str objects; e.g., {'GIT_AUTHOR_NAME' : 'me'}.

        Return the SHA-1 of the new commit object."""

        cmd = ['git', 'commit-tree', tree]
        for parent in parents:
            cmd += ['-p', parent]

        if metadata is not None:
            env = os.environ.copy()
            env.update(metadata)
        else:
            env = os.environ

        process = subprocess.Popen(
            cmd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        out = communicate(process, input=msg)[0]
        retcode = process.poll()

        if retcode:
            # We don't store the output in the CalledProcessError because
            # the "output" keyword parameter was not supported in Python
            # 2.6:
            raise CalledProcessError(retcode, cmd)

        return out.strip()

    def revert(self, commit):
        """Apply the inverse of commit^..commit to HEAD and commit."""

        cmd = ['git', 'revert', '--no-edit']
        if len(self.get_commit_parents(commit)) > 1:
            cmd += ['-m', '1']
        cmd += [commit]
        check_call(cmd)

    def reparent(self, commit, parent_sha1s, msg=None):
        """Create a new commit object like commit, but with the specified parents.

        commit is the SHA1 of an existing commit and parent_sha1s is a
        list of SHA1s.  Create a new commit exactly like that one, except
        that it has the specified parent commits.  Return the SHA1 of the
        resulting commit object, which is already stored in the object
        database but is not yet referenced by anything.

        If msg is set, then use it as the commit message for the new
        commit."""

        old_commit = check_output(['git', 'cat-file', 'commit', commit])
        separator = old_commit.index('\n\n')
        headers = old_commit[:separator + 1].splitlines(True)
        rest = old_commit[separator + 2:]

        new_commit = StringIO()
        for i in range(len(headers)):
            line = headers[i]
            if line.startswith('tree '):
                new_commit.write(line)
                for parent_sha1 in parent_sha1s:
                    new_commit.write('parent %s\n' % (parent_sha1,))
            elif line.startswith('parent '):
                # Discard old parents:
                pass
            else:
                new_commit.write(line)

        new_commit.write('\n')
        if msg is None:
            new_commit.write(rest)
        else:
            new_commit.write(msg)
            if not msg.endswith('\n'):
                new_commit.write('\n')

        process = subprocess.Popen(
            ['git', 'hash-object', '-t', 'commit', '-w', '--stdin'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
        out = communicate(process, input=new_commit.getvalue())[0]
        retcode = process.poll()
        if retcode:
            raise Failure('Could not reparent commit %s' % (commit,))
        return out.strip()

    def temporary_head(self, message):
        """Return a context manager to manage a temporary HEAD.

        On entry, record the current HEAD state. On exit, restore it.
        message is used for the reflog.

        """

        return GitTemporaryHead(self, message)


class MergeRecord(object):
    # Bits for the flags field:

    # There is a saved successful auto merge:
    SAVED_AUTO = 0x01

    # An auto merge (which may have been unsuccessful) has been done:
    NEW_AUTO = 0x02

    # There is a saved successful manual merge:
    SAVED_MANUAL = 0x04

    # A manual merge (which may have been unsuccessful) has been done:
    NEW_MANUAL = 0x08

    # A merge that is currently blocking the merge frontier:
    BLOCKED = 0x10

    # Some useful bit combinations:
    SAVED = SAVED_AUTO | SAVED_MANUAL
    NEW = NEW_AUTO | NEW_MANUAL

    AUTO = SAVED_AUTO | NEW_AUTO
    MANUAL = SAVED_MANUAL | NEW_MANUAL

    ALLOWED_INITIAL_FLAGS = [
        SAVED_AUTO,
        SAVED_MANUAL,
        NEW_AUTO,
        NEW_MANUAL,
        ]

    def __init__(self, sha1=None, flags=0):
        # The currently believed correct merge, or None if it is
        # unknown or the best attempt was unsuccessful.
        self.sha1 = sha1

        if self.sha1 is None:
            if flags != 0:
                raise ValueError('Initial flags (%s) for sha1=None should be 0' % (flags,))
        elif flags not in self.ALLOWED_INITIAL_FLAGS:
            raise ValueError('Initial flags (%s) is invalid' % (flags,))

        # See bits above.
        self.flags = flags

    def record_merge(self, sha1, source):
        """Record a merge at this position.

        source must be SAVED_AUTO, SAVED_MANUAL, NEW_AUTO, or NEW_MANUAL."""

        if source == self.SAVED_AUTO:
            # SAVED_AUTO is recorded in any case, but only used if it
            # is the only info available.
            if self.flags & (self.MANUAL | self.NEW) == 0:
                self.sha1 = sha1
            self.flags |= source
        elif source == self.NEW_AUTO:
            # NEW_AUTO is silently ignored if any MANUAL value is
            # already recorded.
            if self.flags & self.MANUAL == 0:
                self.sha1 = sha1
                self.flags |= source
        elif source == self.SAVED_MANUAL:
            # SAVED_MANUAL is recorded in any case, but only used if
            # no NEW_MANUAL is available.
            if self.flags & self.NEW_MANUAL == 0:
                self.sha1 = sha1
            self.flags |= source
        elif source == self.NEW_MANUAL:
            # NEW_MANUAL is always used, and also causes NEW_AUTO to
            # be forgotten if present.
            self.sha1 = sha1
            self.flags = (self.flags | source) & ~self.NEW_AUTO
        else:
            raise ValueError('Undefined source: %s' % (source,))

    def record_blocked(self, blocked):
        if blocked:
            self.flags |= self.BLOCKED
        else:
            self.flags &= ~self.BLOCKED

    def is_known(self):
        return self.sha1 is not None

    def is_blocked(self):
        return self.flags & self.BLOCKED != 0

    def is_manual(self):
        return self.flags & self.MANUAL != 0

    def save(self, git, name, i1, i2):
        """If this record has changed, save it."""

        def set_ref(source):
            git.update_ref(
                'refs/imerge/%s/%s/%d-%d' % (name, source, i1, i2),
                self.sha1,
                'imerge %r: Record %s merge' % (name, source,),
                )

        def clear_ref(source):
            git.delete_ref(
                'refs/imerge/%s/%s/%d-%d' % (name, source, i1, i2),
                'imerge %r: Remove obsolete %s merge' % (name, source,),
                )

        if self.flags & self.MANUAL:
            if self.flags & self.AUTO:
                # Any MANUAL obsoletes any AUTO:
                if self.flags & self.SAVED_AUTO:
                    clear_ref('auto')

                self.flags &= ~self.AUTO

            if self.flags & self.NEW_MANUAL:
                # Convert NEW_MANUAL to SAVED_MANUAL.
                if self.sha1:
                    set_ref('manual')
                    self.flags |= self.SAVED_MANUAL
                elif self.flags & self.SAVED_MANUAL:
                    # Delete any existing SAVED_MANUAL:
                    clear_ref('manual')
                    self.flags &= ~self.SAVED_MANUAL
                self.flags &= ~self.NEW_MANUAL

        elif self.flags & self.NEW_AUTO:
            # Convert NEW_AUTO to SAVED_AUTO.
            if self.sha1:
                set_ref('auto')
                self.flags |= self.SAVED_AUTO
            elif self.flags & self.SAVED_AUTO:
                # Delete any existing SAVED_AUTO:
                clear_ref('auto')
                self.flags &= ~self.SAVED_AUTO
            self.flags &= ~self.NEW_AUTO


class UnexpectedMergeFailure(Exception):
    def __init__(self, msg, i1, i2):
        Exception.__init__(self, msg)
        self.i1, self.i2 = i1, i2


class BlockCompleteError(Exception):
    pass


class FrontierBlockedError(Exception):
    def __init__(self, msg, i1, i2):
        Exception.__init__(self, msg)
        self.i1 = i1
        self.i2 = i2


class NotABlockingCommitError(Exception):
    pass


def find_frontier_blocks(block):
    """Iterate over the frontier blocks for the specified block.

    Use bisection to find the blocks. Iterate over the blocks starting
    in the bottom left and ending at the top right. Record in block
    any blockers that we find.

    We make the following assumptions (using Python subscript
    notation):

    0. All of the merges in block[1:,0] and block[0,1:] are
       already known.  (This is an invariant of the Block class.)

    1. If a direct merge can be done between block[i1-1,0] and
       block[0,i2-1], then all of the pairwise merges in
       block[1:i1, 1:i2] can also be done.

    2. If a direct merge fails between block[i1-1,0] and
       block[0,i2-1], then all of the pairwise merges in
       block[i1-1:,i2-1:] would also fail.

    Under these assumptions, the merge frontier is a stepstair
    pattern going from the bottom-left to the top-right, and
    bisection can be used to find the transition between mergeable
    and conflicting in any row or column.

    Of course these assumptions are not rigorously true, so the
    frontier blocks returned by this function are only an
    approximation. We check for and correct inconsistencies later.

    """

    # Given that these diagrams typically have few blocks, check
    # the end of a range first to see if the whole range can be
    # determined, and fall back to bisection otherwise.  We
    # determine the frontier block by block, starting in the lower
    # left.

    if block.len1 <= 1 or block.len2 <= 1 or block.is_blocked(1, 1):
        return

    if block.is_mergeable(block.len1 - 1, block.len2 - 1):
        # The whole block is mergable!
        yield block
        return

    if not block.is_mergeable(1, 1):
        # There are no mergeable blocks in block; therefore,
        # block[1,1] must itself be unmergeable.  Record that
        # fact:
        block[1, 1].record_blocked(True)
        return

    # At this point, we know that there is at least one mergeable
    # commit in the first column.  Find the height of the success
    # block in column 1:
    i1 = 1
    i2 = find_first_false(
        lambda i: block.is_mergeable(i1, i),
        2, block.len2,
        )

    # Now we know that (1,i2-1) is mergeable but (1,i2) is not;
    # e.g., (i1, i2) is like 'A' (or maybe 'B') in the following
    # diagram (where '*' means mergeable, 'x' means not mergeable,
    # and '?' means indeterminate) and that the merge under 'A' is
    # not mergeable:
    #
    #          i1
    #
    #        0123456
    #      0 *******
    #      1 **?????
    #  i2  2 **?????
    #      3 **?????
    #      4 *Axxxxx
    #      5 *xxxxxx
    #         B

    while True:
        if i2 == 1:
            break

        # At this point in the loop, we know that any blocks to
        # the left of 'A' have already been recorded, (i1, i2-1)
        # is mergeable but (i1, i2) is not; e.g., we are at a
        # place like 'A' in the following diagram:
        #
        #          i1
        #
        #        0123456
        #      0 **|****
        #      1 **|*???
        #  i2  2 **|*???
        #      3 **|Axxx
        #      4 --+xxxx
        #      5 *xxxxxx
        #
        # This implies that (i1, i2) is the first unmergeable
        # commit in a blocker block (though blocker blocks are not
        # recorded explicitly).  It also implies that a mergeable
        # block has its last mergeable commit somewhere in row
        # i2-1; find its width.
        if (
                i1 == block.len1 - 1
                or block.is_mergeable(block.len1 - 1, i2 - 1)
                ):
            yield block[:block.len1, :i2]
            break
        else:
            i1 = find_first_false(
                lambda i: block.is_mergeable(i, i2 - 1),
                i1 + 1, block.len1 - 1,
                )
            yield block[:i1, :i2]

        # At this point in the loop, (i1-1, i2-1) is mergeable but
        # (i1, i2-1) is not; e.g., 'A' in the following diagram:
        #
        #          i1
        #
        #        0123456
        #      0 **|*|**
        #      1 **|*|??
        #  i2  2 --+-+xx
        #      3 **|xxAx
        #      4 --+xxxx
        #      5 *xxxxxx
        #
        # The block ending at (i1-1,i2-1) has just been recorded.
        # Now find the height of the conflict rectangle at column
        # i1 and fill it in:
        if i2 - 1 == 1 or not block.is_mergeable(i1, 1):
            break
        else:
            i2 = find_first_false(
                lambda i: block.is_mergeable(i1, i),
                2, i2 - 1,
                )


def write_diagram_with_axes(f, diagram, tip1, tip2):
    """Write a diagram of one-space-wide characters to file-like object f.

    Include integers along the top and left sides showing the indexes
    corresponding to the rows and columns.

    """

    len1 = len(diagram)
    len2 = len(diagram[0])

    # Write the line of i1 numbers:
    f.write('   ')
    for i1 in range(0, len1, 5):
        f.write('%5d' % (i1,))

    if (len1 - 1) % 5 == 0:
        # The last multiple-of-five integer that we just wrote was
        # the index of the last column. We're done.
        f.write('\n')
    else:
        if (len1 - 1) % 5 == 1:
            # Add an extra space so that the numbers don't run together:
            f.write(' ')
        f.write('%s%d\n' % (' ' * ((len1 - 1) % 5 - 1), len1 - 1,))

    # Write a line of '|' marks under the numbers emitted above:
    f.write('   ')
    for i1 in range(0, len1, 5):
        f.write('%5s' % ('|',))

    if (len1 - 1) % 5 == 0:
        # The last multiple-of-five integer was at the last
        # column. We're done.
        f.write('\n')
    elif (len1 - 1) % 5 == 1:
        # Tilt the tick mark to account for the extra space:
        f.write(' /\n')
    else:
        f.write('%s|\n' % (' ' * ((len1 - 1) % 5 - 1),))

    # Write the actual body of the diagram:
    for i2 in range(len2):
        if i2 % 5 == 0 or i2 == len2 - 1:
            f.write('%4d - ' % (i2,))
        else:
            f.write('       ')

        for i1 in range(len1):
            f.write(diagram[i1][i2])

        if tip1 and i2 == 0:
            f.write(' - %s\n' % (tip1,))
        else:
            f.write('\n')

    if tip2:
        f.write('       |\n')
        f.write('     %s\n' % (tip2,))


class MergeFrontier(object):
    """The merge frontier within a Block, and a strategy for filling it.

    """

    # Additional codes used in the 2D array returned from create_diagram()
    FRONTIER_WITHIN = 0x10
    FRONTIER_RIGHT_EDGE = 0x20
    FRONTIER_BOTTOM_EDGE = 0x40
    FRONTIER_MASK = 0x70

    def __init__(self, block):
        self.block = block

    def __nonzero__(self):
        """Alias for __bool__."""

        return self.__bool__()

    @classmethod
    def default_formatter(cls, node, legend=None):
        def color(node, within):
            if within:
                return AnsiColor.B_GREEN + node + AnsiColor.END
            else:
                return AnsiColor.B_RED + node + AnsiColor.END

        if legend is None:
            legend = ['?', '*', '.', '#', '@', '-', '|', '+']
        merge = node & Block.MERGE_MASK
        within = merge == Block.MERGE_MANUAL or (node & cls.FRONTIER_WITHIN)
        skip = [Block.MERGE_MANUAL, Block.MERGE_BLOCKED, Block.MERGE_UNBLOCKED]
        if merge not in skip:
            vertex = (cls.FRONTIER_BOTTOM_EDGE | cls.FRONTIER_RIGHT_EDGE)
            edge_status = node & vertex
            if edge_status == vertex:
                return color(legend[-1], within)
            elif edge_status == cls.FRONTIER_RIGHT_EDGE:
                return color(legend[-2], within)
            elif edge_status == cls.FRONTIER_BOTTOM_EDGE:
                return color(legend[-3], within)
        return color(legend[merge], within)

    def create_diagram(self):
        """Generate a diagram of this frontier.

        The returned diagram is a nested list of integers forming a 2D
        array, representing the merge frontier embedded in the diagram
        of commits returned from Block.create_diagram().

        At each node in the returned diagram is an integer whose value
        is a bitwise-or of existing MERGE_* constant from
        Block.create_diagram() and possibly zero or more of the
        FRONTIER_* constants defined in this class.

        """

        return self.block.create_diagram()

    def format_diagram(self, formatter=None, diagram=None):
        if formatter is None:
            formatter = self.default_formatter
        if diagram is None:
            diagram = self.create_diagram()
        return [
            [formatter(diagram[i1][i2]) for i2 in range(self.block.len2)]
            for i1 in range(self.block.len1)]

    def write(self, f, tip1=None, tip2=None):
        """Write this frontier to file-like object f."""

        write_diagram_with_axes(f, self.format_diagram(), tip1, tip2)

    def write_html(self, f, name, cssfile='imerge.css', abbrev_sha1=7):
        class_map = {
            Block.MERGE_UNKNOWN: 'merge_unknown',
            Block.MERGE_MANUAL: 'merge_manual',
            Block.MERGE_AUTOMATIC: 'merge_automatic',
            Block.MERGE_BLOCKED: 'merge_blocked',
            Block.MERGE_UNBLOCKED: 'merge_unblocked',
            self.FRONTIER_WITHIN: 'frontier_within',
            self.FRONTIER_RIGHT_EDGE: 'frontier_right_edge',
            self.FRONTIER_BOTTOM_EDGE: 'frontier_bottom_edge',
            }

        def map_to_classes(i1, i2, node):
            merge = node & Block.MERGE_MASK
            ret = [class_map[merge]]
            for bit in [self.FRONTIER_WITHIN, self.FRONTIER_RIGHT_EDGE,
                        self.FRONTIER_BOTTOM_EDGE]:
                if node & bit:
                    ret.append(class_map[bit])
            if not (node & self.FRONTIER_WITHIN):
                ret.append('frontier_without')
            elif (node & Block.MERGE_MASK) == Block.MERGE_UNKNOWN:
                ret.append('merge_skipped')
            if i1 == 0 or i2 == 0:
                ret.append('merge_initial')
            if i1 == 0:
                ret.append('col_left')
            if i1 == self.block.len1 - 1:
                ret.append('col_right')
            if i2 == 0:
                ret.append('row_top')
            if i2 == self.block.len2 - 1:
                ret.append('row_bottom')
            return ret

        f.write("""\
<html>
<head>
<title>git-imerge: %s</title>
<link rel="stylesheet" href="%s" type="text/css" />
</head>
<body>
<table id="imerge">
""" % (name, cssfile))

        diagram = self.create_diagram()

        f.write('  <tr>\n')
        f.write('    <th class="indexes">&nbsp;</td>\n')
        for i1 in range(self.block.len1):
            f.write('    <th class="indexes">%d-*</td>\n' % (i1,))
        f.write('  </tr>\n')

        for i2 in range(self.block.len2):
            f.write('  <tr>\n')
            f.write('    <th class="indexes">*-%d</td>\n' % (i2,))
            for i1 in range(self.block.len1):
                classes = map_to_classes(i1, i2, diagram[i1][i2])
                record = self.block.get_value(i1, i2)
                sha1 = record.sha1 or ''
                td_id = record.sha1 and ' id="%s"' % (record.sha1) or ''
                td_class = classes and ' class="' + ' '.join(classes) + '"' or ''
                f.write('    <td%s%s>%.*s</td>\n' % (
                        td_id, td_class, abbrev_sha1, sha1))
            f.write('  </tr>\n')
        f.write('</table>\n</body>\n</html>\n')


class FullMergeFrontier(MergeFrontier):
    """A MergeFrontier that is to be filled completely.

    """

    @staticmethod
    def map_known_frontier(block):
        return FullMergeFrontier(block)

    def __bool__(self):
        """Return True iff this frontier contains any merges.

        """

        return (1, 1) in self.block

    def is_complete(self):
        """Return True iff the frontier covers the whole block."""

        return (self.block.len1 - 1, self.block.len2 - 1) in self.block

    def incorporate_merge(self, i1, i2):
        """Incorporate a successful merge at (i1, i2).

        Raise NotABlockingCommitError if that merge was not a blocker.

        """

        if not self.block[i1, i2].is_blocked():
            raise NotABlockingCommitError(
                'Commit %d-%d was not on the frontier.'
                % self.block.get_original_indexes(i1, i2)
                )
        else:
            self.block[i1, i2].record_blocked(False)

    def auto_expand(self):
        block = self.block
        len2 = block.len2

        blocker = None
        for i1 in range(1, block.len1):
            for i2 in range(1, len2):
                if (i1, i2) in block:
                    pass
                elif block.is_blocked(i1, i2):
                    if blocker is None:
                        blocker = (i1, i2)
                    len2 = i2
                    # Done with this row:
                    break
                elif block.auto_fill_micromerge(i1, i2):
                    # Merge successful
                    pass
                else:
                    block[i1, i2].record_blocked(True)
                    if blocker is None:
                        blocker = (i1, i2)
                    len2 = i2
                    # Done with this row:
                    break

        if blocker:
            i1orig, i2orig = self.block.get_original_indexes(*blocker)
            raise FrontierBlockedError(
                'Conflict; suggest manual merge of %d-%d' % (i1orig, i2orig),
                i1orig, i2orig,
                )
        else:
            raise BlockCompleteError('The block is already complete')


class ManualMergeFrontier(FullMergeFrontier):
    """A FullMergeFrontier that is to be filled completely by user merges.

    """

    @staticmethod
    def map_known_frontier(block):
        return ManualMergeFrontier(block)

    def auto_expand(self):
        block = self.block

        for i1 in range(1, block.len1):
            for i2 in range(1, block.len2):
                if (i1, i2) not in block:
                    i1orig, i2orig = block.get_original_indexes(i1, i2)
                    raise FrontierBlockedError(
                        'Manual merges requested; please merge %d-%d' % (i1orig, i2orig),
                        i1orig, i2orig
                        )

        raise BlockCompleteError('The block is already complete')


class BlockwiseMergeFrontier(MergeFrontier):
    """A MergeFrontier that is filled blockwise, using outlining.

    A BlockwiseMergeFrontier is represented by a list of SubBlocks,
    each of which is thought to be completely mergeable. The list is
    kept in normalized form:

    * Only non-empty blocks are retained

    * Blocks are sorted from bottom left to upper right

    * No redundant blocks

    """

    @staticmethod
    def map_known_frontier(block):
        """Return the object describing existing successful merges in block.

        The return value only includes the part that is fully outlined
        and whose outline consists of rectangles reaching back to
        (0,0).

        A blocked commit is *not* considered to be within the
        frontier, even if a merge is registered for it.  Such merges
        must be explicitly unblocked."""

        # FIXME: This algorithm can take combinatorial time, for
        # example if there is a big block of merges that is a dead
        # end:
        #
        #     +++++++
        #     +?+++++
        #     +?+++++
        #     +?+++++
        #     +?*++++
        #
        # The problem is that the algorithm will explore all of the
        # ways of getting to commit *, and the number of paths grows
        # like a binomial coefficient.  The solution would be to
        # remember dead-ends and reject any curves that visit a point
        # to the right of a dead-end.
        #
        # For now we don't intend to allow a situation like this to be
        # created, so we ignore the problem.

        # A list (i1, i2, down) of points in the path so far.  down is
        # True iff the attempted step following this one was
        # downwards.
        path = []

        def create_frontier(path):
            blocks = []
            for ((i1old, i2old, downold), (i1new, i2new, downnew)) in iter_neighbors(path):
                if downold is True and downnew is False:
                    blocks.append(block[:i1new + 1, :i2new + 1])
            return BlockwiseMergeFrontier(block, blocks)

        # Loop invariants:
        #
        # * path is a valid path
        #
        # * (i1, i2) is in block but it not yet added to path
        #
        # * down is True if a step downwards from (i1, i2) has not yet
        #   been attempted
        (i1, i2) = (block.len1 - 1, 0)
        down = True
        while True:
            if down:
                if i2 == block.len2 - 1:
                    # Hit edge of block; can't move down:
                    down = False
                elif (i1, i2 + 1) in block and not block.is_blocked(i1, i2 + 1):
                    # Can move down
                    path.append((i1, i2, True))
                    i2 += 1
                else:
                    # Can't move down.
                    down = False
            else:
                if i1 == 0:
                    # Success!
                    path.append((i1, i2, False))
                    return create_frontier(path)
                elif (i1 - 1, i2) in block and not block.is_blocked(i1 - 1, i2):
                    # Can move left
                    path.append((i1, i2, False))
                    down = True
                    i1 -= 1
                else:
                    # There's no way to go forward; backtrack until we
                    # find a place where we can still try going left:
                    while True:
                        try:
                            (i1, i2, down) = path.pop()
                        except IndexError:
                            # This shouldn't happen because, in the
                            # worst case, there is a valid path across
                            # the top edge of the merge diagram.
                            raise RuntimeError('Block is improperly formed!')
                        if down:
                            down = False
                            break

    @staticmethod
    def initiate_merge(block):
        """Return a BlockwiseMergeFrontier instance for block.

        Compute the blocks making up the boundary using bisection (see
        find_frontier_blocks() for more information). Outline the
        blocks, then return a BlockwiseMergeFrontier reflecting the
        final result.

        """

        top_level_frontier = BlockwiseMergeFrontier(
            block, list(find_frontier_blocks(block)),
            )

        # Now outline the mergeable blocks, backtracking if there are
        # any unexpected merge failures:

        frontier = top_level_frontier
        while frontier:
            subblock = next(iter(frontier))

            try:
                subblock.auto_outline()
            except UnexpectedMergeFailure as e:
                # One of the merges that we expected to succeed in
                # fact failed.
                frontier.remove_failure(e.i1, e.i2)

                if (e.i1, e.i2) == (1, 1):
                    # The failed merge was the first micromerge that we'd
                    # need for `best_block`, so record it as a blocker:
                    subblock[1, 1].record_blocked(True)

                if frontier is not top_level_frontier:
                    # Report that failure back to the top-level
                    # frontier, too (but first we have to translate
                    # the indexes):
                    (i1orig, i2orig) = subblock.get_original_indexes(e.i1, e.i2)
                    top_level_frontier.remove_failure(
                        *block.convert_original_indexes(i1orig, i2orig),
                        )
                # Restart loop for the same frontier...
            else:
                # We're only interested in subfrontiers that contain
                # mergeable subblocks:
                sub_frontiers = [f for f in frontier.partition(subblock) if f]
                if not sub_frontiers:
                    break

                # Since we just outlined the first (i.e., leftmost)
                # mergeable block in `frontier`,
                # `frontier.partition()` can at most have returned a
                # single non-empty value, namely one to the right of
                # `subblock`.
                [frontier] = sub_frontiers

        return top_level_frontier

    def __init__(self, block, blocks=None):
        MergeFrontier.__init__(self, block)
        self.blocks = self._normalized_blocks(blocks or [])

    def __iter__(self):
        """Iterate over blocks from bottom left to upper right."""

        return iter(self.blocks)

    def __bool__(self):
        """Return True iff this frontier contains any SubBlocks.

        Return True if this BlockwiseMergeFrontier contains any
        SubBlocks that are thought to be completely mergeable (whether
        they have been outlined or not).

        """

        return bool(self.blocks)

    def is_complete(self):
        """Return True iff the frontier covers the whole block."""

        return (
            len(self.blocks) == 1
            and self.blocks[0].len1 == self.block.len1
            and self.blocks[0].len2 == self.block.len2
            )

    @staticmethod
    def _normalized_blocks(blocks):
        """Return a normalized list of blocks from the argument.

        * Remove empty blocks.

        * Remove redundant blocks.

        * Sort the blocks according to their len1 members.

        """

        def contains(block1, block2):
            """Return true if block1 contains block2."""

            return block1.len1 >= block2.len1 and block1.len2 >= block2.len2

        blocks = sorted(blocks, key=lambda block: block.len1)
        ret = []

        for block in blocks:
            if block.len1 == 0 or block.len2 == 0:
                continue
            while True:
                if not ret:
                    ret.append(block)
                    break

                last = ret[-1]
                if contains(last, block):
                    break
                elif contains(block, last):
                    ret.pop()
                else:
                    ret.append(block)
                    break

        return ret

    def remove_failure(self, i1, i2):
        """Refine the merge frontier given that the specified merge fails."""

        newblocks = []
        shrunk_block = False

        for block in self.blocks:
            if i1 < block.len1 and i2 < block.len2:
                if i1 > 1:
                    newblocks.append(block[:i1, :])
                if i2 > 1:
                    newblocks.append(block[:, :i2])
                shrunk_block = True
            else:
                newblocks.append(block)

        if shrunk_block:
            self.blocks = self._normalized_blocks(newblocks)

    def partition(self, block):
        """Iterate over the BlockwiseMergeFrontiers partitioned by block.

        Iterate over the zero, one, or two BlockwiseMergeFrontiers to
        the left and/or right of block.

        block must be contained in this frontier and already be
        outlined.

        """

        # Remember that the new blocks have to include the outlined
        # edge of the partitioning block to satisfy the invariant that
        # the left and upper edge of a block has to be known.

        left = []
        right = []
        for b in self.blocks:
            if b.len1 == block.len1 and b.len2 == block.len2:
                # That's the block we're partitioning on; just skip it.
                pass
            elif b.len1 < block.len1 and b.len2 > block.len2:
                left.append(b[:, block.len2 - 1:])
            elif b.len1 > block.len1 and b.len2 < block.len2:
                right.append(b[block.len1 - 1:, :])
            else:
                raise ValueError(
                    'BlockwiseMergeFrontier partitioned with inappropriate block'
                    )

        if block.len2 < self.block.len2:
            yield BlockwiseMergeFrontier(self.block[:block.len1, block.len2 - 1:], left)

        if block.len1 < self.block.len1:
            yield BlockwiseMergeFrontier(self.block[block.len1 - 1:, :block.len2], right)

    def iter_boundary_blocks(self):
        """Iterate over the complete blocks that form this block's boundary.

        Iterate over them from bottom left to top right. This is like
        self.blocks, except that it also includes the implicit blocks
        at self.block[0, :] and self.blocks[:, 0] if they are needed
        to complete the boundary.

        """

        if not self or self.blocks[0].len2 < self.block.len2:
            yield self.block[0, :]
        for block in self:
            yield block
        if not self or self.blocks[-1].len1 < self.block.len1:
            yield self.block[:, 0]

    def iter_blocker_blocks(self):
        """Iterate over the blocks on the far side of this frontier.

        This must only be called for an outlined frontier."""

        for block1, block2 in iter_neighbors(self.iter_boundary_blocks()):
            yield self.block[block1.len1 - 1:block2.len1, block2.len2 - 1: block1.len2]

    def get_affected_blocker_block(self, i1, i2):
        """Return the blocker block that a successful merge (i1,i2) would unblock.

        If there is no such block, raise NotABlockingCommitError."""

        for block in self.iter_blocker_blocks():
            try:
                (block_i1, block_i2) = block.convert_original_indexes(i1, i2)
            except IndexError:
                pass
            else:
                if (block_i1, block_i2) == (1, 1):
                    # That's the one we need to improve this block:
                    return block
                else:
                    # An index pair can only be in a single blocker
                    # block, which we've already found:
                    raise NotABlockingCommitError(
                        'Commit %d-%d was not blocking the frontier.'
                        % self.block.get_original_indexes(i1, i2)
                        )
        else:
            raise NotABlockingCommitError(
                'Commit %d-%d was not on the frontier.'
                % self.block.get_original_indexes(i1, i2)
                )

    def incorporate_merge(self, i1, i2):
        """Incorporate a successful merge at (i1, i2).

        Raise NotABlockingCommitError if that merge was not a blocker.

        """

        unblocked_block = self.get_affected_blocker_block(i1, i2)
        unblocked_block[1, 1].record_blocked(False)

    def auto_expand(self):
        """Try pushing out one of the blocks on this frontier.

        Raise BlockCompleteError if the whole block has already been
        solved.  Raise FrontierBlockedError if the frontier is blocked
        everywhere.  This method does *not* update self; if it returns
        successfully you should recompute the frontier from
        scratch."""

        blocks = list(self.iter_blocker_blocks())
        if not blocks:
            raise BlockCompleteError('The block is already complete')

        # Try blocks from left to right:
        blocks.sort(key=lambda block: block.get_original_indexes(0, 0))

        for block in blocks:
            merge_frontier = BlockwiseMergeFrontier.initiate_merge(block)
            if bool(merge_frontier):
                return
        else:
            # None of the blocks could be expanded.  Suggest that the
            # caller do a manual merge of the commit that is blocking
            # the leftmost blocker block.
            i1, i2 = blocks[0].get_original_indexes(1, 1)
            raise FrontierBlockedError(
                'Conflict; suggest manual merge of %d-%d' % (i1, i2),
                i1, i2
                )

    def create_diagram(self):
        """Generate a diagram of this frontier.

        This method adds FRONTIER_* bits to the diagram generated by
        the super method.

        """

        diagram = MergeFrontier.create_diagram(self)

        try:
            next_block = self.blocks[0]
        except IndexError:
            next_block = None

        diagram[0][-1] |= self.FRONTIER_BOTTOM_EDGE
        for i2 in range(1, self.block.len2):
            if next_block is None or i2 >= next_block.len2:
                diagram[0][i2] |= self.FRONTIER_RIGHT_EDGE

        prev_block = None
        for n in range(len(self.blocks)):
            block = self.blocks[n]
            try:
                next_block = self.blocks[n + 1]
            except IndexError:
                next_block = None

            for i1 in range(block.len1):
                for i2 in range(block.len2):
                    v = self.FRONTIER_WITHIN
                    if i1 == block.len1 - 1 and (
                            next_block is None or i2 >= next_block.len2
                            ):
                        v |= self.FRONTIER_RIGHT_EDGE
                    if i2 == block.len2 - 1 and (
                            prev_block is None or i1 >= prev_block.len1
                            ):
                        v |= self.FRONTIER_BOTTOM_EDGE
                    diagram[i1][i2] |= v
            prev_block = block

        try:
            prev_block = self.blocks[-1]
        except IndexError:
            prev_block = None

        for i1 in range(1, self.block.len1):
            if prev_block is None or i1 >= prev_block.len1:
                diagram[i1][0] |= self.FRONTIER_BOTTOM_EDGE
        diagram[-1][0] |= self.FRONTIER_RIGHT_EDGE

        return diagram


class NoManualMergeError(Exception):
    pass


class ManualMergeUnusableError(Exception):
    def __init__(self, msg, commit):
        Exception.__init__(self, 'Commit %s is not usable; %s' % (commit, msg))
        self.commit = commit


class CommitNotFoundError(Exception):
    def __init__(self, commit):
        Exception.__init__(
            self,
            'Commit %s was not found among the known merge commits' % (commit,),
            )
        self.commit = commit


class Block(object):
    """A rectangular range of commits, indexed by (i1,i2).

    The commits block[0,1:] and block[1:,0] are always all known.
    block[0,0] may or may not be known; it is usually unneeded (except
    maybe implicitly).

    Members:

        name -- the name of the imerge of which this block is part.

        len1, len2 -- the dimensions of the block.

    """

    def __init__(self, git, name, len1, len2):
        self.git = git
        self.name = name
        self.len1 = len1
        self.len2 = len2

    def get_merge_state(self):
        """Return the MergeState instance containing this Block."""

        raise NotImplementedError()

    def get_area(self):
        """Return the area of this block, ignoring the known edges."""

        return (self.len1 - 1) * (self.len2 - 1)

    def _check_indexes(self, i1, i2):
        if not (0 <= i1 < self.len1):
            raise IndexError('first index (%s) is out of range 0:%d' % (i1, self.len1,))
        if not (0 <= i2 < self.len2):
            raise IndexError('second index (%s) is out of range 0:%d' % (i2, self.len2,))

    def _normalize_indexes(self, index):
        """Return a pair of non-negative integers (i1, i2)."""

        try:
            (i1, i2) = index
        except TypeError:
            raise IndexError('Block indexing requires exactly two indexes')

        if i1 < 0:
            i1 += self.len1
        if i2 < 0:
            i2 += self.len2

        self._check_indexes(i1, i2)
        return (i1, i2)

    def get_original_indexes(self, i1, i2):
        """Return the original indexes corresponding to (i1,i2) in this block.

        This function supports negative indexes."""

        return self._normalize_indexes((i1, i2))

    def convert_original_indexes(self, i1, i2):
        """Return the indexes in this block corresponding to original indexes (i1,i2).

        raise IndexError if they are not within this block.  This
        method does not support negative indices."""

        return (i1, i2)

    def _set_value(self, i1, i2, value):
        """Set the MergeRecord for integer indexes (i1, i2).

        i1 and i2 must be non-negative."""

        raise NotImplementedError()

    def get_value(self, i1, i2):
        """Return the MergeRecord for integer indexes (i1, i2).

        i1 and i2 must be non-negative."""

        raise NotImplementedError()

    def __getitem__(self, index):
        """Return the MergeRecord at (i1, i2) (requires two indexes).

        If i1 and i2 are integers but the merge is unknown, return
        None.  If either index is a slice, return a SubBlock."""

        try:
            (i1, i2) = index
        except TypeError:
            raise IndexError('Block indexing requires exactly two indexes')
        if isinstance(i1, slice) or isinstance(i2, slice):
            return SubBlock(self, i1, i2)
        else:
            return self.get_value(*self._normalize_indexes((i1, i2)))

    def __contains__(self, index):
        return self[index].is_known()

    def is_blocked(self, i1, i2):
        """Return True iff the specified commit is blocked."""

        (i1, i2) = self._normalize_indexes((i1, i2))
        return self[i1, i2].is_blocked()

    def is_mergeable(self, i1, i2):
        """Determine whether (i1,i2) can be merged automatically.

        If we already have a merge record for (i1,i2), return True.
        Otherwise, attempt a merge (discarding the result)."""

        (i1, i2) = self._normalize_indexes((i1, i2))
        if (i1, i2) in self:
            return True
        else:
            sys.stderr.write(
                'Attempting automerge of %d-%d...' % self.get_original_indexes(i1, i2)
                )
            try:
                self.git.automerge(self[i1, 0].sha1, self[0, i2].sha1)
            except AutomaticMergeFailed:
                sys.stderr.write('failure.\n')
                return False
            else:
                sys.stderr.write('success.\n')
                return True

    def auto_outline(self):
        """Complete the outline of this Block.

        raise UnexpectedMergeFailure if automerging fails."""

        # Check that all of the merges go through before recording any
        # of them permanently.
        merges = []

        def do_merge(i1, commit1, i2, commit2, msg='Autofilling %d-%d...', record=True):
            if (i1, i2) in self:
                return self[i1, i2].sha1
            (i1orig, i2orig) = self.get_original_indexes(i1, i2)
            sys.stderr.write(msg % (i1orig, i2orig))
            logmsg = 'imerge \'%s\': automatic merge %d-%d' % (self.name, i1orig, i2orig)
            try:
                merge = self.git.automerge(commit1, commit2, msg=logmsg)
            except AutomaticMergeFailed as e:
                sys.stderr.write('unexpected conflict.  Backtracking...\n')
                raise UnexpectedMergeFailure(str(e), i1, i2)
            else:
                sys.stderr.write('success.\n')

            if record:
                merges.append((i1, i2, merge))
            return merge

        i2 = self.len2 - 1
        left = self[0, i2].sha1
        for i1 in range(1, self.len1 - 1):
            left = do_merge(i1, self[i1, 0].sha1, i2, left)

        i1 = self.len1 - 1
        above = self[i1, 0].sha1
        for i2 in range(1, self.len2 - 1):
            above = do_merge(i1, above, i2, self[0, i2].sha1)

        i1, i2 = self.len1 - 1, self.len2 - 1
        if i1 > 1 and i2 > 1:
            # We will compare two ways of doing the final "vertex" merge:
            # as a continuation of the bottom edge, or as a continuation
            # of the right edge.  We only accept it if both approaches
            # succeed and give identical trees.
            vertex_v1 = do_merge(
                i1, self[i1, 0].sha1, i2, left,
                msg='Autofilling %d-%d (first way)...',
                record=False,
                )
            vertex_v2 = do_merge(
                i1, above, i2, self[0, i2].sha1,
                msg='Autofilling %d-%d (second way)...',
                record=False,
                )
            if self.git.get_tree(vertex_v1) == self.git.get_tree(vertex_v2):
                sys.stderr.write(
                    'The two ways of autofilling %d-%d agree.\n'
                    % self.get_original_indexes(i1, i2)
                    )
                # Everything is OK.  Now reparent the actual vertex merge to
                # have above and left as its parents:
                merges.append(
                    (i1, i2, self.git.reparent(vertex_v1, [above, left]))
                    )
            else:
                sys.stderr.write(
                    'The two ways of autofilling %d-%d do not agree.  Backtracking...\n'
                    % self.get_original_indexes(i1, i2)
                    )
                raise UnexpectedMergeFailure('Inconsistent vertex merges', i1, i2)
        else:
            do_merge(
                i1, above, i2, left,
                msg='Autofilling %d-%d...',
                )

        # Done!  Now we can record the results:
        sys.stderr.write('Recording autofilled block %s.\n' % (self,))
        for (i1, i2, merge) in merges:
            self[i1, i2].record_merge(merge, MergeRecord.NEW_AUTO)

    def auto_fill_micromerge(self, i1=1, i2=1):
        """Try to fill micromerge (i1, i2) in this block (default (1, 1)).

        Return True iff the attempt was successful."""

        assert (i1, i2) not in self
        assert (i1 - 1, i2) in self
        assert (i1, i2 - 1) in self
        if self.len1 <= i1 or self.len2 <= i2 or self.is_blocked(i1, i2):
            return False

        (i1orig, i2orig) = self.get_original_indexes(i1, i2)
        sys.stderr.write('Attempting to merge %d-%d...' % (i1orig, i2orig))
        logmsg = 'imerge \'%s\': automatic merge %d-%d' % (self.name, i1orig, i2orig)
        try:
            merge = self.git.automerge(
                self[i1, i2 - 1].sha1,
                self[i1 - 1, i2].sha1,
                msg=logmsg,
                )
        except AutomaticMergeFailed:
            sys.stderr.write('conflict.\n')
            self[i1, i2].record_blocked(True)
            return False
        else:
            sys.stderr.write('success.\n')
            self[i1, i2].record_merge(merge, MergeRecord.NEW_AUTO)
            return True

    # The codes in the 2D array returned from create_diagram()
    MERGE_UNKNOWN = 0
    MERGE_MANUAL = 1
    MERGE_AUTOMATIC = 2
    MERGE_BLOCKED = 3
    MERGE_UNBLOCKED = 4
    MERGE_MASK = 7

    # A map {(is_known(), manual, is_blocked()) : integer constant}
    MergeState = {
        (False, False, False): MERGE_UNKNOWN,
        (False, False, True): MERGE_BLOCKED,
        (True, False, True): MERGE_UNBLOCKED,
        (True, True, True): MERGE_UNBLOCKED,
        (True, False, False): MERGE_AUTOMATIC,
        (True, True, False): MERGE_MANUAL,
        }

    def create_diagram(self):
        """Generate a diagram of this Block.

        The returned diagram, is a nested list of integers forming a 2D array,
        where the integer at diagram[i1][i2] is one of MERGE_UNKNOWN,
        MERGE_MANUAL, MERGE_AUTOMATIC, MERGE_BLOCKED, or MERGE_UNBLOCKED,
        representing the state of the commit at (i1, i2)."""

        diagram = [[None for i2 in range(self.len2)] for i1 in range(self.len1)]

        for i2 in range(self.len2):
            for i1 in range(self.len1):
                rec = self.get_value(i1, i2)
                c = self.MergeState[
                    rec.is_known(), rec.is_manual(), rec.is_blocked()]
                diagram[i1][i2] = c

        return diagram

    def format_diagram(self, legend=None, diagram=None):
        if legend is None:
            legend = [
                AnsiColor.D_GRAY + '?' + AnsiColor.END,
                AnsiColor.B_GREEN + '*' + AnsiColor.END,
                AnsiColor.B_GREEN + '.' + AnsiColor.END,
                AnsiColor.B_RED + '#' + AnsiColor.END,
                AnsiColor.B_YELLOW + '@' + AnsiColor.END,
                ]
        if diagram is None:
            diagram = self.create_diagram()
        return [
            [legend[diagram[i1][i2]] for i2 in range(self.len2)]
            for i1 in range(self.len1)]

    def write(self, f, tip1='', tip2=''):
        write_diagram_with_axes(f, self.format_diagram(), tip1, tip2)

    def writeppm(self, f):
        legend = ['127 127 0', '0 255 0', '0 127 0', '255 0 0', '127 0 0']
        diagram = self.format_diagram(legend)

        f.write('P3\n')
        f.write('%d %d 255\n' % (self.len1, self.len2,))
        for i2 in range(self.len2):
            f.write('  '.join(diagram[i1][i2] for i1 in range(self.len1)) + '\n')


class SubBlock(Block):
    @staticmethod
    def _convert_to_slice(i, len):
        """Return (start, len) for the specified index.

        i may be an integer or a slice with step equal to 1."""

        if isinstance(i, int):
            if i < 0:
                i += len
            i = slice(i, i + 1)
        elif isinstance(i, slice):
            if i.step is not None and i.step != 1:
                raise ValueError('Index has a non-zero step size')
        else:
            raise ValueError('Index cannot be converted to a slice')

        (start, stop, step) = i.indices(len)
        return (start, stop - start)

    def __init__(self, block, slice1, slice2):
        (start1, len1) = self._convert_to_slice(slice1, block.len1)
        (start2, len2) = self._convert_to_slice(slice2, block.len2)
        Block.__init__(self, block.git, block.name, len1, len2)
        if isinstance(block, SubBlock):
            # Peel away one level of indirection:
            self._merge_state = block._merge_state
            self._start1 = start1 + block._start1
            self._start2 = start2 + block._start2
        else:
            assert(isinstance(block, MergeState))
            self._merge_state = block
            self._start1 = start1
            self._start2 = start2

    def get_merge_state(self):
        return self._merge_state

    def get_original_indexes(self, i1, i2):
        i1, i2 = self._normalize_indexes((i1, i2))
        return self._merge_state.get_original_indexes(
            i1 + self._start1,
            i2 + self._start2,
            )

    def convert_original_indexes(self, i1, i2):
        (i1, i2) = self._merge_state.convert_original_indexes(i1, i2)
        if not (
                self._start1 <= i1 < self._start1 + self.len1
                and self._start2 <= i2 < self._start2 + self.len2
                ):
            raise IndexError('Indexes are not within block')
        return (i1 - self._start1, i2 - self._start2)

    def _set_value(self, i1, i2, sha1, flags):
        self._check_indexes(i1, i2)
        self._merge_state._set_value(
            i1 + self._start1,
            i2 + self._start2,
            sha1, flags,
            )

    def get_value(self, i1, i2):
        self._check_indexes(i1, i2)
        return self._merge_state.get_value(i1 + self._start1, i2 + self._start2)

    def __str__(self):
        return '%s[%d:%d,%d:%d]' % (
            self._merge_state,
            self._start1, self._start1 + self.len1,
            self._start2, self._start2 + self.len2,
            )


class MissingMergeFailure(Failure):
    def __init__(self, i1, i2):
        Failure.__init__(self, 'Merge %d-%d is not yet done' % (i1, i2))
        self.i1 = i1
        self.i2 = i2


class MergeState(Block):
    SOURCE_TABLE = {
        'auto': MergeRecord.SAVED_AUTO,
        'manual': MergeRecord.SAVED_MANUAL,
        }

    @staticmethod
    def get_scratch_refname(name):
        return 'refs/heads/imerge/%s' % (name,)

    @staticmethod
    def _check_no_merges(git, commits):
        multiparent_commits = [
            commit
            for commit in commits
            if len(git.get_commit_parents(commit)) > 1
            ]
        if multiparent_commits:
            raise Failure(
                'The following commits on the to-be-rebased branch are merge commits:\n'
                '    %s\n'
                '--goal=\'rebase\' is not yet supported for branches that include merges.\n'
                % ('\n    '.join(multiparent_commits),)
                )

    @staticmethod
    def initialize(
            git, name, merge_base,
            tip1, commits1,
            tip2, commits2,
            goal=DEFAULT_GOAL, goalopts=None,
            manual=False, branch=None,
            ):
        """Create and return a new MergeState object."""

        git.verify_imerge_name_available(name)
        if branch:
            git.check_branch_name_format(branch)
        else:
            branch = name

        if goal == 'rebase':
            MergeState._check_no_merges(git, commits2)

        return MergeState(
            git, name, merge_base,
            tip1, commits1,
            tip2, commits2,
            MergeRecord.NEW_MANUAL,
            goal=goal, goalopts=goalopts,
            manual=manual,
            branch=branch,
            )

    @staticmethod
    def read(git, name):
        (state, merges) = git.read_imerge_state(name)

        # Translate sources from strings into MergeRecord constants
        # SAVED_AUTO or SAVED_MANUAL:
        merges = dict((
            ((i1, i2), (sha1, MergeState.SOURCE_TABLE[source]))
            for ((i1, i2), (sha1, source)) in merges.items()
            ))

        blockers = state.get('blockers', [])

        # Find merge_base, commits1, and commits2:
        (merge_base, source) = merges.pop((0, 0))
        if source != MergeRecord.SAVED_MANUAL:
            raise Failure('Merge base should be manual!')
        commits1 = []
        for i1 in itertools.count(1):
            try:
                (sha1, source) = merges.pop((i1, 0))
                if source != MergeRecord.SAVED_MANUAL:
                    raise Failure('Merge %d-0 should be manual!' % (i1,))
                commits1.append(sha1)
            except KeyError:
                break

        commits2 = []
        for i2 in itertools.count(1):
            try:
                (sha1, source) = merges.pop((0, i2))
                if source != MergeRecord.SAVED_MANUAL:
                    raise Failure('Merge (0,%d) should be manual!' % (i2,))
                commits2.append(sha1)
            except KeyError:
                break

        tip1 = state.get('tip1', commits1[-1])
        tip2 = state.get('tip2', commits2[-1])

        goal = state['goal']
        if goal not in ALLOWED_GOALS:
            raise Failure('Goal %r, read from state, is not recognized.' % (goal,))

        goalopts = state['goalopts']

        manual = state['manual']
        branch = state.get('branch', name)

        state = MergeState(
            git, name, merge_base,
            tip1, commits1,
            tip2, commits2,
            MergeRecord.SAVED_MANUAL,
            goal=goal, goalopts=goalopts,
            manual=manual,
            branch=branch,
            )

        # Now write the rest of the merges to state:
        for ((i1, i2), (sha1, source)) in merges.items():
            if i1 == 0 and i2 >= state.len2:
                raise Failure('Merge 0-%d is missing!' % (state.len2,))
            if i1 >= state.len1 and i2 == 0:
                raise Failure('Merge %d-0 is missing!' % (state.len1,))
            if i1 >= state.len1 or i2 >= state.len2:
                raise Failure(
                    'Merge %d-%d is out of range [0:%d,0:%d]'
                    % (i1, i2, state.len1, state.len2)
                    )
            state[i1, i2].record_merge(sha1, source)

        # Record any blockers:
        for (i1, i2) in blockers:
            state[i1, i2].record_blocked(True)

        return state

    @staticmethod
    def remove(git, name):
        # If HEAD is the scratch refname, abort any in-progress
        # commits and detach HEAD:
        scratch_refname = MergeState.get_scratch_refname(name)
        if git.get_head_refname() == scratch_refname:
            try:
                git.abort_merge()
            except CalledProcessError:
                pass
            # Detach head so that we can delete scratch_refname:
            git.detach('Detach HEAD from %s' % (scratch_refname,))

        # Delete the scratch refname:
        git.delete_ref(
            scratch_refname, 'imerge %s: remove scratch reference' % (name,),
            )

        # Remove any references referring to intermediate merges:
        git.delete_imerge_refs(name)

        # If this merge was the default, unset the default:
        if git.get_default_imerge_name() == name:
            git.set_default_imerge_name(None)

    def __init__(
            self, git, name, merge_base,
            tip1, commits1,
            tip2, commits2,
            source,
            goal=DEFAULT_GOAL, goalopts=None,
            manual=False,
            branch=None,
            ):
        Block.__init__(self, git, name, len(commits1) + 1, len(commits2) + 1)
        self.tip1 = tip1
        self.tip2 = tip2
        self.goal = goal
        self.goalopts = goalopts
        self.manual = bool(manual)
        self.branch = branch or name

        # A simulated 2D array.  Values are None or MergeRecord instances.
        self._data = [[None] * self.len2 for i1 in range(self.len1)]

        self.get_value(0, 0).record_merge(merge_base, source)
        for (i1, commit) in enumerate(commits1, 1):
            self.get_value(i1, 0).record_merge(commit, source)
        for (i2, commit) in enumerate(commits2, 1):
            self.get_value(0, i2).record_merge(commit, source)

    def get_merge_state(self):
        return self

    def set_goal(self, goal):
        if goal not in ALLOWED_GOALS:
            raise ValueError('%r is not an allowed goal' % (goal,))

        if goal == 'rebase':
            self._check_no_merges(
                self.git,
                [self[0, i2].sha1 for i2 in range(1, self.len2)],
                )

        self.goal = goal

    def _set_value(self, i1, i2, value):
        self._data[i1][i2] = value

    def get_value(self, i1, i2):
        value = self._data[i1][i2]
        # Missing values spring to life on first access:
        if value is None:
            value = MergeRecord()
            self._data[i1][i2] = value
        return value

    def __contains__(self, index):
        # Avoid creating new MergeRecord objects here.
        (i1, i2) = self._normalize_indexes(index)
        value = self._data[i1][i2]
        return (value is not None) and value.is_known()

    def map_frontier(self):
        """Return a MergeFrontier instance describing the current frontier.

        """

        if self.manual:
            return ManualMergeFrontier.map_known_frontier(self)
        elif self.goal == 'full':
            return FullMergeFrontier.map_known_frontier(self)
        else:
            return BlockwiseMergeFrontier.map_known_frontier(self)

    def auto_complete_frontier(self):
        """Complete the frontier using automerges.

        If progress is blocked before the frontier is complete, raise
        a FrontierBlockedError.  Save the state as progress is
        made."""

        progress_made = False
        try:
            while True:
                frontier = self.map_frontier()
                try:
                    frontier.auto_expand()
                finally:
                    self.save()
                progress_made = True
        except BlockCompleteError:
            return
        except FrontierBlockedError as e:
            if not progress_made:
                # Adjust the error message:
                raise FrontierBlockedError(
                    'No progress was possible; suggest manual merge of %d-%d'
                    % (e.i1, e.i2),
                    e.i1, e.i2,
                    )
            else:
                raise

    def find_index(self, commit):
        """Return (i1,i2) for the specified commit.

        Raise CommitNotFoundError if it is not known."""

        for i2 in range(0, self.len2):
            for i1 in range(0, self.len1):
                if (i1, i2) in self:
                    record = self[i1, i2]
                    if record.sha1 == commit:
                        return (i1, i2)
        raise CommitNotFoundError(commit)

    def request_user_merge(self, i1, i2):
        """Prepare the working tree for the user to do a manual merge.

        It is assumed that the merges above and to the left of (i1, i2)
        are already done."""

        above = self[i1, i2 - 1]
        left = self[i1 - 1, i2]
        if not above.is_known() or not left.is_known():
            raise RuntimeError('The parents of merge %d-%d are not ready' % (i1, i2))
        refname = MergeState.get_scratch_refname(self.name)
        self.git.update_ref(
            refname, above.sha1,
            'imerge %r: Prepare merge %d-%d' % (self.name, i1, i2,),
            )
        self.git.checkout(refname)
        logmsg = 'imerge \'%s\': manual merge %d-%d' % (self.name, i1, i2)
        try:
            self.git.manualmerge(left.sha1, logmsg)
        except CalledProcessError:
            # We expect an error (otherwise we would have automerged!)
            pass
        sys.stderr.write(
            '\n'
            'Original first commit:\n'
            )
        self.git.summarize_commit(self[i1, 0].sha1)
        sys.stderr.write(
            '\n'
            'Original second commit:\n'
            )
        self.git.summarize_commit(self[0, i2].sha1)
        sys.stderr.write(
            '\n'
            'There was a conflict merging commit %d-%d, shown above.\n'
            'Please resolve the conflict, commit the result, then type\n'
            '\n'
            '    git-imerge continue\n'
            % (i1, i2)
            )

    def incorporate_manual_merge(self, commit):
        """Record commit as a manual merge of its parents.

        Return the indexes (i1,i2) where it was recorded.  If the
        commit is not usable for some reason, raise
        ManualMergeUnusableError."""

        parents = self.git.get_commit_parents(commit)
        if len(parents) < 2:
            raise ManualMergeUnusableError('it is not a merge', commit)
        if len(parents) > 2:
            raise ManualMergeUnusableError('it is an octopus merge', commit)
        # Find the parents among our contents...
        try:
            (i1first, i2first) = self.find_index(parents[0])
            (i1second, i2second) = self.find_index(parents[1])
        except CommitNotFoundError:
            raise ManualMergeUnusableError(
                'its parents are not known merge commits', commit,
                )
        swapped = False
        if i1first < i1second:
            # Swap parents to make the parent from above the first parent:
            (i1first, i2first, i1second, i2second) = (i1second, i2second, i1first, i2first)
            swapped = True
        if i1first != i1second + 1 or i2first != i2second - 1:
            raise ManualMergeUnusableError(
                'it is not a pairwise merge of adjacent parents', commit,
                )
        if swapped:
            # Create a new merge with the parents in the conventional order:
            commit = self.git.reparent(commit, [parents[1], parents[0]])

        i1, i2 = i1first, i2second
        self[i1, i2].record_merge(commit, MergeRecord.NEW_MANUAL)
        return (i1, i2)

    def incorporate_user_merge(self, edit_log_msg=None):
        """If the user has done a merge for us, incorporate the results.

        If the scratch reference refs/heads/imerge/NAME exists and is
        checked out, first check if there are staged changes that can
        be committed. Then try to incorporate the current commit into
        this MergeState, delete the reference, and return (i1,i2)
        corresponding to the merge. If the scratch reference does not
        exist, raise NoManualMergeError(). If the scratch reference
        exists but cannot be used, raise a ManualMergeUnusableError.
        If there are unstaged changes in the working tree, emit an
        error message and raise UncleanWorkTreeError.

        """

        refname = MergeState.get_scratch_refname(self.name)

        try:
            commit = self.git.get_commit_sha1(refname)
        except ValueError:
            raise NoManualMergeError('Reference %s does not exist.' % (refname,))

        head_name = self.git.get_head_refname()
        if head_name is None:
            raise NoManualMergeError('HEAD is currently detached.')
        elif head_name != refname:
            # This should not usually happen.  The scratch reference
            # exists, but it is not current.  Perhaps the user gave up on
            # an attempted merge then switched to another branch.  We want
            # to delete refname, but only if it doesn't contain any
            # content that we don't already know.
            try:
                self.find_index(commit)
            except CommitNotFoundError:
                # It points to a commit that we don't have in our records.
                raise Failure(
                    'The scratch reference, %(refname)s, already exists but is not\n'
                    'checked out.  If it points to a merge commit that you would like\n'
                    'to use, please check it out using\n'
                    '\n'
                    '    git checkout %(refname)s\n'
                    '\n'
                    'and then try to continue again.  If it points to a commit that is\n'
                    'unneeded, then please delete the reference using\n'
                    '\n'
                    '    git update-ref -d %(refname)s\n'
                    '\n'
                    'and then continue.'
                    % dict(refname=refname)
                    )
            else:
                # It points to a commit that is already recorded.  We can
                # delete it without losing any information.
                self.git.delete_ref(
                    refname,
                    'imerge %r: Remove obsolete scratch reference' % (self.name,),
                    )
                sys.stderr.write(
                    '%s did not point to a new merge; it has been deleted.\n'
                    % (refname,)
                    )
                raise NoManualMergeError(
                    'Reference %s was not checked out.' % (refname,)
                    )

        # If we reach this point, then the scratch reference exists and is
        # checked out.  Now check whether there is staged content that
        # can be committed:
        if self.git.commit_user_merge(edit_log_msg=edit_log_msg):
            commit = self.git.get_commit_sha1('HEAD')

        self.git.require_clean_work_tree('proceed')

        # This might throw ManualMergeUnusableError:
        (i1, i2) = self.incorporate_manual_merge(commit)

        # Now detach head so that we can delete refname.
        self.git.detach('Detach HEAD from %s' % (refname,))

        self.git.delete_ref(
            refname, 'imerge %s: remove scratch reference' % (self.name,),
            )

        merge_frontier = self.map_frontier()
        try:
            # This might throw NotABlockingCommitError:
            merge_frontier.incorporate_merge(i1, i2)
            sys.stderr.write(
                'Merge has been recorded for merge %d-%d.\n'
                % self.get_original_indexes(i1, i2)
                )
        finally:
            self.save()

    def _set_refname(self, refname, commit, force=False):
        try:
            ref_oldval = self.git.get_commit_sha1(refname)
        except ValueError:
            # refname doesn't already exist; simply point it at commit:
            self.git.update_ref(refname, commit, 'imerge: recording final merge')
            self.git.checkout(refname, quiet=True)
        else:
            # refname already exists.  This has two ramifications:
            # 1. HEAD might point at it
            # 2. We may only fast-forward it (unless force is set)
            head_refname = self.git.get_head_refname()

            if not force and not self.git.is_ancestor(ref_oldval, commit):
                raise Failure(
                    '%s cannot be fast-forwarded to %s!' % (refname, commit)
                    )

            if head_refname == refname:
                self.git.reset_hard(commit)
            else:
                self.git.update_ref(
                    refname, commit, 'imerge: recording final merge',
                    )
                self.git.checkout(refname, quiet=True)

    def simplify_to_full(self, refname, force=False):
        for i1 in range(1, self.len1):
            for i2 in range(1, self.len2):
                if not (i1, i2) in self:
                    raise Failure(
                        'Cannot simplify to "full" because '
                        'merge %d-%d is not yet done'
                        % (i1, i2)
                        )

        self._set_refname(refname, self[-1, -1].sha1, force=force)

    def simplify_to_rebase_with_history(self, refname, force=False):
        i1 = self.len1 - 1
        for i2 in range(1, self.len2):
            if not (i1, i2) in self:
                raise Failure(
                    'Cannot simplify to rebase-with-history because '
                    'merge %d-%d is not yet done'
                    % (i1, i2)
                    )

        commit = self[i1, 0].sha1
        for i2 in range(1, self.len2):
            orig = self[0, i2].sha1
            tree = self.git.get_tree(self[i1, i2].sha1)

            # Create a commit, copying the old log message:
            msg = (
                self.git.get_log_message(orig).rstrip('\n')
                + '\n\n(rebased-with-history from commit %s)\n' % orig
                )
            commit = self.git.commit_tree(
                tree, [commit, orig],
                msg=msg,
                metadata=self.git.get_author_info(orig),
            )

        self._set_refname(refname, commit, force=force)

    def simplify_to_border(
            self, refname,
            with_history1=False, with_history2=False, force=False,
            ):
        i1 = self.len1 - 1
        for i2 in range(1, self.len2):
            if not (i1, i2) in self:
                raise Failure(
                    'Cannot simplify to border because '
                    'merge %d-%d is not yet done'
                    % (i1, i2)
                    )

        i2 = self.len2 - 1
        for i1 in range(1, self.len1):
            if not (i1, i2) in self:
                raise Failure(
                    'Cannot simplify to border because '
                    'merge %d-%d is not yet done'
                    % (i1, i2)
                    )

        i1 = self.len1 - 1
        commit = self[i1, 0].sha1
        for i2 in range(1, self.len2 - 1):
            orig = self[0, i2].sha1
            tree = self.git.get_tree(self[i1, i2].sha1)

            # Create a commit, copying the old log message:
            if with_history2:
                parents = [commit, orig]
                msg = (
                    self.git.get_log_message(orig).rstrip('\n')
                    + '\n\n(rebased-with-history from commit %s)\n' % (orig,)
                    )
            else:
                parents = [commit]
                msg = (
                    self.git.get_log_message(orig).rstrip('\n')
                    + '\n\n(rebased from commit %s)\n' % (orig,)
                    )

            commit = self.git.commit_tree(
                tree, parents,
                msg=msg,
                metadata=self.git.get_author_info(orig),
            )
        commit1 = commit

        i2 = self.len2 - 1
        commit = self[0, i2].sha1
        for i1 in range(1, self.len1 - 1):
            orig = self[i1, 0].sha1
            tree = self.git.get_tree(self[i1, i2].sha1)

            # Create a commit, copying the old log message:
            if with_history1:
                parents = [orig, commit]
                msg = (
                    self.git.get_log_message(orig).rstrip('\n')
                    + '\n\n(rebased-with-history from commit %s)\n' % (orig,)
                    )
            else:
                parents = [commit]
                msg = (
                    self.git.get_log_message(orig).rstrip('\n')
                    + '\n\n(rebased from commit %s)\n' % (orig,)
                    )

            commit = self.git.commit_tree(
                tree, parents,
                msg=msg,
                metadata=self.git.get_author_info(orig),
            )
        commit2 = commit

        # Construct the apex commit:
        tree = self.git.get_tree(self[-1, -1].sha1)
        msg = (
            'Merge %s into %s (using imerge border)'
            % (self.tip2, self.tip1)
            )

        commit = self.git.commit_tree(tree, [commit1, commit2], msg=msg)

        # Update the reference:
        self._set_refname(refname, commit, force=force)

    def _simplify_to_path(self, refname, base, path, force=False):
        """Simplify based on path and set refname to the result.

        The base and path arguments are defined similarly to
        create_commit_chain(), except that instead of SHA-1s they may
        optionally represent commits via (i1, i2) tuples.

        """

        def to_sha1(arg):
            if type(arg) is tuple:
                commit_record = self[arg]
                if not commit_record.is_known():
                    raise MissingMergeFailure(*arg)
                return commit_record.sha1
            else:
                return arg

        base_sha1 = to_sha1(base)
        path_sha1 = []
        for (commit, metadata) in path:
            commit_sha1 = to_sha1(commit)
            metadata_sha1 = to_sha1(metadata)
            path_sha1.append((commit_sha1, metadata_sha1))

        # A path simplification is allowed to discard history, as long
        # as the *pre-simplification* apex commit is a descendant of
        # the branch to be moved.
        if path:
            apex = path_sha1[-1][0]
        else:
            apex = base_sha1

        if not force and not self.git.is_ff(refname, apex):
            raise Failure(
                '%s cannot be updated to %s without discarding history.\n'
                'Use --force if you are sure, or choose a different reference'
                % (refname, apex,)
                )

        # The update is OK, so here we can set force=True:
        self._set_refname(
            refname,
            self.git.create_commit_chain(base_sha1, path_sha1),
            force=True,
            )

    def simplify_to_rebase(self, refname, force=False):
        i1 = self.len1 - 1
        path = [
            ((i1, i2), (0, i2))
            for i2 in range(1, self.len2)
            ]

        try:
            self._simplify_to_path(refname, (i1, 0), path, force=force)
        except MissingMergeFailure as e:
            raise Failure(
                'Cannot simplify to %s because merge %d-%d is not yet done'
                % (self.goal, e.i1, e.i2)
                )

    def simplify_to_drop(self, refname, force=False):
        try:
            base = self.goalopts['base']
        except KeyError:
            raise Failure('Goal "drop" was not initialized correctly')

        i2 = self.len2 - 1
        path = [
            ((i1, i2), (i1, 0))
            for i1 in range(1, self.len1)
            ]

        try:
            self._simplify_to_path(refname, base, path, force=force)
        except MissingMergeFailure as e:
            raise Failure(
                'Cannot simplify to rebase because merge %d-%d is not yet done'
                % (e.i1, e.i2)
                )

    def simplify_to_revert(self, refname, force=False):
        self.simplify_to_rebase(refname, force=force)

    def simplify_to_merge(self, refname, force=False):
        if not (-1, -1) in self:
            raise Failure(
                'Cannot simplify to merge because merge %d-%d is not yet done'
                % (self.len1 - 1, self.len2 - 1)
                )
        tree = self.git.get_tree(self[-1, -1].sha1)
        parents = [self[-1, 0].sha1, self[0, -1].sha1]

        # Create a preliminary commit with a generic commit message:
        sha1 = self.git.commit_tree(
            tree, parents,
            msg='Merge %s into %s (using imerge)' % (self.tip2, self.tip1),
            )

        self._set_refname(refname, sha1, force=force)

        # Now let the user edit the commit log message:
        self.git.amend()

    def simplify(self, refname, force=False):
        """Simplify this MergeState and save the result to refname.

        The merge must be complete before calling this method."""

        if self.goal == 'full':
            self.simplify_to_full(refname, force=force)
        elif self.goal == 'rebase':
            self.simplify_to_rebase(refname, force=force)
        elif self.goal == 'rebase-with-history':
            self.simplify_to_rebase_with_history(refname, force=force)
        elif self.goal == 'border':
            self.simplify_to_border(refname, force=force)
        elif self.goal == 'border-with-history':
            self.simplify_to_border(refname, with_history2=True, force=force)
        elif self.goal == 'border-with-history2':
            self.simplify_to_border(
                refname, with_history1=True, with_history2=True, force=force,
                )
        elif self.goal == 'drop':
            self.simplify_to_drop(refname, force=force)
        elif self.goal == 'revert':
            self.simplify_to_revert(refname, force=force)
        elif self.goal == 'merge':
            self.simplify_to_merge(refname, force=force)
        else:
            raise ValueError('Invalid value for goal (%r)' % (self.goal,))

    def save(self):
        """Write the current MergeState to the repository."""

        blockers = []
        for i2 in range(0, self.len2):
            for i1 in range(0, self.len1):
                record = self[i1, i2]
                if record.is_known():
                    record.save(self.git, self.name, i1, i2)
                if record.is_blocked():
                    blockers.append((i1, i2))

        state = dict(
            version='.'.join(str(i) for i in STATE_VERSION),
            blockers=blockers,
            tip1=self.tip1, tip2=self.tip2,
            goal=self.goal,
            goalopts=self.goalopts,
            manual=self.manual,
            branch=self.branch,
            )
        self.git.write_imerge_state_dict(self.name, state)

    def __str__(self):
        return 'MergeState(\'%s\', tip1=\'%s\', tip2=\'%s\', goal=\'%s\')' % (
            self.name, self.tip1, self.tip2, self.goal,
            )


def choose_merge_name(git, name):
    names = list(git.iter_existing_imerge_names())

    # If a name was specified, try to use it and fail if not possible:
    if name is not None:
        if name not in names:
            raise Failure('There is no incremental merge called \'%s\'!' % (name,))
        if len(names) > 1:
            # Record this as the new default:
            git.set_default_imerge_name(name)
        return name

    # A name was not specified.  Try to use the default name:
    default_name = git.get_default_imerge_name()
    if default_name:
        if git.check_imerge_exists(default_name):
            return default_name
        else:
            # There's no reason to keep the invalid default around:
            git.set_default_imerge_name(None)
            raise Failure(
                'Warning: The default incremental merge \'%s\' has disappeared.\n'
                '(The setting imerge.default has been cleared.)\n'
                'Please try again.'
                % (default_name,)
                )

    # If there is exactly one imerge, set it to be the default and use it.
    if len(names) == 1 and git.check_imerge_exists(names[0]):
        return names[0]

    raise Failure('Please select an incremental merge using --name')


def read_merge_state(git, name=None):
    return MergeState.read(git, choose_merge_name(git, name))


def cmd_list(parser, options):
    git = GitRepository()
    names = list(git.iter_existing_imerge_names())
    default_merge = git.get_default_imerge_name()
    if not default_merge and len(names) == 1:
        default_merge = names[0]
    for name in names:
        if name == default_merge:
            sys.stdout.write('* %s\n' % (name,))
        else:
            sys.stdout.write('  %s\n' % (name,))


def cmd_init(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')

    if not options.name:
        parser.error(
            'Please specify the --name to be used for this incremental merge'
            )
    tip1 = git.get_head_refname(short=True) or 'HEAD'
    tip2 = options.tip2
    try:
        (merge_base, commits1, commits2) = git.get_boundaries(
            tip1, tip2, options.first_parent,
            )
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))

    merge_state = MergeState.initialize(
        git, options.name, merge_base,
        tip1, commits1,
        tip2, commits2,
        goal=options.goal, manual=options.manual,
        branch=(options.branch or options.name),
        )
    merge_state.save()
    if len(list(git.iter_existing_imerge_names())) > 1:
        git.set_default_imerge_name(options.name)


def cmd_start(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')

    if not options.name:
        parser.error(
            'Please specify the --name to be used for this incremental merge'
            )
    tip1 = git.get_head_refname(short=True) or 'HEAD'
    tip2 = options.tip2

    try:
        (merge_base, commits1, commits2) = git.get_boundaries(
            tip1, tip2, options.first_parent,
            )
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))

    merge_state = MergeState.initialize(
        git, options.name, merge_base,
        tip1, commits1,
        tip2, commits2,
        goal=options.goal, manual=options.manual,
        branch=(options.branch or options.name),
        )
    merge_state.save()
    if len(list(git.iter_existing_imerge_names())) > 1:
        git.set_default_imerge_name(options.name)

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        merge_state.request_user_merge(e.i1, e.i2)
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_merge(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')

    tip2 = options.tip2

    if options.name:
        name = options.name
    else:
        # By default, name the imerge after the branch being merged:
        name = tip2
        git.check_imerge_name_format(name)

    tip1 = git.get_head_refname(short=True)
    if tip1:
        if not options.branch:
            # See if we can store the result to the checked-out branch:
            try:
                git.check_branch_name_format(tip1)
            except InvalidBranchNameError:
                pass
            else:
                options.branch = tip1
    else:
        tip1 = 'HEAD'

    if not options.branch:
        if options.name:
            options.branch = options.name
        else:
            parser.error(
                'HEAD is not a simple branch.  '
                'Please specify --branch for storing results.'
                )

    try:
        (merge_base, commits1, commits2) = git.get_boundaries(
            tip1, tip2, options.first_parent,
            )
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))
    except NothingToDoError as e:
        sys.stdout.write('Already up-to-date.\n')
        sys.exit(0)

    merge_state = MergeState.initialize(
        git, name, merge_base,
        tip1, commits1,
        tip2, commits2,
        goal=options.goal, manual=options.manual,
        branch=options.branch,
        )
    merge_state.save()
    if len(list(git.iter_existing_imerge_names())) > 1:
        git.set_default_imerge_name(name)

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        merge_state.request_user_merge(e.i1, e.i2)
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_rebase(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')

    tip1 = options.tip1

    tip2 = git.get_head_refname(short=True)
    if tip2:
        if not options.branch:
            # See if we can store the result to the current branch:
            try:
                git.check_branch_name_format(tip2)
            except InvalidBranchNameError:
                pass
            else:
                options.branch = tip2
        if not options.name:
            # By default, name the imerge after the branch being rebased:
            options.name = tip2
    else:
        tip2 = git.rev_parse('HEAD')

    if not options.name:
        parser.error(
            'The checked-out branch could not be used as the imerge name.\n'
            'Please use the --name option.'
            )

    if not options.branch:
        if options.name:
            options.branch = options.name
        else:
            parser.error(
                'HEAD is not a simple branch.  '
                'Please specify --branch for storing results.'
                )

    try:
        (merge_base, commits1, commits2) = git.get_boundaries(
            tip1, tip2, options.first_parent,
            )
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))
    except NothingToDoError as e:
        sys.stdout.write('Already up-to-date.\n')
        sys.exit(0)

    merge_state = MergeState.initialize(
        git, options.name, merge_base,
        tip1, commits1,
        tip2, commits2,
        goal=options.goal, manual=options.manual,
        branch=options.branch,
        )
    merge_state.save()
    if len(list(git.iter_existing_imerge_names())) > 1:
        git.set_default_imerge_name(options.name)

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        merge_state.request_user_merge(e.i1, e.i2)
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_drop(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')

    m = re.match(r'^(?P<start>.*[^\.])(?P<sep>\.{2,})(?P<end>[^\.].*)$', options.range)
    if m:
        if m.group('sep') != '..':
            parser.error(
                'Range must either be a single commit '
                'or in the form "commit..commit"'
                )
        start = git.rev_parse(m.group('start'))
        end = git.rev_parse(m.group('end'))
    else:
        end = git.rev_parse(options.range)
        start = git.rev_parse('%s^' % (end,))

    try:
        to_drop = git.linear_ancestry(start, end, options.first_parent)
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))

    # Suppose we want to drop commits 2 and 3 in the branch below.
    # Then we set up an imerge as follows:
    #
    #     o - 0 - 1 - 2 - 3 - 4 - 5 - 6    ← tip1
    #                     |
    #                     3⁻¹
    #                     |
    #                     2⁻¹
    #
    #                     ↑
    #                    tip2
    #
    # We first use imerge to rebase tip1 onto tip2, then we simplify
    # by discarding the sequence (2, 3, 3⁻¹, 2⁻¹) (which together are
    # a NOOP). In this case, goalopts would have the following
    # contents:
    #
    #     goalopts['base'] = rev_parse(commit1)

    tip1 = git.get_head_refname(short=True)
    if tip1:
        if not options.branch:
            # See if we can store the result to the current branch:
            try:
                git.check_branch_name_format(tip1)
            except InvalidBranchNameError:
                pass
            else:
                options.branch = tip1
        if not options.name:
            # By default, name the imerge after the branch being rebased:
            options.name = tip1
    else:
        tip1 = git.rev_parse('HEAD')

    if not options.name:
        parser.error(
            'The checked-out branch could not be used as the imerge name.\n'
            'Please use the --name option.'
            )

    if not options.branch:
        if options.name:
            options.branch = options.name
        else:
            parser.error(
                'HEAD is not a simple branch.  '
                'Please specify --branch for storing results.'
                )

    # Create a branch based on end that contains the inverse of the
    # commits that we want to drop. This will be tip2:

    git.checkout(end)
    for commit in reversed(to_drop):
        git.revert(commit)

    tip2 = git.rev_parse('HEAD')

    try:
        (merge_base, commits1, commits2) = git.get_boundaries(
            tip1, tip2, options.first_parent,
            )
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))
    except NothingToDoError as e:
        sys.stdout.write('Already up-to-date.\n')
        sys.exit(0)

    merge_state = MergeState.initialize(
        git, options.name, merge_base,
        tip1, commits1,
        tip2, commits2,
        goal='drop', goalopts={'base' : start},
        manual=options.manual,
        branch=options.branch,
        )
    merge_state.save()
    if len(list(git.iter_existing_imerge_names())) > 1:
        git.set_default_imerge_name(options.name)

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        merge_state.request_user_merge(e.i1, e.i2)
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_revert(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')

    m = re.match(r'^(?P<start>.*[^\.])(?P<sep>\.{2,})(?P<end>[^\.].*)$', options.range)
    if m:
        if m.group('sep') != '..':
            parser.error(
                'Range must either be a single commit '
                'or in the form "commit..commit"'
                )
        start = git.rev_parse(m.group('start'))
        end = git.rev_parse(m.group('end'))
    else:
        end = git.rev_parse(options.range)
        start = git.rev_parse('%s^' % (end,))

    try:
        to_revert = git.linear_ancestry(start, end, options.first_parent)
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))

    # Suppose we want to revert commits 2 and 3 in the branch below.
    # Then we set up an imerge as follows:
    #
    #     o - 0 - 1 - 2 - 3 - 4 - 5 - 6    ← tip1
    #                     |
    #                     3⁻¹
    #                     |
    #                     2⁻¹
    #
    #                     ↑
    #                    tip2
    #
    # Then we use imerge to rebase tip2 onto tip1.

    tip1 = git.get_head_refname(short=True)
    if tip1:
        if not options.branch:
            # See if we can store the result to the current branch:
            try:
                git.check_branch_name_format(tip1)
            except InvalidBranchNameError:
                pass
            else:
                options.branch = tip1
        if not options.name:
            # By default, name the imerge after the branch being rebased:
            options.name = tip1
    else:
        tip1 = git.rev_parse('HEAD')

    if not options.name:
        parser.error(
            'The checked-out branch could not be used as the imerge name.\n'
            'Please use the --name option.'
            )

    if not options.branch:
        if options.name:
            options.branch = options.name
        else:
            parser.error(
                'HEAD is not a simple branch.  '
                'Please specify --branch for storing results.'
                )

    # Create a branch based on end that contains the inverse of the
    # commits that we want to drop. This will be tip2:

    git.checkout(end)
    for commit in reversed(to_revert):
        git.revert(commit)

    tip2 = git.rev_parse('HEAD')

    try:
        (merge_base, commits1, commits2) = git.get_boundaries(
            tip1, tip2, options.first_parent,
            )
    except NonlinearAncestryError as e:
        if options.first_parent:
            parser.error(str(e))
        else:
            parser.error('%s\nPerhaps use "--first-parent"?' % (e,))
    except NothingToDoError as e:
        sys.stdout.write('Already up-to-date.\n')
        sys.exit(0)

    merge_state = MergeState.initialize(
        git, options.name, merge_base,
        tip1, commits1,
        tip2, commits2,
        goal='revert',
        manual=options.manual,
        branch=options.branch,
        )
    merge_state.save()
    if len(list(git.iter_existing_imerge_names())) > 1:
        git.set_default_imerge_name(options.name)

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        merge_state.request_user_merge(e.i1, e.i2)
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_remove(parser, options):
    git = GitRepository()
    MergeState.remove(git, choose_merge_name(git, options.name))


def cmd_continue(parser, options):
    git = GitRepository()
    merge_state = read_merge_state(git, options.name)
    try:
        merge_state.incorporate_user_merge(edit_log_msg=options.edit)
    except NoManualMergeError:
        pass
    except NotABlockingCommitError as e:
        raise Failure(str(e))
    except ManualMergeUnusableError as e:
        raise Failure(str(e))

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        merge_state.request_user_merge(e.i1, e.i2)
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_record(parser, options):
    git = GitRepository()
    merge_state = read_merge_state(git, options.name)
    try:
        merge_state.incorporate_user_merge(edit_log_msg=options.edit)
    except NoManualMergeError as e:
        raise Failure(str(e))
    except NotABlockingCommitError:
        raise Failure(str(e))
    except ManualMergeUnusableError as e:
        raise Failure(str(e))

    try:
        merge_state.auto_complete_frontier()
    except FrontierBlockedError as e:
        pass
    else:
        sys.stderr.write('Merge is complete!\n')


def cmd_autofill(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')
    merge_state = read_merge_state(git, options.name)
    with git.temporary_head(message='imerge: restoring'):
        try:
            merge_state.auto_complete_frontier()
        except FrontierBlockedError as e:
            raise Failure(str(e))


def cmd_simplify(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')
    merge_state = read_merge_state(git, options.name)
    if not merge_state.map_frontier().is_complete():
        raise Failure('Merge %s is not yet complete!' % (merge_state.name,))
    refname = 'refs/heads/%s' % ((options.branch or merge_state.branch),)
    if options.goal is not None:
        merge_state.set_goal(options.goal)
        merge_state.save()
    merge_state.simplify(refname, force=options.force)


def cmd_finish(parser, options):
    git = GitRepository()
    git.require_clean_work_tree('proceed')
    merge_state = read_merge_state(git, options.name)
    if not merge_state.map_frontier().is_complete():
        raise Failure('Merge %s is not yet complete!' % (merge_state.name,))
    refname = 'refs/heads/%s' % ((options.branch or merge_state.branch),)
    if options.goal is not None:
        merge_state.set_goal(options.goal)
        merge_state.save()
    merge_state.simplify(refname, force=options.force)
    MergeState.remove(git, merge_state.name)


def cmd_diagram(parser, options):
    git = GitRepository()
    if not (options.commits or options.frontier):
        options.frontier = True
    if not (options.color or (options.color is None and sys.stdout.isatty())):
        AnsiColor.disable()

    merge_state = read_merge_state(git, options.name)
    if options.commits:
        merge_state.write(sys.stdout, merge_state.tip1, merge_state.tip2)
        sys.stdout.write('\n')
    if options.frontier:
        merge_frontier = merge_state.map_frontier()
        merge_frontier.write(sys.stdout, merge_state.tip1, merge_state.tip2)
        sys.stdout.write('\n')
    if options.html:
        merge_frontier = merge_state.map_frontier()
        html = open(options.html, 'w')
        merge_frontier.write_html(html, merge_state.name)
        html.close()
    sys.stdout.write(
        'Key:\n'
        )
    if options.frontier:
        sys.stdout.write(
            '  |,-,+ = rectangles forming current merge frontier\n'
            )
    sys.stdout.write(
        '  * = merge done manually\n'
        '  . = merge done automatically\n'
        '  # = conflict that is currently blocking progress\n'
        '  @ = merge was blocked but has been resolved\n'
        '  ? = no merge recorded\n'
        '\n'
        )


def reparent_recursively(git, start_commit, parents, end_commit):
    """Change the parents of start_commit and its descendants.

    Change start_commit to have the specified parents, and reparent
    all commits on the ancestry path between start_commit and
    end_commit accordingly. Return the replacement end_commit.
    start_commit, parents, and end_commit must all be resolved OIDs.

    """

    # A map {old_oid : new_oid} keeping track of which replacements
    # have to be made:
    replacements = {}

    # Reparent start_commit:
    replacements[start_commit] = git.reparent(start_commit, parents)

    for (commit, parents) in git.rev_list_with_parents(
            '--ancestry-path', '--topo-order', '--reverse',
            '%s..%s' % (start_commit, end_commit)
            ):
        parents = [replacements.get(p, p) for p in parents]
        replacements[commit] = git.reparent(commit, parents)

    try:
        return replacements[end_commit]
    except KeyError:
        raise ValueError(
            "%s is not an ancestor of %s" % (start_commit, end_commit),
        )


def cmd_reparent(parser, options):
    git = GitRepository()
    try:
        commit = git.get_commit_sha1(options.commit)
    except ValueError:
        sys.exit('%s is not a valid commit', options.commit)

    try:
        head = git.get_commit_sha1('HEAD')
    except ValueError:
        sys.exit('HEAD is not a valid commit')

    try:
        parents = [git.get_commit_sha1(p) for p in options.parents]
    except ValueError as e:
        sys.exit(e.message)

    sys.stderr.write('Reparenting %s..HEAD\n' % (options.commit,))

    try:
        new_head = reparent_recursively(git, commit, parents, head)
    except ValueError as e:
        sys.exit(e.message)

    sys.stdout.write('%s\n' % (new_head,))


def main(args):
    NAME_INIT_HELP = 'name to use for this incremental merge'

    def add_name_argument(subparser, help=None):
        if help is None:
            subcommand = subparser.prog.split()[1]
            help = 'name of incremental merge to {0}'.format(subcommand)

        subparser.add_argument(
            '--name', action='store', default=None, help=help,
            )

    def add_goal_argument(subparser, default=DEFAULT_GOAL):
        help = 'the goal of the incremental merge'
        if default is None:
            help = (
                'the type of simplification to be made '
                '(default is the value provided to "init" or "start")'
                )
        subparser.add_argument(
            '--goal',
            action='store', default=default,
            choices=ALLOWED_GOALS,
            help=help,
            )

    def add_branch_argument(subparser):
        subcommand = subparser.prog.split()[1]
        help = 'the name of the branch to which the result will be stored'
        if subcommand in ['simplify', 'finish']:
            help = (
                'the name of the branch to which to store the result '
                '(default is the value provided to "init" or "start" if any; '
                'otherwise the name of the merge).   '
                'If BRANCH already exists then it must be able to be '
                'fast-forwarded to the result unless the --force option is '
                'specified.'
                )
        subparser.add_argument(
            '--branch',
            action='store', default=None,
            help=help,
            )

    def add_manual_argument(subparser):
        subparser.add_argument(
            '--manual',
            action='store_true', default=False,
            help=(
                'ask the user to complete all merges manually, even when they '
                'appear conflict-free.  This option disables the usual bisection '
                'algorithm and causes the full incremental merge diagram to be '
                'completed.'
                ),
            )

    def add_first_parent_argument(subparser, default=None):
        subcommand = subparser.prog.split()[1]
        help = (
            'handle only the first parent commits '
            '(this option is currently required if the history is nonlinear)'
            )
        if subcommand in ['merge', 'rebase']:
            help = argparse.SUPPRESS
        subparser.add_argument(
            '--first-parent', action='store_true', default=default, help=help,
            )

    def add_tip2_argument(subparser):
        subparser.add_argument(
            'tip2', action='store', metavar='branch',
            help='the tip of the branch to be merged into HEAD',
        )

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        )
    subparsers = parser.add_subparsers(dest='subcommand', help='sub-command')

    subparser = subparsers.add_parser(
        'start',
        help=(
            'start a new incremental merge '
            '(equivalent to "init" followed by "continue")'
            ),
        )
    add_name_argument(subparser, help=NAME_INIT_HELP)
    add_goal_argument(subparser)
    add_branch_argument(subparser)
    add_manual_argument(subparser)
    add_first_parent_argument(subparser)
    add_tip2_argument(subparser)

    subparser = subparsers.add_parser(
        'merge',
        help='start a simple merge via incremental merge',
        )
    add_name_argument(subparser, help=NAME_INIT_HELP)
    add_goal_argument(subparser, default='merge')
    add_branch_argument(subparser)
    add_manual_argument(subparser)
    add_first_parent_argument(subparser, default=True)
    add_tip2_argument(subparser)

    subparser = subparsers.add_parser(
        'rebase',
        help='start a simple rebase via incremental merge',
        )
    add_name_argument(subparser, help=NAME_INIT_HELP)
    add_goal_argument(subparser, default='rebase')
    add_branch_argument(subparser)
    add_manual_argument(subparser)
    add_first_parent_argument(subparser, default=True)
    subparser.add_argument(
        'tip1', action='store', metavar='branch',
        help=(
            'the tip of the branch onto which the current branch should '
            'be rebased'
            ),
        )

    subparser = subparsers.add_parser(
        'drop',
        help='drop one or more commits via incremental merge',
        )
    add_name_argument(subparser, help=NAME_INIT_HELP)
    add_branch_argument(subparser)
    add_manual_argument(subparser)
    add_first_parent_argument(subparser, default=True)
    subparser.add_argument(
        'range', action='store', metavar='[commit | commit..commit]',
        help=(
            'the commit or range of commits that should be dropped'
            ),
        )

    subparser = subparsers.add_parser(
        'revert',
        help='revert one or more commits via incremental merge',
        )
    add_name_argument(subparser, help=NAME_INIT_HELP)
    add_branch_argument(subparser)
    add_manual_argument(subparser)
    add_first_parent_argument(subparser, default=True)
    subparser.add_argument(
        'range', action='store', metavar='[commit | commit..commit]',
        help=(
            'the commit or range of commits that should be reverted'
            ),
        )

    subparser = subparsers.add_parser(
        'continue',
        help=(
            'record the merge at branch imerge/NAME '
            'and start the next step of the merge '
            '(equivalent to "record" followed by "autofill" '
            'and then sets up the working copy with the next '
            'conflict that has to be resolved manually)'
            ),
        )
    add_name_argument(subparser)
    subparser.set_defaults(edit=None)
    subparser.add_argument(
        '--edit', '-e', dest='edit', action='store_true',
        help='commit staged changes with the --edit option',
        )
    subparser.add_argument(
        '--no-edit', dest='edit', action='store_false',
        help='commit staged changes with the --no-edit option',
        )

    subparser = subparsers.add_parser(
        'finish',
        help=(
            'simplify then remove a completed incremental merge '
            '(equivalent to "simplify" followed by "remove")'
            ),
        )
    add_name_argument(subparser)
    add_goal_argument(subparser, default=None)
    add_branch_argument(subparser)
    subparser.add_argument(
        '--force',
        action='store_true', default=False,
        help='allow the target branch to be updated in a non-fast-forward manner',
        )

    subparser = subparsers.add_parser(
        'diagram',
        help='display a diagram of the current state of a merge',
        )
    add_name_argument(subparser)
    subparser.add_argument(
        '--commits', action='store_true', default=False,
        help='show the merges that have been made so far',
        )
    subparser.add_argument(
        '--frontier', action='store_true', default=False,
        help='show the current merge frontier',
        )
    subparser.add_argument(
        '--html', action='store', default=None,
        help='generate HTML diagram showing the current merge frontier',
        )
    subparser.add_argument(
        '--color', dest='color', action='store_true', default=None,
        help='draw diagram with colors',
        )
    subparser.add_argument(
        '--no-color', dest='color', action='store_false',
        help='draw diagram without colors',
        )

    subparser = subparsers.add_parser(
        'list',
        help=(
            'list the names of incremental merges that are currently in progress.  '
            'The active merge is shown with an asterisk next to it.'
            ),
        )

    subparser = subparsers.add_parser(
        'init',
        help='initialize a new incremental merge',
        )
    add_name_argument(subparser, help=NAME_INIT_HELP)
    add_goal_argument(subparser)
    add_branch_argument(subparser)
    add_manual_argument(subparser)
    add_first_parent_argument(subparser)
    add_tip2_argument(subparser)

    subparser = subparsers.add_parser(
        'record',
        help='record the merge at branch imerge/NAME',
        )
    # record:
    add_name_argument(
        subparser,
        help='name of merge to which the merge should be added',
        )
    subparser.set_defaults(edit=None)
    subparser.add_argument(
        '--edit', '-e', dest='edit', action='store_true',
        help='commit staged changes with the --edit option',
        )
    subparser.add_argument(
        '--no-edit', dest='edit', action='store_false',
        help='commit staged changes with the --no-edit option',
        )

    subparser = subparsers.add_parser(
        'autofill',
        help='autofill non-conflicting merges',
        )
    add_name_argument(subparser)

    subparser = subparsers.add_parser(
        'simplify',
        help=(
            'simplify a completed incremental merge by discarding unneeded '
            'intermediate merges and cleaning up the ancestry of the commits '
            'that are retained'
            ),
        )
    add_name_argument(subparser)
    add_goal_argument(subparser, default=None)
    add_branch_argument(subparser)
    subparser.add_argument(
        '--force',
        action='store_true', default=False,
        help='allow the target branch to be updated in a non-fast-forward manner',
        )

    subparser = subparsers.add_parser(
        'remove',
        help='irrevocably remove an incremental merge',
        )
    add_name_argument(subparser)

    subparser = subparsers.add_parser(
        'reparent',
        help=(
            'change the parents of the specified commit and propagate the '
            'change to HEAD'
            ),
        )
    subparser.add_argument(
        '--commit', metavar='COMMIT', default='HEAD',
        help=(
            'target commit to reparent. Create a new commit identical to '
            'this one, but having the specified parents. Then create '
            'new versions of all descendants of this commit all the way to '
            'HEAD, incorporating the modified commit. Output the SHA-1 of '
            'the replacement HEAD commit.'
        ),
    )
    subparser.add_argument(
        'parents', nargs='*', metavar='PARENT',
        help='a list of commits',
        )

    options = parser.parse_args(args)

    # Set an environment variable GIT_IMERGE=1 while we are running.
    # This makes it possible for hook scripts etc. to know that they
    # are being run within git-imerge, and should perhaps behave
    # differently.  In the future we might make the value more
    # informative, like GIT_IMERGE=[automerge|autofill|...].
    os.environ[str('GIT_IMERGE')] = str('1')

    if options.subcommand == 'list':
        cmd_list(parser, options)
    elif options.subcommand == 'init':
        cmd_init(parser, options)
    elif options.subcommand == 'start':
        cmd_start(parser, options)
    elif options.subcommand == 'merge':
        cmd_merge(parser, options)
    elif options.subcommand == 'rebase':
        cmd_rebase(parser, options)
    elif options.subcommand == 'drop':
        cmd_drop(parser, options)
    elif options.subcommand == 'revert':
        cmd_revert(parser, options)
    elif options.subcommand == 'remove':
        cmd_remove(parser, options)
    elif options.subcommand == 'continue':
        cmd_continue(parser, options)
    elif options.subcommand == 'record':
        cmd_record(parser, options)
    elif options.subcommand == 'autofill':
        cmd_autofill(parser, options)
    elif options.subcommand == 'simplify':
        cmd_simplify(parser, options)
    elif options.subcommand == 'finish':
        cmd_finish(parser, options)
    elif options.subcommand == 'diagram':
        cmd_diagram(parser, options)
    elif options.subcommand == 'reparent':
        cmd_reparent(parser, options)
    else:
        parser.error('Unrecognized subcommand')


def climain():
    try:
        main(sys.argv[1:])
    except Failure as e:
        sys.exit(str(e))


if __name__ == "__main__":
    climain()


# vim: set expandtab ft=python:

#!/usr/bin/env python
"""WAL-E is a program to assist in performing PostgreSQL continuous
archiving on S3: it handles the major four operations of
arching/receiving WAL segments and archiving/receiving base hot
backups of the PostgreSQL file cluster.

"""

import argparse
import contextlib
import copy
import csv
import datetime
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

# Provides guidence in object names as to the version of the file
# structure.
FILE_STRUCTURE_VERSION = '002'
PSQL_BIN = 'psql'
LZOP_BIN = 'lzop'
S3CMD_BIN = 's3cmd'


def subprocess_setup(f=None):
    """
    SIGPIPE reset for subprocess workaround

    Python installs a SIGPIPE handler by default. This is usually not
    what non-Python subprocesses expect.

    Calls an optional "f" first in case other code wants a preexec_fn,
    then restores SIGPIPE to what most Unix processes expect.

    http://bugs.python.org/issue1652
    http://www.chiark.greenend.org.uk/ucgi/~cjwatson/blosxom/2009-07-02-python-sigpipe.html

    """

    def wrapper(*args, **kwargs):
        if f is not None:
            f(*args, **kwargs)

        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    return wrapper


def popen_sp(*args, **kwargs):
    """
    Same as subprocess.Popen, but restores SIGPIPE

    This bug is documented (See subprocess_setup) but did not make it
    to standard library.  Could also be resolved by using the
    python-subprocess32 backport and using it appropriately (See
    'restore_signals' keyword argument to Popen)

    """

    kwargs['preexec_fn'] = subprocess_setup(kwargs.get('preexec_fn'))
    return subprocess.Popen(*args, **kwargs)


def pipe(*args):
    """
    Takes as parameters several dicts, each with the same
    parameters passed to popen.

    Runs the various processes in a pipeline, connecting
    the stdout of every process except the last with the
    stdin of the next process.

    Adapted from http://www.enricozini.org/2009/debian/python-pipes/

    """
    if len(args) < 2:
        raise ValueError, "pipe needs at least 2 processes"
    # Set stdout=PIPE in every subprocess except the last
    for i in args[:-1]:
        i["stdout"] = subprocess.PIPE

    # Runs all subprocesses connecting stdins and stdouts to create the
    # pipeline. Closes stdouts to avoid deadlocks.
    popens = [popen_sp(**args[0])]
    for i in range(1,len(args)):
        args[i]["stdin"] = popens[i - 1].stdout
        popens.append(popen_sp(**args[i]))
        popens[i - 1].stdout.close()

    # Returns the array of subprocesses just created
    return popens


def pipe_wait(popens):
    """
    Given an array of Popen objects returned by the
    pipe method, wait for all processes to terminate
    and return the array with their return values.

    Taken from http://www.enricozini.org/2009/debian/python-pipes/

    """
    # Avoid mutating the passed copy
    popens = copy.copy(popens)
    results = [0] * len(popens)
    while popens:
        last = popens.pop(-1)
        results[len(popens)] = last.wait()
    return results


def psql_csv_run(sql_command, error_handler=None):
    """
    Runs psql and returns a CSVReader object from the query

    This CSVReader includes header names as the first record in all
    situations.  The output is fully buffered into Python.

    """
    csv_query = ('COPY ({query}) TO STDOUT WITH CSV HEADER;'
                 .format(query=sql_command))

    psql_proc = popen_sp([PSQL_BIN, '-d', 'postgres', '-c', csv_query],
                         stdout=subprocess.PIPE)
    stdout, stderr = psql_proc.communicate()

    if psql_proc.returncode != 0:
        if error_handler is not None:
            error_handler(psql_proc)
        else:
            assert error_handler is None
            raise Exception('Could not csv-execute "{query}" successfully'
                            .format(query=self._sqlcmd))

    # Previous code must raise any desired exceptions for non-zero
    # exit codes
    assert psql_proc.returncode == 0

    # Fake enough iterator interface to get a CSV Reader object
    # that works.
    return csv.reader(iter(stdout.strip().split('\n')))

class PgBackupStatements(object):
    """
    Contains operators to start and stop a backup on a Postgres server

    Relies on PsqlHelp for underlying mechanism.

    """

    @staticmethod
    def _dict_transform(csv_reader):
        rows = list(csv_reader)
        assert len(rows) == 2, 'Expect header row and data row'
        return dict(zip(*rows))

    @classmethod
    def run_start_backup(cls):
        """
        Connects to a server and attempts to start a hot backup

        Yields the WAL information in a dictionary for bookkeeping and
        recording.

        """
        def handler(popen):
            assert popen.returncode != 0
            raise Exception('Could not start hot backup')

        label = 'freeze_start_' + datetime.datetime.now().isoformat()

        return cls._dict_transform(psql_csv_run(
                "SELECT file_name, "
                "  lpad(file_offset::text, 8, '0') AS file_offset "
                "FROM pg_xlogfile_name_offset("
                "  pg_start_backup('{0}'))".format(label),
                error_handler=handler))

    @classmethod
    def run_stop_backup(cls):
        """
        Stop a hot backup, if it was running, or error

        Return the last WAL file name and position that is required to
        gain consistency on the captured heap.

        """
        def handler(popen):
            assert popen.returncode != 0
            raise Exception('Could not stop hot backup')

        return cls._dict_transform(psql_csv_run(
                "SELECT file_name, "
                "  lpad(file_offset::text, 8, '0') AS file_offset "
                "FROM pg_xlogfile_name_offset("
                "  pg_stop_backup())", error_handler=handler))

    @classmethod
    def pg_version(cls):
        """
        Get a very informative version string from Postgres

        Includes minor version, major version, and architecture, among
        other details.

        """
        return cls._dict_transform(psql_csv_run('SELECT * FROM version()'))


def check_call_wait_sigint(*popenargs, **kwargs):
    got_sigint = False
    wait_sigint_proc = None

    try:
        wait_sigint_proc = popen_sp(*popenargs, **kwargs)
    except KeyboardInterrupt, e:
        got_sigint = True
        if wait_sigint_proc is not None:
            wait_sigint_proc.send_signal(signal.SIGINT)
            wait_sigint_proc.wait()
            raise e
    finally:
        if wait_sigint_proc and not got_sigint:
            wait_sigint_proc.wait()

            if wait_sigint_proc.returncode != 0:
                # Try to identify the argv sent via 'popenargs' and
                # kwargs sent to subprocess.Popen: this can be sent
                # positionally, or in the form of kwargs.
                if len(args) > 0:
                    raise subprocess.CalledProcessError(
                        wait_sigint_proc.returncode, args[0])
                elif 'args' in kwargs:
                    raise subprocess.CalledProcessError(
                        wait_sigint_proc.returncode, kwargs['args'])
                else:
                    assert False
            else:
                return wait_sigint_proc.returncode


def do_lzop_s3_put(s3_url, path, s3cmd_config_path):
    """
    Synchronous version of the s3-upload wrapper

    Nominally intended to be used through a pool, but exposed here
    for testing and experimentation.

    """
    with tempfile.NamedTemporaryFile(mode='w') as tf:
        compression_p = popen_sp([LZOP_BIN, '--stdout', path], stdout=tf)
        compression_p.wait()

        if compression_p.returncode != 0:
            raise Exception(
                'Could not properly compress heap file: {path}'
                .format(path=path))

        # Not to be confused with fsync: the point is to make
        # sure any Python-buffered output is visible to other
        # processes, but *NOT* force a write to disk.
        tf.flush()

        check_call_wait_sigint([S3CMD_BIN, '-c', s3cmd_config_path,
                                'put', tf.name, s3_url + '.lzo'])


def do_lzop_s3_get(s3_url, path, s3cmd_config_path):
    """
    Get and decompress a S3 URL

    This streams the s3cmd directly to lzop; the compressed version is
    never stored on disk.

    """

    assert s3_url.endswith('.lzo'), 'Expect an lzop-compressed file'

    with open(path, 'wb') as decomp_out:
        popens = []

        try:
            popens = pipe(
                dict(args=[S3CMD_BIN, '-c', s3cmd_config_path,
                           'get', s3_url, '-']),
                dict(args=[LZOP_BIN, '-d'], stdout=decomp_out))
            pipe_wait(popens)

            s3cmd_proc, lzop_proc = popens

            def check_exitcode(cmdname, popen):
                if popen.returncode != 0:
                    raise Exception(cmdname + ' terminated with exit code: ' +
                                    unicode(s3cmd_proc.returncode))

            check_exitcode('s3cmd', s3cmd_proc)
            check_exitcode('lzop', lzop_proc)

            print >>sys.stderr, ('Got and decompressed file: '
                                 '{s3_url} to {path}'
                                 .format(**locals()))
        except KeyboardInterrupt, keyboard_int:
            for popen in popens:
                try:
                    popen.send_signal(signal.SIGINT)
                    popen.wait()
                except OSError, e:
                    # No such process == 3
                    if e.errno != 3:
                        raise e

            raise keyboard_int


class S3Backup(object):
    """
    A performs s3cmd uploads to of PostgreSQL WAL files and clusters

    """

    def __init__(self,
                 aws_access_key_id, aws_secret_access_key, s3_prefix):
        self.aws_access_key_id = aws_access_key_id
        self.aws_secret_access_key = aws_secret_access_key

        # Canonicalize the s3 prefix by stripping any trailing slash
        self.s3_prefix = s3_prefix.rstrip('/')

    @property
    @contextlib.contextmanager
    def s3cmd_temp_config(self):
        with tempfile.NamedTemporaryFile(mode='w') as s3cmd_config:
            s3cmd_config.write(textwrap.dedent("""\
            [default]
            access_key = {aws_access_key_id}
            secret_key = {aws_secret_access_key}
            use_https = True
            """).format(aws_access_key_id=self.aws_access_key_id,
                        aws_secret_access_key=self.aws_secret_access_key))

            s3cmd_config.flush()
            s3cmd_config.seek(0)

            yield s3cmd_config

    def backup_list(self):
        """
        Prints out a list of the basebackups directory

        This is raw s3cmd output, intended for processing via *NIX
        text wrangling or casual inspection.
        """
        with self.s3cmd_temp_config as s3cmd_config:
            check_call_wait_sigint([S3CMD_BIN, '-c', s3cmd_config.name, 'ls',
                                    self.s3_prefix + '/basebackups_{version}/'
                                    .format(version=FILE_STRUCTURE_VERSION)])

    def _s3_upload_pg_cluster_dir(self, start_backup_info, pg_cluster_dir,
                                  version, pool_size):
        """
        Upload to s3_url_prefix from pg_cluster_dir

        This function ignores the directory pg_xlog, which contains WAL
        files and are not generally part of a base backup.

        Note that this is also lzo compresses the files: thus, the number
        of pooled processes involves doing a full sequential scan of the
        uncompressed Postgres heap file that is pipelined into lzo. Once
        lzo is completely finished (necessary to have access to the file
        size) the file is sent to S3.

        TODO: Investigate an optimization to decouple the compression and
        upload steps to make sure that the most efficient possible use of
        pipelining of network and disk resources occurs.  Right now it
        possible to bounce back and forth between bottlenecking on reading
        from the database block device and subsequently the S3 sending
        steps should the processes be at the same stage of the upload
        pipeline: this can have a very negative impact on being able to
        make full use of system resources.

        Furthermore, it desirable to overflowing the page cache: having
        separate tunables for number of simultanious compression jobs
        (which occupy /tmp space and page cache) and number of uploads
        (which affect upload throughput) would help.

        """

        # Get a manifest of files first.
        matches = []

        def raise_walk_error(e):
            raise e

        walker = os.walk(pg_cluster_dir, onerror=raise_walk_error)
        for root, dirnames, filenames in walker:
            # Don't care about WAL, only heap.
            if 'pg_xlog' in dirnames:
                dirnames.remove('pg_xlog')

            for filename in filenames:
                matches.append(os.path.join(root, filename))

        backup_s3_prefix = ('{0}/basebackups_{1}/'
                               'base_{file_name}_{file_offset}'
                               .format(self.s3_prefix, FILE_STRUCTURE_VERSION,
                                       **start_backup_info))

        # absolute upload paths are used for telling lzop what to compress
        local_abspaths = [os.path.abspath(match) for match in matches]

        # computed to subtract out extra extraneous absolute path
        # information when storing on S3
        common_local_prefix = os.path.commonprefix(local_abspaths)

        # A multiprocessing pool to do the uploads with
        pool = multiprocessing.Pool(processes=pool_size)

        # a list to accumulate async upload jobs
        uploads = []

        with self.s3cmd_temp_config as s3cmd_config:

            # Make an attempt to upload extended version metadata
            with tempfile.NamedTemporaryFile(mode='w') as version_tempf:
                version_tempf.write(unicode(version))
                version_tempf.flush()

                check_call_wait_sigint(
                    [S3CMD_BIN, '-c', s3cmd_config.name,
                     '--mime-type=text/plain', 'put',
                     version_tempf.name,
                     backup_s3_prefix + '/extended_version.txt'])

            # Enqueue uploads for parallel execution
            try:
                for local_abspath in local_abspaths:
                    remote_suffix = local_abspath[len(common_local_prefix):]

                    remote_absolute_path = '/'.join(
                        [backup_s3_prefix, 'pgcluster', remote_suffix])

                    uploads.append(pool.apply_async(
                            do_lzop_s3_put,
                            [remote_absolute_path, local_abspath,
                             s3cmd_config.name]))

                pool.close()
            finally:
                # Necessary in case finally block gets hit before
                # .close()
                pool.close()

                while uploads:
                    # XXX: Need timeout to work around Python bug:
                    #
                    # http://bugs.python.org/issue8296
                    uploads.pop().get(1e100)

                pool.join()

        return backup_s3_prefix

    def database_s3_fetch(self, pg_cluster_dir, backup_name, pool_size):

        basebackups_prefix = '/'.join(
            [self.s3_prefix, 'basebackups_' + FILE_STRUCTURE_VERSION])

        with self.s3cmd_temp_config as s3cmd_config:

            # Verify sane looking input for backup_name
            if backup_name == 'LATEST':
                # "LATEST" is a special backup name that is always valid
                # to always find the lexically-largest backup, with the
                # intend of getting the freshest database as soon as
                # possible.

                backup_find = popen_sp(
                    [S3CMD_BIN, '-c', s3cmd_config.name,
                     'ls', basebackups_prefix + '/'],
                    stdout=subprocess.PIPE)
                stdout, stderr = backup_find.communicate()


                sentinel_suffix = '_backup_stop_sentinel.txt'
                # Find sentinel files as markers of guaranteed good backups
                sentinel_urls = []

                for line in (l.strip() for l in stdout.split('\n')):

                    if line.endswith(sentinel_suffix):
                        sentinel_urls.append(line.split()[-1])

                if not sentinel_urls:
                    raise Exception('No base backups found in ' +
                                    basebackups_prefix)
                else:
                    sentinel_urls.sort()

                    # Slice away the extra URL cruft to locate just
                    # the base backup name.
                    #
                    # NB: '... + 1' is for trailing slash
                    begin_slice = len(basebackups_prefix) + 1
                    end_slice = -len(sentinel_suffix)
                    backup_name = sentinel_urls[-1][begin_slice:end_slice]

            base_backup_regexp = (r'base'
                                  r'_(?P<segment>[0-9a-zA-Z.]{0,60})'
                                  r'_(?P<position>[0-9A-F]{8})')
            match = re.match(base_backup_regexp, backup_name)
            if match is None:
                raise Exception('Non-conformant backup name passed: ' +
                                backup_name)

            assert backup_name != 'LATEST', ('Must be rewritten to the actual '
                                             'name of the last base backup')

            backup_s3_cluster_prefix = '/'.join([basebackups_prefix, backup_name,
                                                 'pgcluster']) + '/'

            ls_proc = popen_sp(
                [S3CMD_BIN, '-c', s3cmd_config.name, '--recursive',
                 'ls', backup_s3_cluster_prefix],
                stdout=subprocess.PIPE)
            stdout, stderr = ls_proc.communicate()

            pool = multiprocessing.Pool(processes=pool_size)
            results = []
            try:
                for line in stdout.split('\n'):
                    # Skip any blank lines
                    if not line.strip():
                        continue

                    pos = line.rfind('s3://')
                    if pos > 0:
                        s3_url = line[pos:]
                        assert s3_url.startswith(backup_s3_cluster_prefix)

                        relative_local_path = \
                            s3_url[len(backup_s3_cluster_prefix):-len('.lzo')]

                        assert not relative_local_path.startswith('/')

                        cluster_abspath = os.path.abspath(pg_cluster_dir)
                        complete_abspath = os.path.join(cluster_abspath,
                                                        relative_local_path)

                        assert not (set(complete_abspath.split(os.path.sep)) &
                                    set(['..', '.'])), \
                                    'S3 must not return relative paths'

                        dirpart, filepart = os.path.split(complete_abspath)

                        try:
                            os.makedirs(dirpart)
                        except OSError, e:
                            # file already exists, in this case, for a
                            # directory -- that is probably okay, just
                            # continue
                            if e.errno != 17 or not os.path.isdir(dirpart):
                                raise e

                        results.append(pool.apply_async(
                                do_lzop_s3_get,
                                (s3_url, complete_abspath, s3cmd_config.name)))
                    else:
                        raise Exception('Unexpected s3cmd output: '
                                        'could not find s3:// url')
                pool.close()
            finally:
                # Necessary in case finally block gets hit before
                # .close()
                pool.close()

                while results:
                    # XXX: Need timeout to work around Python bug:
                    #
                    # http://bugs.python.org/issue8296
                    results.pop().get(1e100)

                pool.join()


    def database_s3_backup(self, *args, **kwargs):
        """
        Uploads a PostgreSQL file cluster to S3

        Mechanism: just wraps _s3_upload_pg_cluster_dir with
        start/stop backup actions with exception handling.

        In particular there is a 'finally' block to stop the backup in
        most situations.

        """

        upload_good = False
        backup_stop_good = False
        try:
            start_backup_info = PgBackupStatements.run_start_backup()
            version = PgBackupStatements.pg_version()['version']
            uploaded_to = self._s3_upload_pg_cluster_dir(start_backup_info,
                                                         version=version,
                                                         *args, **kwargs)
            upload_good = True
        finally:
            # XXX: Gross timing hack to get message to appear at the
            # bottom of a terminal.  Better solution: don't spew
            # multiprocessing stack traces everywhere.  This message
            # itself is because of a hypothetical function missing
            # from PostgreSQL: pg_cancel_backup()
            time.sleep(1)

            if not upload_good:
                print >>sys.stderr, ('Blocking on sending WAL segments, even '
                                     'though backup was not completed.  '
                                     'See README: TODO about pg_cancel_backup')
            stop_backup_info = PgBackupStatements.run_stop_backup()
            backup_stop_good = True

        if upload_good and backup_stop_good:
            # Make a best-effort attempt to write a sentinel file to
            # the cluster backup directory that indicates that the
            # base backup upload has definitely run its course (it may
            # have, even without this file, though) and also
            # communicates what WAL segments are needed to get to
            # consistency.
            try:
                with self.s3cmd_temp_config as s3cmd_config:
                    with tempfile.NamedTemporaryFile(mode='w') as sentinel:
                        sentinel.write('{file_name}:{file_offset}\n'
                                       .format(**stop_backup_info))
                        sentinel.flush()

                        # Avoid using do_lzop_s3_put to store
                        # uncompressed: easier to read/double click
                        # on/dump to terminal
                        check_call_wait_sigint(
                            [S3CMD_BIN, '-c', s3cmd_config.name,
                             '--mime-type=text/plain', 'put',
                             sentinel.name,
                             uploaded_to + '_backup_stop_sentinel.txt'])
            except KeyboardInterrupt, e:
                # Specially re-raise exception on SIGINT to allow
                # propagation.
                raise e
            except:
                # Failing to upload the sentinel is not strictly
                # lethal, so ignore any (other) exception.
                pass
        else:
            # NB: Other exceptions should be raised before this that
            # have more informative results, it is intended that this
            # exception never will get raised.
            raise Exception('Could not complete backup process')

    def wal_s3_archive(self, wal_path):
        """
        Uploads a WAL file to S3

        This code is intended to typically be called from Postgres's
        archive_command feature.

        """
        wal_file_name = os.path.basename(wal_path)

        with self.s3cmd_temp_config as s3cmd_config:
            do_lzop_s3_put(
                '{0}/wal_{1}/{2}'.format(self.s3_prefix,
                                         FILE_STRUCTURE_VERSION,
                                         wal_file_name),
                wal_path, s3cmd_config.name)

    def wal_s3_restore(self, wal_name, wal_destination):
        """
        Downloads a WAL file from S3

        This code is intended to typically be called from Postgres's
        restore_command feature.

        NB: Postgres doesn't guarantee that wal_name ==
        basename(wal_path), so both are required.

        """
        with self.s3cmd_temp_config as s3cmd_config:
            do_lzop_s3_get(
                '{0}/wal_{1}/{2}.lzo'.format(self.s3_prefix,
                                             FILE_STRUCTURE_VERSION,
                                             wal_name),
                wal_destination, s3cmd_config.name)


def external_program_check(
    to_check=frozenset([PSQL_BIN, LZOP_BIN, S3CMD_BIN])):
    """
    Validates the existence and basic working-ness of other programs

    Implemented because it is easy to get confusing error output when
    one does not install a dependency because of the fork-worker model
    that is both necessary for throughput and makes more obscure the
    cause of failures.  This is intended to be a time and frustration
    saving measure.  This problem has confused The Author in practice
    when switching rapidly between machines.

    """

    could_not_run = []
    error_msgs = []

    def psql_err_handler(popen):
        assert popen.returncode != 0
        error_msgs.append(textwrap.fill(
                'Could not get a connection to the database: '
                'note that superuser access is required'))

        # Bogus error message that is re-caught and re-raised
        raise Exception('It is also possible that psql is not installed')

    with open(os.devnull, 'w') as nullf:
        for program in to_check:
            try:
                if program is PSQL_BIN:
                    psql_csv_run('SELECT 1', error_handler=psql_err_handler)
                else:
                    subprocess.call([program], stdout=nullf, stderr=nullf)
            except IOError, e:
                could_not_run.append(program)

    if could_not_run:
        error_msgs.append(textwrap.fill(
                'Could not run the following programs, are they installed? ' +
                ', '.join(could_not_run)))


    if error_msgs:
        raise Exception('\n' + '\n'.join(error_msgs))

    return None


def main(argv=None):
    if argv is None:
        argv = sys.argv

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)

    parser.add_argument('-k', '--aws-access-key-id',
                        help='public AWS access key. Can also be defined in an '
                        'environment variable. If both are defined, '
                        'the one defined in the programs arguments takes '
                        'precedence.')

    parser.add_argument('--s3-prefix',
                        help='S3 prefix to run all commands against.  '
                        'Can also be defined via environment variable '
                        'WALE_S3_PREFIX')

    subparsers = parser.add_subparsers(title='subcommands',
                                       dest='subcommand')

    # Common options for backup-fetch and backup-push
    backup_fetchpull_parent = argparse.ArgumentParser(add_help=False)
    backup_fetchpull_parent.add_argument('PG_CLUSTER_DIRECTORY',
                                         help="Postgres cluster path, "
                                         "such as '/var/lib/database'")
    backup_fetchpull_parent.add_argument('--pool-size', '-p',
                                         type=int, default=6,
                                         help='Download pooling size')

    wal_fetchpull_parent = argparse.ArgumentParser(add_help=False)
    wal_fetchpull_parent.add_argument('WAL_SEGMENT',
                                      help='Path to a WAL segment to upload')

    backup_fetch_parser = subparsers.add_parser(
        'backup-fetch', help='fetch a hot backup from S3',
        parents=[backup_fetchpull_parent])
    backup_list_parser = subparsers.add_parser(
        'backup-list', help='list backups in S3')
    backup_push_parser = subparsers.add_parser(
        'backup-push', help='pushing a fresh hot backup to S3',
        parents=[backup_fetchpull_parent])
    recovery_conf_generate_parser = subparsers.add_parser(
        'recovery-conf-generator', help='help generating recovery.conf')
    wal_fetch_parser = subparsers.add_parser(
        'wal-fetch', help='fetch a WAL file from S3',
        parents=[wal_fetchpull_parent])
    wal_push_parser = subparsers.add_parser(
        'wal-push', help='push a WAL file to S3',
        parents=[wal_fetchpull_parent])


    # backup-fetch operator section
    backup_fetch_parser.add_argument('BACKUP_NAME',
                                     help='the name of the backup to fetch')

    # recovery conf generator section
    recovery_conf_generate_parser.add_argument(
        '--python-bin', default='python', nargs='?',
        help='the Python binary to run wal-e with.'
        'Example: "python2.6"')

    recovery_conf_generate_parser.add_argument(
        'RECOVERY_OUTPUT_FILE',
        type=argparse.FileType('w'), nargs='?', default=sys.stdout,
        help='the destination of the recovery.conf to write, '
        'or \'-\' for stdout.')

    timeline_recovery_group = (recovery_conf_generate_parser
                               .add_mutually_exclusive_group())
    timeline_recovery_group.add_argument(
        '-t', '--target-time',
        help='Provide a time for Postgres to recover to.  '
        'Any Postgres-compatible time format is allowed.')
    timeline_recovery_group.add_argument(
        '-x', '--target-xid',
        type=int, help='Provide a transaction id for Postgres to recover to.')

    # wal-push operator section
    wal_fetch_parser.add_argument('WAL_DESTINATION',
                                 help='Path to download the WAL segment to')

    args = parser.parse_args()

    secret_key = os.getenv('AWS_SECRET_ACCESS_KEY')
    if secret_key is None:
        print >>sys.stderr, ('Must define AWS_SECRET_ACCESS_KEY ask S3 to do '
                             'anything')
        sys.exit(1)

    s3_prefix = args.s3_prefix or os.getenv('WALE_S3_PREFIX')

    if s3_prefix is None:
        print >>sys.stderr, ('Must pass --s3-prefix or define environment '
                             'variable WALE_S3_PREFIX')
        sys.exit(1)

    if args.aws_access_key_id is None:
        aws_access_key_id = os.getenv('AWS_ACCESS_KEY_ID')
        if aws_access_key_id is None:
            print >>sys.stderr, ('Must define an AWS_ACCESS_KEY_ID, '
                                 'using environment variable or '
                                 '--aws_access_key_id')

    else:
        aws_access_key_id = args.aws_access_key_id

    backup_cxt = S3Backup(aws_access_key_id, secret_key, s3_prefix)

    subcommand = args.subcommand

    if subcommand == 'backup-fetch':
        external_program_check([S3CMD_BIN, LZOP_BIN])
        backup_cxt.database_s3_fetch(args.PG_CLUSTER_DIRECTORY,
                                     args.BACKUP_NAME,
                                     pool_size=args.pool_size)
    elif subcommand == 'backup-list':
        external_program_check([S3CMD_BIN])
        backup_cxt.backup_list()
    elif subcommand == 'backup-push':
        external_program_check([S3CMD_BIN, LZOP_BIN, PSQL_BIN])
        backup_cxt.database_s3_backup(args.PG_CLUSTER_DIRECTORY,
                                      pool_size=args.pool_size)
    elif subcommand == 'wal-fetch':
        external_program_check([S3CMD_BIN, LZOP_BIN])
        backup_cxt.wal_s3_restore(args.WAL_SEGMENT, args.WAL_DESTINATION)
    elif subcommand == 'wal-push':
        external_program_check([S3CMD_BIN, LZOP_BIN])
        backup_cxt.wal_s3_archive(args.WAL_SEGMENT)
    elif subcommand == 'recovery-conf-generator':
        this_bin = os.path.abspath(argv[0])
        command = ('{python} {wal_e} --aws-access-key-id={aws_access_key_id} '
                   '--s3-prefix={s3_prefix} wal-fetch "%f" "%p"'
                   .format(python=args.python_bin, wal_e=this_bin,
                           aws_access_key_id=aws_access_key_id,
                           s3_prefix=s3_prefix))

        lines = []
        lines.append("restore_command = '{0}'".format(command))

        if args.target_time is not None:
            assert ('"' not in args.point_in_time and
                    "'" not in args.point_in_time)
            lines.append("recovery_target_time = '{0}'"
                         .format(args.point_in_time))
        elif args.target_xid is not None:
            # No sanitization necessary: argparse knows this is an
            # integer.
            lines.append("recovery_target_xid = '{0}'"
                         .format(args.target_xid))

        print >>args.RECOVERY_OUTPUT_FILE, '\n'.join(lines)
    else:
        print >>sys.stderr, ('Subcommand {0} not implemented!'
                             .format(subcommand))
        sys.exit(127)

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
import errno
import os
import stat
import argparse
import sqlite3
import time
import logging
import datetime

from contextlib import closing
from collections import namedtuple


logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')

logger = logging.getLogger('cleanup')


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY,
                                  isdir INTEGER,
                                  atime REAL,
                                  ctime REAL,
                                  mtime REAL,
                                  size INTEGER);
"""

INSERT_OR_REPLACE = """
INSERT OR REPLACE INTO files (path, isdir, atime, ctime, mtime, size)
VALUES(?,?,?,?,?,?);
"""

COUNT = """
SELECT COUNT(*) FROM files;
"""

SELECT = """
SELECT * FROM files ORDER BY atime LIMIT ?
"""

DELETE = """
DELETE FROM files WHERE path=?
"""

SELECT_PATH = """
SELECT * FROM files WHERE path=?;
"""

DB_DIR = "/tmp"


def bytes_to_string(n):
    u = ['', 'K', 'M', 'G', 'T', 'P']
    i = 0
    while n >= 1024:
        n /= 1024.0
        i += 1
    return "%g%s" % (int(n * 10 + 0.5) / 10.0, u[i])


def second_to_string(n):
    if n is None:
        return '???'
    return str(datetime.datetime.now() - n)


def df(name):
    fs = os.statvfs(name)
    size = fs.f_blocks * fs.f_bsize
    used = int((1.0 - float(fs.f_bavail) / float(fs.f_blocks)) * 100 + 0.5)
    return (used, size)


def mount_point(path):
    ret = os.path.realpath(path)
    while not os.path.ismount(ret):
        ret = os.path.dirname(ret)
    return ret


FileStat = namedtuple('FileStat', ['path',
                                   'isdir',
                                   'atime',
                                   'ctime',
                                   'mtime',
                                   'size'])


def needs_update(oldstat, newstat):
    if oldstat.isdir != newstat.isdir:
        return True

    # Make sure we don't consired accessed directories as changed
    # as a simple 'ls' with change the
    if oldstat.isdir:
        if (oldstat.mtime, oldstat.ctime) != (newstat.mtime, newstat.ctime):
            return True

    return oldstat != newstat


def stat_file(full):
    s = None

    try:
        s = os.lstat(full)
    except Exception as e:
        logger.info("Cannot lstat(%s); %s" % (full, e))
        return None

    return FileStat(full,
                    stat.S_ISDIR(s.st_mode),
                    s.st_atime,
                    s.st_ctime,
                    s.st_mtime,
                    s.st_size)


def database(path):
    path = os.path.realpath(path)
    m = mount_point(path)
    name = m.replace("/", "_")
    base = os.path.join(DB_DIR, "".join(["cleanup", name, ".sqlite3"]))

    logger.debug("Database is %s", base)

    conn = sqlite3.connect(base)
    cursor = conn.cursor()
    cursor.execute(CREATE_TABLE)
    return conn


def number_of_files(cursor):
    cursor.execute(COUNT)
    return list(cursor)[0][0]


##########################################################

def _scan(path, cursor):

    files = []

    def flush(files):
        cursor.executemany(INSERT_OR_REPLACE, files)
        files = []

    for filename in os.listdir(path):

        if filename in {".", ".."}:
            continue

        full = os.path.join(path, filename)
        s = stat_file(full)
        if s is None:
            continue

        if len(files) >= 10000:
            flush(files)
            files = []

        files.append(s)

        if s.isdir:
            _scan(full, cursor)

    flush(files)


def scan(path):
    with closing(database(path)) as conn:
        cursor = conn.cursor()

        before = number_of_files(cursor)

        _scan(os.path.realpath(path), cursor)

        after = number_of_files(cursor)

        conn.commit()

        logger.info("scan(%s): Added %d entries to the files database" % (path, after - before))
        return after - before

##########################################################


def _quick_scan(path, cursor, limit):

    logger.debug("quick_scan %s" % (path,))

    for filename in os.listdir(path):

        if filename in {".", ".."}:
            continue

        if time.time() > limit:
            logger.info("quick_scan time limit reached")
            return

        full = os.path.join(path, filename)
        newstat = stat_file(full)
        if newstat is None:
            continue

        cursor.execute(SELECT_PATH, (full,))

        for row in cursor:
            oldstat = FileStat(*row)
            if not needs_update(oldstat, newstat):
                # Skip for speed, this will miss new files in deep directories
                return

        if newstat.isdir:
            _quick_scan(full, cursor, limit)

        logger.debug("quick_scan add %s" % (full,))
        cursor.execute(INSERT_OR_REPLACE, newstat)


def quick_scan(path):
    with closing(database(path)) as conn:
        cursor = conn.cursor()

        now = time.time()

        # Should run under a minute to give chance to later cron to run
        _quick_scan(os.path.realpath(path), cursor, now + 40)

        logger.debug("quick_scan done in %s seconds" % (time.time() - now))

        conn.commit()


def delete_file(oldstat):

    newstat = stat_file(oldstat.path)

    if newstat is None:
        return None

    if needs_update(oldstat, newstat):
        # The file has been modified since last scan
        logger.info("%s has changed to %s" % (oldstat, newstat))
        return newstat

    try:
        if oldstat.isdir:
            os.rmdir(oldstat.path)
        else:
            os.unlink(oldstat.path)
            try:
                # This is a workaround around a bug in caching
                # by the AdaptorURL
                os.rmdir(os.path.dirname(oldstat.path))
            except Exception:
                pass

    except OSError as err:
        if err.errno in (errno.ENOENT, errno.ENOTEMPTY):
            # Do not complain if file vanished. or the directory is still full
            pass
        else:
            logger.info("Error removing %s %s" % (oldstat.path, err))
    except Exception as err:
        logger.info("Error removing %s %s" % (oldstat.path, err))

    return None


def _delete_files(cursor, target, maximum):

    to_delete = []
    to_update = []

    before = number_of_files(cursor)
    total = 0
    oldest = None
    youngest = None

    cursor.execute(SELECT, (maximum,))
    for row in cursor:
        row = FileStat(*row)
        s = delete_file(row)
        if s is None:
            to_delete.append((row.path,))
            total += row.size
            if oldest is None:
                youngest = oldest = datetime.datetime.fromtimestamp(int(row.atime))
            else:
                oldest = min(oldest, datetime.datetime.fromtimestamp(int(row.atime)))
                youngest = max(youngest, datetime.datetime.fromtimestamp(int(row.atime)))
        else:
            to_update.append(s)

        if total >= target:
            break

    cursor.executemany(INSERT_OR_REPLACE, to_update)
    cursor.executemany(DELETE, to_delete)

    after = number_of_files(cursor)

    return before - after, total, oldest, youngest


def delete_files(path, target, maximum):

    logger.info("Deleting %s from %s" % (bytes_to_string(target), path))

    with closing(database(path)) as conn:
        cursor = conn.cursor()
        count, volume, oldest, youngest = _delete_files(cursor, target, maximum)
        logger.info("(%s): Deleted %d entries, size: %s, oldest: %s, youngest: %s" % (path,
                                                                                      count,
                                                                                      bytes_to_string(volume),
                                                                                      second_to_string(oldest),
                                                                                      second_to_string(youngest)))
        conn.commit()

    return count


def process(path, high, low, max):
    used, total = df(path)

    if used > high:
        logger.info("%s: %d%% (total %s)" % (path, used, bytes_to_string(total)))
        while used > low:
            if delete_files(path, total * (used - low) / 100, max) == 0:
                if scan(path) == 0:
                    logger.warning("No files deleted under %s, "
                                   "and no candidates for deletion found" % (path,))
                    break
            used, total = df(path)
            logger.info("%s: %d%%" % (path, used))
    else:
        quick_scan(path)


parser = argparse.ArgumentParser(description='cleanup.')
# parser.add_argument('--timeout', type=int, default=60)

parser.add_argument('--high', type=int, default=70)
parser.add_argument('--low', type=int, default=70)
parser.add_argument('--max', type=int, default=10000)
parser.add_argument('--scan', action='store_true')
parser.add_argument('--test-run', action='store_true')


parser.add_argument('path', nargs='+')
args = parser.parse_args()
print(f"{args}")
if not args.test_run:
    for p in args.path:
        if args.scan:
            scan(p)
        else:
            process(p, args.high, args.low, args.max)
else:
    print("Test run, script loaded successfully")


#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

from subprocess import Popen, PIPE
import json
import time
import cherrypy
import lazylibrarian
from lazylibrarian import logger, database
from lazylibrarian.formatter import unaccented, getList
from lazylibrarian.importer import addAuthorNameToDB, search_for, import_book
from lazylibrarian.librarysync import find_book_in_db
from lib.fuzzywuzzy import fuzz

# calibredb custom_columns
# calibredb add_custom_column label name bool
# calibredb remove_custom_column --force label
# calibredb set_custom label id value
# calibredb search "#label":"false"  # returns list of ids (slow)


def calibreList(col_read, col_toread):
    """ Get a list from calibre of all books in its library, including optional 'read' and 'toread' columns
        If success, return list of dicts {"title": "", "id": 0, "authors": ""}
        The "read" and "toread" columns are passed as column names so they can be per-user and may not be present.
        Can be true, false, or empty in which case not included in dict. We only use the "true" state
        If error, return error message (not a dict) """

    fieldlist = 'title,authors'
    if col_read:
        fieldlist += ',*' + col_read
    if col_toread:
        fieldlist += ',*' + col_toread
    res, err, rc = calibredb("list", "", ['--for-machine', '--fields', fieldlist])
    if rc:
        if res:
            return res
        return err
    else:
        return json.loads(res)


def syncCalibreList(col_read=None, col_toread=None, userid=None):
    """ Get the lazylibrarian bookid for each read/toread calibre book so we can map our id to theirs,
        and sync current/supplied user's read/toread or supplied read/toread columns to calibre database.
        Return message giving totals """

    myDB = database.DBConnection()
    if not userid:
        cookie = cherrypy.request.cookie
        if cookie and 'll_uid' in cookie.keys():
            userid = cookie['ll_uid'].value
    if userid:
        res = myDB.match('SELECT UserName,ToRead,HaveRead,CalibreRead,CalibreToRead,Perms from users where UserID=?',
                         (userid,))
        if res:
            username = res['UserName']
            if not col_read:
                col_read = res['CalibreRead']
            if not col_toread:
                col_toread = res['CalibreToRead']
            toreadlist = getList(res['ToRead'])
            readlist = getList(res['HaveRead'])
            # suppress duplicates (just in case)
            toreadlist = list(set(toreadlist))
            readlist = list(set(readlist))
        else:
            return "Error: Unable to get user column settings for %s" % userid

    if not userid:
        return "Error: Unable to find current userid"

    if not col_read and not col_toread:
        return "User %s has no calibre columns set" % username

    # check user columns exist in calibre and create if not
    res = calibredb('custom_columns')
    columns = res[0].split('\n')
    custom_columns = []
    for column in columns:
        if column:
            custom_columns.append(column.split(' (')[0])

    if col_read not in custom_columns:
        added = calibredb('add_custom_column', [col_read, col_read, 'bool'])
        if "column created" not in added[0]:
            return added
    if col_toread not in custom_columns:
        added = calibredb('add_custom_column', [col_toread, col_toread, 'bool'])
        if "column created" not in added[0]:
            return added

    nomatch = 0
    readcol = ''
    toreadcol = ''
    map_ctol = {}
    map_ltoc = {}
    if col_read:
        readcol = '*' + col_read
    if col_toread:
        toreadcol = '*' + col_toread

    calibre_list = calibreList(col_read, col_toread)
    if not isinstance(calibre_list, list):
        # got an error message from calibredb
        return '"%s"' % calibre_list

    for item in calibre_list:
        if toreadcol and toreadcol in item or readcol and readcol in item:
            authorname, authorid, added = addAuthorNameToDB(item['authors'], refresh=False, addbooks=False)
            if authorname:
                if authorname != item['authors']:
                    logger.debug("Changed authorname for [%s] from [%s] to [%s]" %
                                 (item['title'], item['authors'], authorname))
                    item['authors'] = authorname
                bookid = find_book_in_db(authorname, item['title'])
                if not bookid:
                    searchterm = "%s <ll> %s" % (item['title'], authorname)
                    results = search_for(unaccented(searchterm))
                    if results:
                        result = results[0]
                        if result['author_fuzz'] > lazylibrarian.CONFIG['MATCH_RATIO'] \
                                and result['book_fuzz'] > lazylibrarian.CONFIG['MATCH_RATIO']:
                            logger.debug("Found (%s%% %s%%) %s: %s" % (result['author_fuzz'], result['book_fuzz'],
                                                                       result['authorname'], result['bookname']))
                            bookid = result['bookid']
                            import_book(bookid)
                if bookid:
                    # NOTE: calibre bookid is always an integer, lazylibrarian bookid is a string
                    # (goodreads could be used as an int, but googlebooks can't as it's alphanumeric)
                    # so convert all dict items to strings for ease of matching.
                    map_ctol[str(item['id'])] = str(bookid)
                    map_ltoc[str(bookid)] = str(item['id'])
                else:
                    logger.warn('Calibre Book [%s] by [%s] is not in lazylibrarian database' %
                                (item['title'], authorname))
                    nomatch += 1
            else:
                logger.warn('Calibre Author [%s] not matched in lazylibrarian database' % (item['authors']))
                nomatch += 1

    # Now check current users lazylibrarian read/toread against the calibre library, warn about missing ones
    # which might be books calibre doesn't have, or might be minor differences in author or title

    for idlist in [("Read", readlist), ("To_Read", toreadlist)]:
        booklist = idlist[1]
        for bookid in booklist:
            cmd = "SELECT AuthorID,BookName from books where BookID=?"
            book = myDB.match(cmd, (bookid,))
            if not book:
                logger.error('Error finding bookid %s' % bookid)
            else:
                cmd = "SELECT AuthorName from authors where AuthorID=?"
                author = myDB.match(cmd, (book['AuthorID'],))
                if not author:
                    logger.error('Error finding authorid %s' % book['AuthorID'])
                else:
                    match = False
                    for item in calibre_list:
                        if item['authors'] == author['AuthorName'] and item['title'] == book['BookName']:
                            logger.debug("Exact match for %s [%s]" % (idlist[0], book['BookName']))
                            map_ctol[str(item['id'])] = str(bookid)
                            map_ltoc[str(bookid)] = str(item['id'])
                            match = True
                            break
                    if not match:
                        high = 0
                        highname = ''
                        highid = ''
                        for item in calibre_list:
                            if item['authors'] == author['AuthorName']:
                                n = fuzz.token_sort_ratio(item['title'], book['BookName'])
                                if n > high:
                                    high = n
                                    highname = item['title']
                                    highid = item['id']

                        if high > 95:
                            logger.debug("Found ratio match %s%% [%s] for %s [%s]" %
                                         (high, highname, idlist[0], book['BookName']))
                            map_ctol[str(highid)] = str(bookid)
                            map_ltoc[str(bookid)] = str(highid)
                            match = True

                    if not match:
                        logger.warn("No match for %s %s by %s in calibre database, closest match %s%% [%s]" %
                                    (idlist[0], book['BookName'], author['AuthorName'], high, highname))
                        nomatch += 1

    logger.debug("BookID mapping complete, %s match %s, nomatch %s" % (username, len(map_ctol), nomatch))

    # now sync the lists
    if userid:
        last_read = []
        last_toread = []
        calibre_read = []
        calibre_toread = []

        cmd = 'select SyncList from sync where UserID=? and Label=?'
        res = myDB.match(cmd, (userid, col_read))
        if res:
            last_read = getList(res['SyncList'])
        res = myDB.match(cmd, (userid, col_toread))
        if res:
            last_toread = getList(res['SyncList'])

        for item in calibre_list:
            if toreadcol and toreadcol in item and item[toreadcol]:  # only if True
                if str(item['id']) in map_ctol:
                    calibre_toread.append(map_ctol[str(item['id'])])
                else:
                    logger.warn("Calibre to_read book %s:%s has no lazylibrarian bookid" %
                                (item['authors'], item['title']))
            if readcol and readcol in item and item[readcol]:  # only if True
                if str(item['id']) in map_ctol:
                    calibre_read.append(map_ctol[str(item['id'])])
                else:
                    logger.warn("Calibre read book %s:%s has no lazylibrarian bookid" %
                                (item['authors'], item['title']))

        logger.debug("Found %s calibre read, %s calibre toread" % (len(calibre_read), len(calibre_toread)))
        logger.debug("Found %s lazylib read, %s lazylib toread" % (len(readlist), len(toreadlist)))

        added_to_ll_toread = list(set(toreadlist) - set(last_toread))
        removed_from_ll_toread = list(set(last_toread) - set(toreadlist))
        added_to_ll_read = list(set(readlist) - set(last_read))
        removed_from_ll_read = list(set(last_read) - set(readlist))
        logger.debug("lazylibrarian changes to copy to calibre: %s %s %s %s" % (len(added_to_ll_toread),
                     len(removed_from_ll_toread), len(added_to_ll_read), len(removed_from_ll_read)))

        added_to_calibre_toread = list(set(calibre_toread) - set(last_toread))
        removed_from_calibre_toread = list(set(last_toread) - set(calibre_toread))
        added_to_calibre_read = list(set(calibre_read) - set(last_read))
        removed_from_calibre_read = list(set(last_read) - set(calibre_read))
        logger.debug("calibre changes to copy to lazylibrarian: %s %s %s %s" % (len(added_to_calibre_toread),
                     len(removed_from_calibre_toread), len(added_to_calibre_read), len(removed_from_calibre_read)))

        calibre_changes = 0
        for item in added_to_calibre_read:
            if item not in readlist:
                readlist.append(item)
                logger.debug("Lazylibrarian marked %s as read" % item)
                calibre_changes += 1
        for item in added_to_calibre_toread:
            if item not in toreadlist:
                toreadlist.append(item)
                logger.debug("Lazylibrarian marked %s as to_read" % item)
                calibre_changes += 1
        for item in removed_from_calibre_read:
            if item in readlist:
                readlist.remove(item)
                logger.debug("Lazylibrarian removed %s from read" % item)
                calibre_changes += 1
        for item in removed_from_calibre_toread:
            if item in toreadlist:
                toreadlist.remove(item)
                logger.debug("Lazylibrarian removed %s from to_read" % item)
                calibre_changes += 1
        if calibre_changes:
            myDB.action('UPDATE users SET ToRead=?,HaveRead=? WHERE UserID=?',
                        (', '.join(toreadlist), ', '.join(readlist), userid))
        ll_changes = 0
        for item in added_to_ll_toread:
            if item in map_ltoc:
                res, err, rc = calibredb('set_custom', [col_toread, map_ltoc[item], 'true'], [])
                if rc:
                    msg = "calibredb set_custom error: "
                    if err:
                        logger.error(msg + err)
                    elif res:
                        logger.error(msg + res)
                    else:
                        logger.error(msg + str(rc))
                else:
                    ll_changes += 1
            else:
                logger.warn("Unable to set calibre %s true for %s" % (col_toread, item))
        for item in removed_from_ll_toread:
            if item in map_ltoc:
                res, err, rc = calibredb('set_custom', [col_toread, map_ltoc[item], ''], [])
                if rc:
                    msg = "calibredb set_custom error: "
                    if err:
                        logger.error(msg + err)
                    elif res:
                        logger.error(msg + res)
                    else:
                        logger.error(msg + str(rc))
                else:
                    ll_changes += 1
            else:
                logger.warn("Unable to clear calibre %s for %s" % (col_toread, item))

        for item in added_to_ll_read:
            if item in map_ltoc:
                res, err, rc = calibredb('set_custom', [col_read, map_ltoc[item], 'true'], [])
                if rc:
                    msg = "calibredb set_custom error: "
                    if err:
                        logger.error(msg + err)
                    elif res:
                        logger.error(msg + res)
                    else:
                        logger.error(msg + str(rc))
                else:
                    ll_changes += 1
            else:
                logger.warn("Unable to set calibre %s true for %s" % (col_read, item))

        for item in removed_from_ll_read:
            if item in map_ltoc:
                res, err, rc = calibredb('set_custom', [col_read, map_ltoc[item], ''], [])
                if rc:
                    msg = "calibredb set_custom error: "
                    if err:
                        logger.error(msg + err)
                    elif res:
                        logger.error(msg + res)
                    else:
                        logger.error(msg + str(rc))
                else:
                    ll_changes += 1
            else:
                logger.warn("Unable to clear calibre %s for %s" % (col_read, item))

        # store current sync list as comparison for next sync
        controlValueDict = {"UserID": userid, "Label": col_read}
        newValueDict = {"Date": str(time.time()), "Synclist": ', '.join(readlist)}
        myDB.upsert("sync", newValueDict, controlValueDict)
        controlValueDict = {"UserID": userid, "Label": col_toread}
        newValueDict = {"Date": str(time.time()), "Synclist": ', '.join(toreadlist)}
        myDB.upsert("sync", newValueDict, controlValueDict)

        msg = "%s sync updated: %s calibre, %s lazylibrarian" % (username, ll_changes, calibre_changes)
    return msg


def calibreTest():
    res, err, rc = calibredb('--version')
    if rc:
        msg = "calibredb communication failed: "
        if err:
            return msg + err
        return msg + res

    res = res.strip('\n')
    if '(calibre ' in res and res.endswith(')'):
        # extract calibredb version number
        res = res.split('(calibre ')[1]
        res = 'calibredb ok, version ' + res[:-1]
        # get a list of categories and counters from the database
        cats, err, rc = calibredb('list_categories', ['-i'])
        cnt = 0
        if rc:
            res = res + '\nDatabase READ Failed'
        else:
            res = res + '\nDatabase: ' + err
            for entry in cats.split('\n'):
                try:
                    words = entry.split()
                    if words[-1].isdigit():
                        cnt += int(words[-1])
                except IndexError:
                    cnt += 0
        if cnt:
            res = res + '\nDatabase READ ok'
            wrt, err, rc = calibredb('add', ['--authors', 'LazyLibrarian', '--title', 'dummy', '--empty'], [])
            if 'Added book ids' not in wrt:
                res = res + '\nDatabase WRITE Failed'
            else:
                calibre_id = wrt.split("book ids: ", 1)[1].split("\n", 1)[0]
                rmv, err, rc = calibredb('remove', ['--permanent', calibre_id], [])
                if not rc:
                    res = res + '\nDatabase WRITE ok'
                else:
                    res = res + '\nDatabase WRITE2 Failed: '
        else:
            res = res + '\nDatabase READ Failed or database is empty'
    else:
        res = 'calibredb Failed'
    return res


def calibredb(cmd=None, prelib=None, postlib=None):
    """ calibre-server needs to be started with --enable-auth and needs user/password to add/remove books
        only basic features are available without auth. calibre_server should look like  http://address:port/#library
        default library is used if no #library in the url
        or calibredb can talk to the database file as long as there is no running calibre """

    if not lazylibrarian.CONFIG['IMP_CALIBREDB']:
        return "No calibredb set in config", '', 1

    params = [lazylibrarian.CONFIG['IMP_CALIBREDB'], cmd]
    if lazylibrarian.CONFIG['CALIBRE_USE_SERVER']:
        dest_url = lazylibrarian.CONFIG['CALIBRE_SERVER']
        if lazylibrarian.CONFIG['CALIBRE_USER'] and lazylibrarian.CONFIG['CALIBRE_PASS']:
            params.extend(['--username', lazylibrarian.CONFIG['CALIBRE_USER'],
                           '--password', lazylibrarian.CONFIG['CALIBRE_PASS']])
    else:
        dest_url = lazylibrarian.DIRECTORY('eBook')
    if prelib:
        params.extend(prelib)

    if cmd != "--version":
        params.extend(['--with-library', '%s' % dest_url])
    if postlib:
        params.extend(postlib)
    logger.debug(str(params))
    res = ''

    try:
        p = Popen(params, stdout=PIPE, stderr=PIPE)
        res, err = p.communicate()
        rc = p.returncode
        if rc:
            if 'Errno 111' in err:
                logger.debug("calibredb returned %s: Connection refused" % rc)
            else:
                logger.debug("calibredb returned %s: res[%s] err[%s]" % (rc, res, err))
    except Exception as e:
        err = "calibredb exception: %s %s" % (type(e).__name__, str(e))
        logger.debug(err)
        rc = 1

    if rc and dest_url.startswith('http') and not res.startswith('Forbidden'):
        # if not forbidden (auth issue), might be no server running, retry using file
        params = [lazylibrarian.CONFIG['IMP_CALIBREDB'], cmd]
        if prelib:
            params.extend(prelib)
        dest_url = lazylibrarian.DIRECTORY('eBook')
        params.extend(['--with-library', dest_url])
        if postlib:
            params.extend(postlib)
        logger.debug(str(params))
        try:
            q = Popen(params, stdout=PIPE, stderr=PIPE)
            res, err = q.communicate()
            rc = q.returncode
            if rc:
                logger.debug("calibredb retry returned %s: res[%s] err[%s]" % (rc, res, err))
        except Exception as e:
            err = "calibredb retry exception: %s %s" % (type(e).__name__, str(e))
            logger.debug(err)
            rc = 1
    if rc:
        return res, err, rc
    else:
        return res, dest_url, 0

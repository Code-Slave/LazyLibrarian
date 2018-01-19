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

import os
import shutil
import time
import urllib
import traceback
import lib.id3reader as id3reader

import lazylibrarian
from lazylibrarian import logger, database
from lazylibrarian.cache import cache_img, fetchURL, get_xml_request
from lazylibrarian.formatter import safe_unicode, plural, cleanName, unaccented, formatAuthorName, \
    is_valid_booktype, check_int, getList, replace_all, makeUnicode, makeBytestr
from lib.fuzzywuzzy import fuzz


# Need to remove characters we don't want in the filename BEFORE adding to EBOOK_DIR
# as windows drive identifiers have colon, eg c:  but no colons allowed elsewhere?
__dic__ = {'<': '', '>': '', '...': '', ' & ': ' ', ' = ': ' ', '?': '', '$': 's',
           ' + ': ' ', '"': '', ',': '', '*': '', ':': '', ';': '', '\'': '', '//': '/', '\\\\': '\\'}


def audioRename(bookid):
    for item in ['$Part', '$Title']:
        if item not in lazylibrarian.CONFIG['AUDIOBOOK_DEST_FILE']:
            logger.error("Unable to audioRename, check AUDIOBOOK_DEST_FILE")
            return ''

    myDB = database.DBConnection()
    cmd = 'select AuthorName,BookName,AudioFile from books,authors where books.AuthorID = authors.AuthorID and bookid=?'
    exists = myDB.match(cmd, (bookid,))
    if exists:
        book_filename = exists['AudioFile']
        if book_filename:
            r = os.path.dirname(book_filename)
        else:
            logger.debug("No filename for %s in audioRename %s" % bookid)
            return ''
    else:
        logger.debug("Invalid bookid in audioRename %s" % bookid)
        return ''

    cnt = 0
    parts = []
    author = ''
    book = ''
    track = ''
    audio_file = ''
    for f in os.listdir(makeBytestr(r)):
        f = makeUnicode(f)
        if is_valid_booktype(f, booktype='audiobook'):
            cnt += 1
            audio_file = f
            try:
                id3r = id3reader.Reader(os.path.join(r, f))
                performer = id3r.getValue('performer')
                composer = id3r.getValue('TCOM')
                book = id3r.getValue('album')
                track = id3r.getValue('track')

                if not track:
                    track = '0'
                if composer:  # if present, should be author
                    author = composer
                elif performer:  # author, or narrator if composer == author
                    author = performer
                if author and book:
                    parts.append([track, book, author, f])
            except Exception as e:
                logger.debug("id3reader %s %s" % (type(e).__name__, str(e)))
                pass

    logger.debug("%s found %s audiofile%s" % (exists['BookName'], cnt, plural(cnt)))

    if cnt == 1 and not parts:  # single file audiobook
        parts = ['1', exists['BookName'], exists['AuthorName'], audio_file]

    if cnt != len(parts):
        logger.warn("%s: Incorrect number of parts (found %i from %i)" % (exists['BookName'], len(parts), cnt))
        return book_filename

    # does the track include total (eg 1/12)
    if '/' in track:
        a, b = track.split('/')
        if check_int(b, 0) and check_int(b, 0) != cnt:
            logger.warn("%s: Expected %s parts, got %i" % (exists['BookName'], b, cnt))
            return book_filename

    # check all parts have the same author and title
    if len(parts) > 1:
        for part in parts:
            if part[1] != book:
                logger.warn("%s: Inconsistent title: [%s][%s]" % (exists['BookName'], part[1], book))
                return book_filename
            if part[2] != author:
                logger.warn("%s: Inconsistent author: [%s][%s]" % (exists['BookName'], part[2], author))
                return book_filename

    # strip out just part number
    for part in parts:
        if '/' in part[0]:
            part[0] = part[0].split('/')[0]

    # do we have any track info (value is 0 if not)
    if check_int(parts[0][0], 0) == 0:
        tokmatch = ''
        # try to extract part information from filename. Search for token style of part 1 in this order...
        for token in [' 001.', ' 01.', ' 1.', ' 001 ', ' 01 ', ' 1 ', '01']:
            if tokmatch:
                break
            for part in parts:
                if token in part[3]:
                    tokmatch = token
                    break
        if tokmatch:  # we know the numbering style, get numbers for the other parts
            cnt = 0
            while cnt < len(parts):
                cnt += 1
                if tokmatch == ' 001.':
                    pattern = ' %s.' % str(cnt).zfill(3)
                elif tokmatch == ' 01.':
                    pattern = ' %s.' % str(cnt).zfill(2)
                elif tokmatch == ' 1.':
                    pattern = ' %s.' % str(cnt)
                elif tokmatch == ' 001 ':
                    pattern = ' %s ' % str(cnt).zfill(3)
                elif tokmatch == ' 01 ':
                    pattern = ' %s ' % str(cnt).zfill(2)
                elif tokmatch == ' 1 ':
                    pattern = ' %s ' % str(cnt)
                else:
                    pattern = '%s' % str(cnt).zfill(2)
                # standardise numbering of the parts
                for part in parts:
                    if pattern in part[3]:
                        part[0] = str(cnt)
                        break
    # check all parts are present
    cnt = 0
    found = True
    while found and cnt < len(parts):
        found = False
        cnt += 1
        for part in parts:
            trk = check_int(part[0], 0)
            if trk == cnt:
                found = True
                break
        if not found:
            logger.warn("%s: No part %i found" % (exists['BookName'], cnt))
            return book_filename

    # if we get here, looks like we have all the parts needed to rename properly

    dest_path = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace(
        '$Author', author).replace(
        '$Title', book).replace(
        '$Series', seriesInfo(bookid)).replace(
        '$SerName', seriesInfo(bookid, 'Name')).replace(
        '$SerNum', seriesInfo(bookid, 'Num')).replace(
        '$$', ' ')
    dest_path = ' '.join(dest_path.split()).strip()
    dest_path = replace_all(dest_path, __dic__)
    dest_dir = lazylibrarian.DIRECTORY('Audio')
    dest_path = os.path.join(dest_dir, dest_path)
    if r != dest_path:
        try:
            shutil.move(r, dest_path)
            r = dest_path
        except Exception as why:
            if not os.path.isdir(dest_path):
                logger.debug('Unable to create directory %s: %s' % (dest_path, why))

    for part in parts:
        pattern = lazylibrarian.CONFIG['AUDIOBOOK_DEST_FILE']
        pattern = pattern.replace(
            '$Author', author).replace(
            '$Title', book).replace(
            '$Part', part[0].zfill(len(str(len(parts))))).replace(
            '$Total', str(len(parts))).replace(
            '$Series', seriesInfo(bookid)).replace(
            '$SerName', seriesInfo(bookid, 'Name')).replace(
            '$SerNum', seriesInfo(bookid, 'Num')).replace(
            '$$', ' ')
        pattern = ' '.join(pattern.split()).strip()

        n = os.path.join(r, pattern + os.path.splitext(part[3])[1])
        o = os.path.join(r, part[3])
        if o != n:
            try:
                shutil.move(o, n)
                if check_int(part[0], 0) == 1:
                    book_filename = n  # return part 1 of set
                logger.debug('%s: audioRename [%s] to [%s]' % (exists['BookName'], o, n))

            except Exception as e:
                logger.error('Unable to rename [%s] to [%s] %s %s' % (o, n, type(e).__name__, str(e)))
    return book_filename


def seriesInfo(bookid, part=None):
    """ Return series info for a bookid as a formatted string (seriesname #number)
        or (seriesname number) if no numeric part, or if not numeric eg "Book Two"
        If part is "Name" or "Num" just return relevant part of result """
    myDB = database.DBConnection()
    cmd = 'SELECT SeriesID,SeriesNum from member WHERE bookid=?'
    res = myDB.match(cmd, (bookid,))
    if not res:
        return ''

    seriesid = res['SeriesID']
    serieslist = getList(res['SeriesNum'])
    seriesnum = ''
    seriesname = ''
    # might be "Book 3.5" or similar, just get the numeric part
    while serieslist:
        seriesnum = serieslist.pop()
        try:
            _ = float(seriesnum)
            break
        except ValueError:
            seriesnum = ''
            pass

    if not seriesnum:
        # couldn't figure out number, keep everything we got, could be something like "Book Two"
        serieslist = res['SeriesNum']

    cmd = 'SELECT SeriesName from series WHERE seriesid=?'
    res = myDB.match(cmd, (seriesid,))
    if res:
        seriesname = res['SeriesName']
        if not seriesnum:
            # add what we got back to end of series name
            if serieslist:
                seriesname = "%s %s" % (seriesname, serieslist)

    if part == 'Name' and seriesname:
        return lazylibrarian.CONFIG['FMT_SERNAME'].replace('$SerName', seriesname).replace('$$', ' ')
    elif part == 'Num' and seriesnum:
        return lazylibrarian.CONFIG['FMT_SERNUM'].replace('$SerNum', seriesnum).replace('$$', ' ')
    elif seriesname or seriesnum:
        return lazylibrarian.CONFIG['FMT_SERIES'].replace('$SerNum', seriesnum).replace(
            '$SerName', seriesname).replace('$$', ' ')
    else:
        return ''


def bookRename(bookid):
    myDB = database.DBConnection()
    cmd = 'select AuthorName,BookName,BookFile from books,authors where books.AuthorID = authors.AuthorID and bookid=?'
    exists = myDB.match(cmd, (bookid,))
    if not exists:
        logger.debug("Invalid bookid in bookRename %s" % bookid)
        return ''

    f = exists['BookFile']
    if not f:
        logger.debug("No filename for %s in BookRename %s" % bookid)
        return ''

    r = os.path.dirname(f)
    try:
        calibreid = r.rsplit('(', 1)[1].split(')')[0]
        if not calibreid.isdigit():
            calibreid = ''
    except IndexError:
        calibreid = ''

    if calibreid:
        msg = '[%s] looks like a calibre directory: not renaming book' % os.path.basename(r)
        logger.debug(msg)
        return f

    dest_path = lazylibrarian.CONFIG['EBOOK_DEST_FOLDER'].replace(
        '$Author', exists['AuthorName']).replace(
        '$Title', exists['BookName']).replace(
        '$Series', seriesInfo(bookid)).replace(
        '$SerName', seriesInfo(bookid, 'Name')).replace(
        '$SerNum', seriesInfo(bookid, 'Num')).replace(
        '$$', ' ')
    dest_path = ' '.join(dest_path.split()).strip()
    dest_path = replace_all(dest_path, __dic__)
    dest_dir = lazylibrarian.DIRECTORY('eBook')
    dest_path = os.path.join(dest_dir, dest_path)

    if r != dest_path:
        try:
            shutil.move(r, dest_path)
            r = dest_path
        except Exception as why:
            if not os.path.isdir(dest_path):
                logger.debug('Unable to create directory %s: %s' % (dest_path, why))

    book_basename, prefextn = os.path.splitext(os.path.basename(f))
    new_basename = lazylibrarian.CONFIG['EBOOK_DEST_FILE']

    new_basename = new_basename.replace(
        '$Author', exists['AuthorName']).replace(
        '$Title', exists['BookName']).replace(
        '$Series', seriesInfo(bookid)).replace(
        '$SerName', seriesInfo(bookid, 'Name')).replace(
        '$SerNum', seriesInfo(bookid, 'Num')).replace(
        '$$', ' ')
    new_basename = ' '.join(new_basename.split()).strip()

    # replace all '/' not surrounded by whitespace with '_' as '/' is a directory separator
    slash = new_basename.find('/')
    while slash > 0:
        if new_basename[slash - 1] != ' ':
            if new_basename[slash + 1] != ' ':
                new_basename = new_basename[:slash] + '_' + new_basename[slash + 1:]
        slash = new_basename.find('/', slash + 1)

    if ' / ' in new_basename:  # used as a separator in goodreads omnibus
        logger.warn("bookRename [%s] looks like an omnibus? Not renaming %s" % (new_basename, book_basename))
        new_basename = book_basename

    if book_basename != new_basename:
        # only rename bookname.type, bookname.jpg, bookname.opf, not cover.jpg or metadata.opf
        for fname in os.listdir(makeBytestr(r)):
            fname = makeUnicode(fname)
            extn = ''
            if is_valid_booktype(fname, booktype='ebook'):
                extn = os.path.splitext(fname)[1]
            elif fname.endswith('.opf') and not fname == 'metadata.opf':
                extn = '.opf'
            elif fname.endswith('.jpg') and not fname == 'cover.jpg':
                extn = '.jpg'
            if extn:
                ofname = os.path.join(r, fname)
                nfname = os.path.join(r, new_basename + extn)
                try:
                    shutil.move(ofname, nfname)
                    logger.debug("bookRename %s to %s" % (ofname, nfname))
                    if ofname == exists['BookFile']:  # if we renamed the preferred filetype, return new name
                        f = nfname
                except Exception as e:
                    logger.error('Unable to rename [%s] to [%s] %s %s' %
                                 (ofname, nfname, type(e).__name__, str(e)))
    return f


def setAllBookAuthors():
    myDB = database.DBConnection()
    myDB.action('drop table if exists bookauthors')
    myDB.action('create table bookauthors (AuthorID TEXT, BookID TEXT, UNIQUE (AuthorID, BookID))')
    books = myDB.select('SELECT AuthorID,BookID from books')
    for item in books:
        myDB.action('insert into bookauthors (AuthorID, BookID) values (?, ?)',
                    (item['AuthorID'], item['BookID']), suppress='UNIQUE')
    # also need to drop authorid from books table once it all works properly
    totalauthors = 0
    totalrefs = 0
    books = myDB.select('select bookid,bookname,authorid from books where workpage is not null and workpage != ""')
    for book in books:
        newauthors, newrefs = setBookAuthors(book)
        totalauthors += newauthors
        totalrefs += newrefs
    msg = "Added %s new authors to database, %s new bookauthors" % (totalauthors, totalrefs)
    logger.debug(msg)
    return totalauthors, totalrefs


def setBookAuthors(book):
    myDB = database.DBConnection()
    newauthors = 0
    newrefs = 0
    try:
        authorlist = getBookAuthors(book['bookid'])
        for author in authorlist:
            authtype = author['type']
            if authtype in ['primary author', 'main author', 'secondary author']:
                if author['role'] in ['Author', '&mdash;'] and author['work'] == 'all editions':
                    name = formatAuthorName(author['name'])
                    exists = myDB.match('select authorid from authors where authorname=?', (name,))
                    if exists:
                        authorid = exists['authorid']
                    else:
                        # try to add new author to database by name
                        name, authorid, new = lazylibrarian.importer.addAuthorNameToDB(name, False, False)
                        if new and authorid:
                            newauthors += 1
                    if authorid:
                        # suppress duplicates in bookauthors
                        myDB.action('INSERT into bookauthors (AuthorID, BookID) VALUES (?, ?)',
                                    (authorid, book['bookid']), suppress='UNIQUE')
                        newrefs += 1
    except Exception as e:
        logger.debug("Error parsing authorlist for %s: %s %s" % (book['bookname'], type(e).__name__, str(e)))
    return newauthors, newrefs


def getAuthorImages():
    """ Try to get an author image for all authors without one"""
    myDB = database.DBConnection()
    cmd = 'select AuthorID, AuthorName from authors where (AuthorImg like "%nophoto%" or AuthorImg is null)'
    cmd += ' and Manual is not "1"'
    authors = myDB.select(cmd)
    if authors:
        logger.info('Checking images for %s author%s' % (len(authors), plural(len(authors))))
        counter = 0
        for author in authors:
            authorid = author['AuthorID']
            imagelink = getAuthorImage(authorid)
            newValueDict = {}
            if not imagelink:
                logger.debug('No image found for %s' % author['AuthorName'])
                newValueDict = {"AuthorImg": 'images/nophoto.png'}
            elif 'nophoto' not in imagelink:
                logger.debug('Updating %s image to %s' % (author['AuthorName'], imagelink))
                newValueDict = {"AuthorImg": imagelink}

            if newValueDict:
                counter += 1
                controlValueDict = {"AuthorID": authorid}
                myDB.upsert("authors", newValueDict, controlValueDict)

        msg = 'Updated %s image%s' % (counter, plural(counter))
        logger.info('Author Image check complete: ' + msg)
    else:
        msg = 'No missing author images'
        logger.debug(msg)
    return msg


def getBookCovers():
    """ Try to get a cover image for all books """

    myDB = database.DBConnection()
    books = myDB.select('select BookID,BookImg from books where BookImg like "%nocover%" and Manual is not "1"')
    if books:
        logger.info('Checking covers for %s book%s' % (len(books), plural(len(books))))
        counter = 0
        for book in books:
            bookid = book['BookID']
            coverlink = getBookCover(bookid)
            if coverlink and "nocover" not in coverlink:
                controlValueDict = {"BookID": bookid}
                newValueDict = {"BookImg": coverlink}
                myDB.upsert("books", newValueDict, controlValueDict)
                counter += 1
        msg = 'Updated %s cover%s' % (counter, plural(counter))
        logger.info('Cover check complete: ' + msg)
    else:
        msg = 'No missing book covers'
        logger.debug(msg)
    return msg


def setAllBookSeries():
    """ Try to set series details for all books from workpages"""
    myDB = database.DBConnection()
    books = myDB.select('select BookID from books where Manual is not "1"')
    counter = 0
    if books:
        logger.info('Checking series for %s book%s' % (len(books), plural(len(books))))
        for book in books:
            bookid = book['BookID']
            seriesdict = getWorkSeries(bookid)
            if seriesdict:
                counter += 1
                setSeries(seriesdict, bookid)
    deleteEmptySeries()
    msg = 'Updated %s book%s' % (counter, plural(counter))
    logger.info('Series check complete: ' + msg)
    return msg


def setSeries(seriesdict=None, bookid=None, seriesauthors=True, seriesdisplay=True):
    """ set series details in series/member tables from the supplied dict
        and a displayable summary in book table """
    myDB = database.DBConnection()
    if bookid:
        # delete any old series-member entries
        myDB.action('DELETE from member WHERE BookID=?', (bookid,))
        for item in seriesdict:
            match = myDB.match('SELECT SeriesID from series where SeriesName=? COLLATE NOCASE', (item,))
            if not match:
                # new series, need to set status and get SeriesID
                myDB.action('INSERT into series (SeriesName, Status) VALUES (?, ?)', (item, "Active"))
                match = myDB.match('SELECT SeriesID from series where SeriesName=?', (item,))
                # don't ask librarything what other books are in the series - leave for user to query if series wanted
                # _ = getSeriesMembers(match['SeriesID'])
            book = myDB.match('SELECT AuthorID from books where BookID=?', (bookid,))
            if match and book:
                controlValueDict = {"BookID": bookid, "SeriesID": match['SeriesID']}
                newValueDict = {"SeriesNum": seriesdict[item]}
                myDB.upsert("member", newValueDict, controlValueDict)
                # database versions earlier than 17 don't have seriesauthors table
                # but this function is used in dbupgrade v15
                if seriesauthors:
                    myDB.action('INSERT INTO seriesauthors ("SeriesID", "AuthorID") VALUES (?, ?)',
                                (match['SeriesID'], book['AuthorID']), suppress='UNIQUE')
            else:
                logger.debug('Unable to set series for book %s, %s' % (bookid, repr(seriesdict)))

        # database versions earlier than 19 don't have seriesdisplay
        # but this function is used in dbupgrade v15
        if seriesdisplay:
            series = ''
            for item in seriesdict:
                newseries = "%s %s" % (item, seriesdict[item])
                newseries.strip()
                if series and newseries:
                    series += '<br>'
                series += newseries
            myDB.action('UPDATE books SET SeriesDisplay=? WHERE BookID=?', (series, bookid))

            # removed deleteEmptySeries as setSeries slows down drastically if run in a loop
            # eg dbupgrade or setAllBookSeries. Better to tidy up all empties when loop finished
            # deleteEmptySeries()


def setStatus(bookid=None, seriesdict=None, default=None):
    """ Set the status of a book according to series/author/newbook/newauthor preferences
        return default if unchanged, default is passed in as newbook or newauthor status """
    myDB = database.DBConnection()
    if not bookid:
        return default

    match = myDB.match('SELECT Status,AuthorID,BookName from books WHERE BookID=?', (bookid,))
    if not match:
        return default

    # Don't update status if we already have the book but allow status change if ignored
    # might be we had ignore author set, but want to allow this series
    current_status = match['Status']
    if current_status in ['Have', 'Open']:
        return current_status

    new_status = ''
    authorid = match['AuthorID']
    bookname = match['BookName']
    # Is the book part of any series we want?
    for item in seriesdict:
        match = myDB.match('SELECT Status from series where SeriesName=? COLLATE NOCASE', (item,))
        if match['Status'] == 'Wanted':
            new_status = 'Wanted'
            logger.debug('Marking %s as %s, series %s' % (bookname, new_status, item))
            break

    if not new_status:
        # Is it part of any series we don't want?
        for item in seriesdict:
            match = myDB.match('SELECT Status from series where SeriesName=? COLLATE NOCASE', (item,))
            if match['Status'] == 'Skipped':
                new_status = 'Skipped'
                logger.debug('Marking %s as %s, series %s' % (bookname, new_status, item))
                break

    if not new_status:
        # Author we don't want?
        match = myDB.match('SELECT Status from authors where AuthorID=?', (authorid,))
        if match['Status'] in ['Paused', 'Ignored']:
            new_status = 'Skipped'
            logger.debug('Marking %s as %s, author %s' % (bookname, new_status, match['Status']))

    if not new_status:
        # Author we do want?
        match = myDB.match('SELECT Status from authors where AuthorID=?', (authorid,))
        if match['Status'] == 'Wanted':
            new_status = 'Wanted'
            logger.debug('Marking %s as %s, author %s' % (bookname, new_status, match['Status']))

    # If none of these, leave default "newbook" or "newauthor" status
    if new_status:
        myDB.action('UPDATE books SET Status=? WHERE BookID=?', (new_status, bookid))
        return new_status

    return default


def deleteEmptySeries():
    """ remove any series from series table that have no entries in member table, return how many deleted """
    myDB = database.DBConnection()
    series = myDB.select('SELECT SeriesID,SeriesName from series')
    count = 0
    for item in series:
        match = myDB.match('SELECT BookID from member where SeriesID=?', (item['SeriesID'],))
        if not match:
            logger.debug('Deleting empty series %s' % item['SeriesName'])
            count += 1
            myDB.action('DELETE from series where SeriesID=?', (item['SeriesID'],))
    return count


def setWorkPages():
    """ Set the workpage link for any books that don't already have one """

    myDB = database.DBConnection()
    cmd = 'select BookID,AuthorName,BookName from books,authors where length(WorkPage) < 4'
    cmd += ' and books.AuthorID = authors.AuthorID'
    books = myDB.select(cmd)
    if books:
        logger.debug('Setting WorkPage for %s book%s' % (len(books), plural(len(books))))
        counter = 0
        for book in books:
            bookid = book['BookID']
            worklink = getWorkPage(bookid)
            if worklink:
                controlValueDict = {"BookID": bookid}
                newValueDict = {"WorkPage": worklink}
                myDB.upsert("books", newValueDict, controlValueDict)
                counter += 1
            else:
                logger.debug('No WorkPage found for %s: %s' % (book['AuthorName'], book['BookName']))
        msg = 'Updated %s page%s' % (counter, plural(counter))
        logger.debug("setWorkPages complete: " + msg)
    else:
        msg = 'No missing WorkPages'
        logger.debug(msg)
    return msg


def librarything_wait():
    """ Wait for a second between librarything api calls """
    time_now = int(time.time())
    if time_now <= lazylibrarian.LAST_LIBRARYTHING:  # called within the last second?
        time.sleep(1)  # sleep 1 second to respect librarything api terms
    lazylibrarian.LAST_LIBRARYTHING = time_now


def getBookWork(bookID=None, reason=None, seriesID=None):
    """ return the contents of the LibraryThing workpage for the given bookid, or seriespage if seriesID given
        preferably from the cache. If not already cached cache the results
        Return None if no workpage/seriespage available """
    if not bookID and not seriesID:
        logger.error("getBookWork - No bookID or seriesID")
        return None

    if not reason:
        reason = ""

    myDB = database.DBConnection()
    if bookID:
        # need to specify authors.AuthorName here as function is called during dbupgrade v15 to v16
        # while books.authorname column is still present
        cmd = 'select BookName,authors.AuthorName,BookISBN from books,authors where bookID=?'
        cmd += ' and books.AuthorID = authors.AuthorID'
        cacheLocation = "WorkCache"
        item = myDB.match(cmd, (bookID,))
    else:
        cmd = 'select SeriesName from series where SeriesID=?'
        cacheLocation = "SeriesCache"
        item = myDB.match(cmd, (seriesID,))
    if item:
        cacheLocation = os.path.join(lazylibrarian.CACHEDIR, cacheLocation)
        if bookID:
            workfile = os.path.join(cacheLocation, str(bookID) + '.html')
        else:
            workfile = os.path.join(cacheLocation, str(seriesID) + '.html')

        # does the workpage need to expire? For now only expire if it was an error page
        # (small file) or a series page as librarything might get better info over time, more series members etc
        if os.path.isfile(workfile):
            if seriesID or os.path.getsize(workfile) < 500:
                cache_modified_time = os.stat(workfile).st_mtime
                time_now = time.time()
                expiry = lazylibrarian.CONFIG['CACHE_AGE'] * 24 * 60 * 60  # expire cache after this many seconds
                if cache_modified_time < time_now - expiry:
                    # Cache entry is too old, delete it
                    os.remove(workfile)

                    # os.remove(workfile)  # ignore cache for testing

        if os.path.isfile(workfile):
            # use cached file if possible to speed up refreshactiveauthors and librarysync re-runs
            lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
            if bookID:
                if reason:
                    logger.debug(u"getBookWork: Returning Cached entry for %s %s" % (bookID, reason))
                else:
                    logger.debug(u"getBookWork: Returning Cached workpage for %s" % bookID)
            else:
                logger.debug(u"getBookWork: Returning Cached seriespage for %s" % item['seriesName'])

            with open(workfile, "r") as cachefile:
                source = cachefile.read()
            return source
        else:
            lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1
            if bookID:
                title = safe_unicode(item['BookName'])
                title = title.encode(lazylibrarian.SYS_ENCODING)
                author = safe_unicode(item['AuthorName'])
                author = author.encode(lazylibrarian.SYS_ENCODING)
                URL = 'http://www.librarything.com/api/whatwork.php?author=%s&title=%s' % \
                      (urllib.quote_plus(author), urllib.quote_plus(title))
            else:
                seriesname = safe_unicode(item['seriesName'])
                seriesname = seriesname.encode(lazylibrarian.SYS_ENCODING)
                URL = 'http://www.librarything.com/series/%s' % urllib.quote_plus(seriesname)

            librarything_wait()
            result, success = fetchURL(URL)
            if bookID and success:
                # noinspection PyBroadException
                try:
                    workpage = result.split('<link>')[1].split('</link>')[0]
                    librarything_wait()
                    result, success = fetchURL(workpage)
                except Exception:
                    try:
                        errmsg = result.split('<error>')[1].split('</error>')[0]
                    except IndexError:
                        errmsg = "Unknown Error"
                    # if no workpage link, try isbn instead
                    if item['BookISBN']:
                        URL = 'http://www.librarything.com/api/whatwork.php?isbn=' + item['BookISBN']
                        librarything_wait()
                        result, success = fetchURL(URL)
                        if success:
                            # noinspection PyBroadException
                            try:
                                workpage = result.split('<link>')[1].split('</link>')[0]
                                librarything_wait()
                                result, success = fetchURL(workpage)
                            except Exception:
                                # no workpage link found by isbn
                                try:
                                    errmsg = result.split('<error>')[1].split('</error>')[0]
                                except IndexError:
                                    errmsg = "Unknown Error"
                                # still cache if whatwork returned a result without a link, so we don't keep retrying
                                logger.debug("getBookWork: Librarything: [%s] for ISBN %s" %
                                             (errmsg, item['BookISBN']))
                                success = True
                    else:
                        # still cache if whatwork returned a result without a link, so we don't keep retrying
                        msg = "getBookWork: Librarything: [" + errmsg + "] for "
                        logger.debug(msg + item['AuthorName'] + ' ' + item['BookName'])
                        success = True
            if success:
                with open(workfile, "w") as cachefile:
                    cachefile.write(result)
                    if bookID:
                        logger.debug(u"getBookWork: Caching workpage for %s" % workfile)
                    else:
                        logger.debug(u"getBookWork: Caching series page for %s" % workfile)
                    # return None if we got an error page back
                    if '</request><error>' in result:
                        return None
                return result
            else:
                if bookID:
                    logger.debug(u"getBookWork: Unable to cache workpage, got %s" % result)
                else:
                    logger.debug(u"getBookWork: Unable to cache series page, got %s" % result)
            return None
    else:
        if bookID:
            logger.debug('Get Book Work - Invalid bookID [%s]' % bookID)
        else:
            logger.debug('Get Book Work - Invalid seriesID [%s]' % seriesID)
        return None


def getWorkPage(bookID=None):
    """ return the URL of the LibraryThing workpage for the given bookid
        or an empty string if no workpage available """
    if not bookID:
        logger.error("getWorkPage - No bookID")
        return ''
    work = getBookWork(bookID, "Workpage")
    if work:
        try:
            page = work.split('og:url')[1].split('="')[1].split('"')[0]
        except IndexError:
            return ''
        return page
    return ''


def getAllSeriesAuthors():
    """ For each entry in the series table, get a list of authors contributing to the series
        and import those authors (and their books) into the database """
    myDB = database.DBConnection()
    series = myDB.select('select SeriesID from series')
    if series:
        logger.debug('Getting series authors for %s series' % len(series))
        counter = 0
        total = 0
        for entry in series:
            seriesid = entry['SeriesID']
            result = getSeriesAuthors(seriesid)
            if result:
                counter += 1
                total += result
            else:
                logger.debug('No series info found for series %s' % seriesid)
        msg = 'Updated authors for %s series, added %s new author%s' % (counter, total, plural(total))
        logger.debug("Series pages complete: " + msg)
    else:
        msg = 'No entries in the series table'
        logger.debug(msg)
    return msg


def getBookAuthors(bookid):
    """ Get a list of authors contributing to a book from the bookwork file """
    data = getBookWork(bookid, "Authors")
    if data:
        try:
            data = data.split('otherauthors_container')[1].split('</table>')[0].split('<table')[1].split('>', 1)[1]
        except IndexError:
            data = ''

    authorlist = []
    if data and 'Work?' in data:
        try:
            rows = data.split('<tr')
            for row in rows[2:]:
                author = {}
                col = row.split('<td>')
                author['name'] = col[1].split('">')[1].split('<')[0]
                author['role'] = col[2].split('<')[0]
                author['type'] = col[3].split('<')[0]
                author['work'] = col[4].split('<')[0]
                author['status'] = col[5].split('<')[0]
                authorlist.append(author)
        except IndexError:
            logger.debug('Error parsing authorlist for %s' % bookid)
    return authorlist


def getSeriesAuthors(seriesid):
    """ Get a list of authors contributing to a series
        and import those authors (and their books) into the database
        Return how many authors you added """
    myDB = database.DBConnection()
    result = myDB.match("select count('AuthorID') as counter from authors")
    start = int(result['counter'])
    result = myDB.match('select SeriesName from series where SeriesID=?', (seriesid,))
    seriesname = result['SeriesName']
    members = getSeriesMembers(seriesid)
    if members:
        myDB = database.DBConnection()
        for member in members:
            # order = member[0]
            bookname = member[1]
            authorname = member[2]

            base_url = 'https://www.goodreads.com/search.xml?q='
            params = {"key": lazylibrarian.CONFIG['GR_API']}
            searchname = bookname + ' ' + authorname
            searchname = cleanName(unaccented(searchname))
            searchname = searchname.encode(lazylibrarian.SYS_ENCODING)
            searchterm = urllib.quote_plus(searchname)
            set_url = base_url + searchterm + '&' + urllib.urlencode(params)
            authorid = ''
            try:
                rootxml, in_cache = get_xml_request(set_url)
                if rootxml is None:
                    logger.warn('Error getting XML for %s' % searchname)
                else:
                    resultxml = rootxml.getiterator('work')
                    for item in resultxml:
                        try:
                            booktitle = item.find('./best_book/title').text
                        except (KeyError, AttributeError):
                            booktitle = ""
                        book_fuzz = fuzz.token_set_ratio(booktitle, bookname)
                        if book_fuzz >= 98:
                            try:
                                author = item.find('./best_book/author/name').text
                            except (KeyError, AttributeError):
                                author = ""
                            try:
                                authorid = item.find('./best_book/author/id').text
                            except (KeyError, AttributeError):
                                authorid = ""
                            logger.debug("Author Search found %s %s, authorid %s" % (author, booktitle, authorid))
                            break
                if not authorid:  # try again with title only
                    searchname = cleanName(unaccented(bookname))
                    searchname = searchname.encode(lazylibrarian.SYS_ENCODING)
                    searchterm = urllib.quote_plus(searchname)
                    set_url = base_url + searchterm + '&' + urllib.urlencode(params)
                    rootxml, in_cache = get_xml_request(set_url)
                    if rootxml is None:
                        logger.warn('Error getting XML for %s' % searchname)
                    else:
                        resultxml = rootxml.getiterator('work')
                        for item in resultxml:
                            booktitle = item.find('./best_book/title').text
                            book_fuzz = fuzz.token_set_ratio(booktitle, bookname)
                            if book_fuzz >= 98:
                                try:
                                    author = item.find('./best_book/author/name').text
                                except (KeyError, AttributeError):
                                    author = ""
                                try:
                                    authorid = item.find('./best_book/author/id').text
                                except (KeyError, AttributeError):
                                    authorid = ""
                                logger.debug("Title Search found %s %s, authorid %s" % (author, booktitle, authorid))
                                break
                if not authorid:
                    logger.warn("GoodReads doesn't know about %s %s" % (authorname, bookname))
            except Exception as e:
                logger.error("Error finding goodreads results: %s %s" % (type(e).__name__, str(e)))

            if authorid:
                lazylibrarian.importer.addAuthorToDB(refresh=False, authorid=authorid)

    result = myDB.match("select count('AuthorID') as counter from authors")
    finish = int(result['counter'])
    newauth = finish - start
    logger.info("Added %s new author%s for %s" % (newauth, plural(newauth), seriesname))
    return newauth


def getSeriesMembers(seriesID=None):
    """ Ask librarything for order, bookname, authorname for all books in a series
        Return as a list of lists """
    results = []
    data = getBookWork(None, "SeriesPage", seriesID)
    if data:
        try:
            table = data.split('class="worksinseries"')[1].split('</table>')[0]
            rows = table.split('<tr')
            for row in rows:
                if 'href=' in row:
                    booklink = row.split('href="')[1]
                    bookname = booklink.split('">')[1].split('<')[0]
                    # booklink = booklink.split('"')[0]
                    try:
                        authorlink = row.split('href="')[2]
                        authorname = authorlink.split('">')[1].split('<')[0]
                        # authorlink = authorlink.split('"')[0]
                        order = row.split('class="order">')[1].split('<')[0]
                        results.append([order, bookname, authorname])
                    except IndexError:
                        logger.debug('Incomplete data in series table for series %s' % seriesID)
        except IndexError:
            if 'class="worksinseries"' in data:  # error parsing, or just no series data available?
                logger.debug('Error in series table for series %s' % seriesID)
    return results


def getWorkSeries(bookID=None):
    """ Return the series names and numbers in series for the given bookid as a dictionary """
    seriesdict = {}
    if not bookID:
        logger.error("getWorkSeries - No bookID")
        return seriesdict

    work = getBookWork(bookID, "Series")
    if work:
        try:
            serieslist = work.split('<h3><b>Series:')[1].split('</h3>')[0].split('<a href="/series/')
            for item in serieslist[1:]:
                try:
                    series = item.split('">')[1].split('</a>')[0]
                    if series and '(' in series:
                        seriesnum = series.split('(')[1].split(')')[0].strip()
                        series = series.split(' (')[0].strip()
                    else:
                        seriesnum = ''
                        series = series.strip()
                    series = cleanName(unaccented(series), '&/')
                    seriesnum = cleanName(unaccented(seriesnum))
                    seriesdict[series] = seriesnum
                except IndexError:
                    pass
        except IndexError:
            pass

    return seriesdict


def getBookCover(bookID=None, src=None):
    """ Return link to a local file containing a book cover image for a bookid.
        Try 1. Local file cached from goodreads/googlebooks when book was imported
            2. cover.jpg if we have the book
            3. LibraryThing whatwork
            4. Goodreads search if book was imported from goodreads
            5. Google images search
        if src is specified, get a cover from that source but do not cache it
        src = cache, cover, goodreads, librarything, google
        Return None if no cover available. """
    if not bookID:
        logger.error("getBookCover- No bookID")
        return None

    if not src:
        src = ''
    logger.debug("Getting %s cover for %s" % (src, bookID))
    # noinspection PyBroadException
    try:
        cachedir = lazylibrarian.CACHEDIR
        coverfile = os.path.join(cachedir, "book", bookID + '.jpg')

        if not src or src == 'cache' or src == 'current':
            if os.path.isfile(coverfile):  # use cached image if there is one
                lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
                logger.debug(u"getBookCover: Returning Cached response for %s" % coverfile)
                coverlink = 'cache/book/' + bookID + '.jpg'
                return coverlink
            elif src == 'cache':
                return None
            else:
                lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1

        myDB = database.DBConnection()
        if not src or src == 'cover':
            item = myDB.match('select BookFile from books where bookID=?', (bookID,))
            if item:
                bookfile = item['BookFile']
                if bookfile:  # we may have a cover.jpg in the same folder
                    bookdir = os.path.dirname(bookfile)
                    coverimg = os.path.join(bookdir, "cover.jpg")
                    if os.path.isfile(coverimg):
                        if src == 'cover':
                            coverfile = os.path.join(cachedir, "book", bookID + '_cover.jpg')
                            coverlink = 'cache/book/' + bookID + '_cover.jpg'
                            logger.debug(u"getBookCover: Caching cover.jpg for %s" % bookID)
                        else:
                            coverlink = 'cache/book/' + bookID + '.jpg'
                            logger.debug(u"getBookCover: Caching book cover for %s" % coverfile)
                        shutil.copyfile(coverimg, coverfile)
                        return coverlink
            if src == 'cover':
                logger.debug('getBookCover: No cover.jpg found for %s' % bookID)
                return None

        # see if librarything workpage has a cover
        if not src or src == 'librarything':
            work = getBookWork(bookID, "Cover")
            if work:
                try:
                    img = work.split('workCoverImage')[1].split('="')[1].split('"')[0]
                    if img and img.startswith('http'):
                        if src == 'librarything':
                            coverlink, success = cache_img("book", bookID + '_lt', img)
                        else:
                            coverlink, success = cache_img("book", bookID, img)
                        if success:
                            logger.debug(u"getBookCover: Caching librarything cover for %s" % bookID)
                            return coverlink
                        else:
                            logger.debug('getBookCover: Failed to cache image for %s [%s]' % (img, coverlink))
                    else:
                        logger.debug("getBookCover: No image found in work page for %s" % bookID)
                except IndexError:
                    logger.debug('getBookCover: workCoverImage not found in work page for %s' % bookID)

                try:
                    img = work.split('og:image')[1].split('="')[1].split('"')[0]
                    if img and img.startswith('http'):
                        if src == 'librarything':
                            coverlink, success = cache_img("book", bookID + '_lt', img)
                        else:
                            coverlink, success = cache_img("book", bookID, img)
                        if success:
                            logger.debug(u"getBookCover: Caching librarything cover for %s" % bookID)
                            return coverlink
                        else:
                            logger.debug('getBookCover: Failed to cache image for %s [%s]' % (img, coverlink))
                    else:
                        logger.debug("getBookCover: No image found in work page for %s" % bookID)
                except IndexError:
                    logger.debug('getBookCover: og:image not found in work page for %s' % bookID)
            else:
                logger.debug('getBookCover: No work page for %s' % bookID)
            if src == 'librarything':
                return None

        cmd = 'select BookName,AuthorName,BookLink from books,authors where bookID=?'
        cmd += ' and books.AuthorID = authors.AuthorID'
        item = myDB.match(cmd, (bookID,))
        safeparams = ''
        booklink = ''
        if item:
            title = safe_unicode(item['BookName'])
            title = title.encode(lazylibrarian.SYS_ENCODING)
            author = safe_unicode(item['AuthorName'])
            author = author.encode(lazylibrarian.SYS_ENCODING)
            booklink = item['BookLink']
            safeparams = urllib.quote_plus("%s %s" % (author, title))

        # try to get a cover from goodreads
        if not src or src == 'goodreads':
            if booklink and 'goodreads' in booklink:
                # if the bookID is a goodreads one, we can call https://www.goodreads.com/book/show/{bookID}
                # and scrape the page for og:image
                # <meta property="og:image" content="https://i.gr-assets.com/images/S/photo.goodreads.com/books/
                # 1388267702i/16304._UY475_SS475_.jpg"/>
                # to get the cover

                time_now = int(time.time())
                if time_now <= lazylibrarian.LAST_GOODREADS:
                    time.sleep(1)
                    lazylibrarian.LAST_GOODREADS = time_now
                result, success = fetchURL(booklink)
                if success:
                    try:
                        img = result.split('id="coverImage"')[1].split('src="')[1].split('"')[0]
                    except IndexError:
                        try:
                            img = result.split('og:image')[1].split('="')[1].split('"')[0]
                        except IndexError:
                            img = None
                    if img and img.startswith('http') and 'nocover' not in img and 'nophoto' not in img:
                        time_now = int(time.time())
                        if time_now <= lazylibrarian.LAST_GOODREADS:
                            time.sleep(1)
                            lazylibrarian.LAST_GOODREADS = time_now
                        if src == 'goodreads':
                            coverlink, success = cache_img("book", bookID + '_gr', img)
                        else:
                            coverlink, success = cache_img("book", bookID, img)
                        if success:
                            logger.debug("getBookCover: Caching goodreads cover for %s %s" %
                                         (item['AuthorName'], item['BookName']))
                            return coverlink
                        else:
                            logger.debug("getBookCover: Error getting goodreads image for %s, [%s]" % (img, coverlink))
                    else:
                        logger.debug("getBookCover: No image found in goodreads page for %s" % bookID)
                else:
                    logger.debug("getBookCover: Error getting page %s, [%s]" % (booklink, result))
            else:
                logger.debug("getBookCover: Not a goodreads booklink for %s [%s]" % (bookID, booklink))
            if src == 'goodreads':
                return None

        if not src or src == 'google':
            # try a google image search...
            # tbm=isch      search images
            # tbs=isz:l     large images
            # ift:jpg       jpeg file type
            if safeparams:
                URL = "https://www.google.com/search?tbm=isch&tbs=isz:l,ift:jpg&as_q=" + safeparams + "+ebook"
                result, success = fetchURL(URL)
                if success:
                    try:
                        img = result.split('url?q=')[1].split('">')[1].split('src="')[1].split('"')[0]
                    except IndexError:
                        img = None
                    if img and img.startswith('http'):
                        if src == 'google':
                            coverlink, success = cache_img("book", bookID + '_gb', img)
                        else:
                            coverlink, success = cache_img("book", bookID, img)
                        if success:
                            logger.debug("getBookCover: Caching google cover for %s %s" %
                                         (item['AuthorName'], item['BookName']))
                            return coverlink
                        else:
                            logger.debug("getBookCover: Error getting google image %s, [%s]" % (img, coverlink))
                    else:
                        logger.debug("getBookCover: No image found in google page for %s" % bookID)
                else:
                    logger.debug("getBookCover: Error getting google page for %s, [%s]" % (safeparams, result))
            else:
                logger.debug("getBookCover: No parameters for google page search for %s" % bookID)
        return None
    except Exception:
        logger.error('Unhandled exception in getBookCover: %s' % traceback.format_exc())
    return None


def getAuthorImage(authorid=None):
    # tbm=isch      search images
    # tbs=ift:jpg  jpeg file type
    if not authorid:
        logger.error("getAuthorImage: No authorid")
        return None

    cachedir = lazylibrarian.CACHEDIR
    coverfile = os.path.join(cachedir, "author", authorid + '.jpg')

    if os.path.isfile(coverfile):  # use cached image if there is one
        lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
        logger.debug(u"getAuthorImage: Returning Cached response for %s" % coverfile)
        coverlink = 'cache/author/' + authorid + '.jpg'
        return coverlink

    lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1
    myDB = database.DBConnection()
    authors = myDB.select('select AuthorName from authors where AuthorID=?', (authorid,))
    if authors:
        authorname = safe_unicode(authors[0][0])
        authorname = authorname.encode(lazylibrarian.SYS_ENCODING)
        safeparams = urllib.quote_plus("author %s" % authorname)
        URL = "https://www.google.com/search?tbm=isch&tbs=ift:jpg&as_q=" + safeparams
        result, success = fetchURL(URL)
        if success:
            try:
                img = result.split('url?q=')[1].split('">')[1].split('src="')[1].split('"')[0]
            except IndexError:
                img = None
            if img and img.startswith('http'):
                coverlink, success = cache_img("author", authorid, img)
                if success:
                    logger.debug("Cached google image for %s" % authorname)
                    return coverlink
                else:
                    logger.debug("Error getting google image %s, [%s]" % (img, coverlink))
            else:
                logger.debug("No image found in google page for %s" % authorname)
        else:
            logger.debug("Error getting google page for %s, [%s]" % (safeparams, result))
    else:
        logger.debug("No author found for %s" % authorid)
    return None

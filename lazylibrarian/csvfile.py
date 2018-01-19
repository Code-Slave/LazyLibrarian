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
import traceback

import lazylibrarian
import lib.csv as csv
from lazylibrarian import database, logger
from lazylibrarian.common import csv_file
from lazylibrarian.formatter import plural, is_valid_isbn, now, unaccented, formatAuthorName, makeUnicode
from lazylibrarian.importer import search_for, import_book, addAuthorNameToDB
from lazylibrarian.librarysync import find_book_in_db


def export_CSV(search_dir=None, status="Wanted"):
    """ Write a csv file to the search_dir containing all books marked as "Wanted" """
    # noinspection PyBroadException
    try:
        if not search_dir:
            msg = "Alternate Directory not configured"
            logger.warn(msg)
            return msg
        elif not os.path.isdir(search_dir):
            msg = "Alternate Directory [%s] not found" % search_dir
            logger.warn(msg)
            return msg
        elif not os.access(search_dir, os.W_OK | os.X_OK):
            msg = "Alternate Directory [%s] not writable" % search_dir
            logger.warn(msg)
            return msg

        csvFile = os.path.join(search_dir, "%s - %s.csv" % (status, now().replace(':', '-')))

        myDB = database.DBConnection()

        cmd = 'SELECT BookID,AuthorName,BookName,BookIsbn,books.AuthorID FROM books,authors '
        cmd += 'WHERE books.Status=? and books.AuthorID = authors.AuthorID'
        find_status = myDB.select(cmd, (status,))

        if not find_status:
            msg = "No books marked as %s" % status
            logger.warn(msg)
        else:
            count = 0
            with open(csvFile, 'wb') as csvfile:
                csvwrite = csv.writer(csvfile, delimiter=',',
                                      quotechar='"', quoting=csv.QUOTE_MINIMAL)

                # write headers, change AuthorName BookName BookIsbn to match import csv names (Author, Title, ISBN10)
                csvwrite.writerow(['BookID', 'Author', 'Title', 'ISBN', 'AuthorID'])

                for resulted in find_status:
                    logger.debug(u"Exported CSV for book %s" % resulted['BookName'])
                    row = ([resulted['BookID'], resulted['AuthorName'], resulted['BookName'],
                            resulted['BookIsbn'], resulted['AuthorID']])
                    csvwrite.writerow([("%s" % s).encode(lazylibrarian.SYS_ENCODING) for s in row])
                    count += 1
            msg = "CSV exported %s book%s to %s" % (count, plural(count), csvFile)
            logger.info(msg)
        return msg
    except Exception:
        msg = 'Unhandled exception in exportCSV: %s' % traceback.format_exc()
        logger.error(msg)
        return msg


def finditem(item, authorname, headers):
    """
    Try to find book matching the csv item in the database
    Return database entry, or False if not found
    """
    myDB = database.DBConnection()
    bookmatch = ""
    isbn10 = ""
    isbn13 = ""
    bookid = ""
    bookname = item['Title']

    bookname = makeUnicode(bookname)
    if 'ISBN' in headers:
        isbn10 = item['ISBN']
    if 'ISBN13' in headers:
        isbn13 = item['ISBN13']
    if 'BookID' in headers:
        bookid = item['BookID']

    # try to find book in our database using bookid or isbn, or if that fails, name matching
    cmd = 'SELECT AuthorName,BookName,BookID,books.Status FROM books,authors where books.AuthorID = authors.AuthorID '
    if bookid:
        fullcmd = cmd + 'and BookID=?'
        bookmatch = myDB.match(fullcmd, (bookid,))
    if not bookmatch:
        if is_valid_isbn(isbn10):
            fullcmd = cmd + 'and BookIsbn=?'
            bookmatch = myDB.match(fullcmd, (isbn10,))
    if not bookmatch:
        if is_valid_isbn(isbn13):
            fullcmd = cmd + 'and BookIsbn=?'
            bookmatch = myDB.match(fullcmd, (isbn13,))
    if not bookmatch:
        bookid = find_book_in_db(authorname, bookname)
        if bookid:
            fullcmd = cmd + 'and BookID=?'
            bookmatch = myDB.match(fullcmd, (bookid,))
    return bookmatch


# noinspection PyTypeChecker
def import_CSV(search_dir=None):
    """ Find a csv file in the search_dir and process all the books in it,
        adding authors to the database if not found
        and marking the books as "Wanted"
    """
    # noinspection PyBroadException
    try:
        if not search_dir:
            msg = "Alternate Directory not configured"
            logger.warn(msg)
            return msg
        elif not os.path.isdir(search_dir):
            msg = "Alternate Directory [%s] not found" % search_dir
            logger.warn(msg)
            return msg

        csvFile = csv_file(search_dir)

        headers = None
        content = {}

        if not csvFile:
            msg = "No CSV file found in %s" % search_dir
            logger.warn(msg)
            return msg
        else:
            logger.debug(u'Reading file %s' % csvFile)
            reader = csv.reader(open(csvFile))
            for row in reader:
                if reader.line_num == 1:
                    # If we are on the first line, create the headers list from the first row
                    headers = row
                else:
                    # Otherwise, the key in the content dictionary is the first item in the
                    # row and we can create the sub-dictionary by using the zip() function.
                    # we include the key in the dictionary as our exported csv files use
                    # bookid as the key
                    content[row[0]] = dict(zip(headers, row))

            # We can now get to the content by using the resulting dictionary, so to see
            # the list of lines, we can do: print content.keys()  to get a list of keys
            # To see the list of fields available for each book:  print headers

            if 'Author' not in headers or 'Title' not in headers:
                msg = 'Invalid CSV file found %s' % csvFile
                logger.warn(msg)
                return msg

            myDB = database.DBConnection()
            bookcount = 0
            authcount = 0
            skipcount = 0
            logger.debug(u"CSV: Found %s book%s in csv file" % (
                         len(content.keys()), plural(len(content.keys()))))
            for item in content.keys():
                authorname = formatAuthorName(authorname)
                title = content[item]['Title']
                title = makeUnicode(title)

                authmatch = myDB.match('SELECT * FROM authors where AuthorName=?', (authorname,))

                if authmatch:
                    logger.debug(u"CSV: Author %s found in database" % authorname)
                else:
                    logger.debug(u"CSV: Author %s not found" % authorname)
                    newauthor, authorid, new = addAuthorNameToDB(author=authorname,
                                                                 addbooks=lazylibrarian.CONFIG['NEWAUTHOR_BOOKS'])
                    if len(newauthor) and newauthor != authorname:
                        logger.debug("Preferred authorname changed from [%s] to [%s]" % (authorname, newauthor))
                        authorname = newauthor
                    if new:
                        authcount += 1

                bookmatch = finditem(content[item], authorname, headers)
                result = ''
                if bookmatch:
                    authorname = bookmatch['AuthorName']
                    bookname = bookmatch['BookName']
                    bookid = bookmatch['BookID']
                    bookstatus = bookmatch['Status']
                    if bookstatus in ['Open', 'Wanted', 'Have']:
                        logger.info(u'Found book %s by %s, already marked as "%s"' % (bookname, authorname, bookstatus))
                    else:  # skipped/ignored
                        logger.info(u'Found book %s by %s, marking as "Wanted"' % (bookname, authorname))
                        controlValueDict = {"BookID": bookid}
                        newValueDict = {"Status": "Wanted"}
                        myDB.upsert("books", newValueDict, controlValueDict)
                        bookcount += 1
                else:
                    searchterm = "%s <ll> %s" % (title, authorname)
                    results = search_for(unaccented(searchterm))
                    if results:
                        result = results[0]
                        if result['author_fuzz'] > lazylibrarian.CONFIG['MATCH_RATIO'] \
                                and result['book_fuzz'] > lazylibrarian.CONFIG['MATCH_RATIO']:
                            logger.info("Found (%s%% %s%%) %s: %s" % (result['author_fuzz'], result['book_fuzz'],
                                                                      result['authorname'], result['bookname']))
                            import_book(result['bookid'])
                            bookcount += 1
                            bookmatch = True

                if not bookmatch:
                    msg = "Skipping book %s by %s" % (title, authorname)
                    if not result:
                        msg += ', No results returned'
                        logger.warn(msg)
                    else:
                        msg += ', No match found'
                        logger.warn(msg)
                        msg = "Closest match (%s%% %s%%) %s: %s" % (result['author_fuzz'], result['book_fuzz'],
                                                                    result['authorname'], result['bookname'])
                        logger.warn(msg)
                    skipcount += 1
            msg = "Added %i new author%s, marked %i book%s as 'Wanted', %i book%s not found" % \
                  (authcount, plural(authcount), bookcount, plural(bookcount), skipcount, plural(skipcount))
            logger.info(msg)
            return msg
    except Exception:
        msg = 'Unhandled exception in importCSV: %s' % traceback.format_exc()
        logger.error(msg)
        return msg

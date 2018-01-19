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


# example
# https://www.googleapis.com/books/v1/volumes?q=+inauthor:george+martin+intitle:song+ice+fire

import re
import traceback
import urllib
try:
    import requests
except ImportError:
    import lib.requests as requests

import lazylibrarian
from lazylibrarian import logger, database
from lazylibrarian.bookwork import librarything_wait, getBookCover, getWorkSeries, getWorkPage, deleteEmptySeries, \
    setSeries, setStatus
from lazylibrarian.cache import get_json_request, cache_img
from lazylibrarian.formatter import plural, today, replace_all, unaccented, unaccented_str, is_valid_isbn, \
    getList, cleanName, check_int, makeUnicode
from lazylibrarian.common import proxyList
from lazylibrarian.gr import GoodReads
from lib.fuzzywuzzy import fuzz


class GoogleBooks:
    def __init__(self, name=None):
        self.name = makeUnicode(name)
        if not lazylibrarian.CONFIG['GB_API']:
            logger.warn('No GoogleBooks API key, check config')
        self.url = 'https://www.googleapis.com/books/v1/volumes?q='
        self.params = {
            'maxResults': 40,
            'printType': 'books',
            'key': lazylibrarian.CONFIG['GB_API']
        }

    # noinspection PyBroadException
    def find_results(self, searchterm=None, queue=None):
        """ GoogleBooks performs much better if we search for author OR title
            not both at once, so if searchterm is not isbn, two searches needed.
            Lazylibrarian searches use <ll> to separate title from author in searchterm
            If this token isn't present, it's an isbn or searchterm as supplied by user
        """
        try:
            myDB = database.DBConnection()
            resultlist = []
            # See if we should check ISBN field, otherwise ignore it
            api_strings = ['inauthor:', 'intitle:']
            if is_valid_isbn(searchterm):
                api_strings = ['isbn:']

            api_hits = 0

            ignored = 0
            total_count = 0
            no_author_count = 0
            title = ''
            authorname = ''

            if ' <ll> ' in searchterm:  # special token separates title from author
                title, authorname = searchterm.split(' <ll> ')

            fullterm = searchterm.replace(' <ll> ', ' ')
            logger.debug('Now searching Google Books API with searchterm: %s' % fullterm)

            for api_value in api_strings:
                set_url = self.url
                if api_value == "isbn:":
                    set_url = set_url + urllib.quote(api_value + searchterm)
                elif api_value == 'intitle:':
                    searchterm = fullterm
                    if title:  # just search for title
                        # noinspection PyUnresolvedReferences
                        title = title.split(' (')[0]  # without any series info
                        searchterm = title
                    searchterm = searchterm.replace("'", "").replace('"', '').strip()  # and no quotes
                    searchterm = searchterm.encode(lazylibrarian.SYS_ENCODING)
                    set_url = set_url + urllib.quote(api_value + '"' + searchterm + '"')
                elif api_value == 'inauthor:':
                    searchterm = fullterm
                    if authorname:
                        searchterm = authorname  # just search for author
                    searchterm = searchterm.strip()
                    searchterm = searchterm.encode(lazylibrarian.SYS_ENCODING)
                    set_url = set_url + urllib.quote(api_value + '"' + searchterm + '"')

                startindex = 0
                resultcount = 0
                ignored = 0
                number_results = 1
                total_count = 0
                no_author_count = 0
                try:
                    while startindex < number_results:

                        self.params['startIndex'] = startindex
                        URL = set_url + '&' + urllib.urlencode(self.params)

                        try:
                            jsonresults, in_cache = get_json_request(URL)
                            if jsonresults is None:
                                number_results = 0
                            else:
                                if not in_cache:
                                    api_hits += 1
                                number_results = jsonresults['totalItems']
                                logger.debug('Searching url: ' + URL)
                            if number_results == 0:
                                logger.warn('Found no results for %s with value: %s' % (api_value, searchterm))
                                break
                            else:
                                pass
                        except Exception as err:
                            if hasattr(err, 'reason'):
                                errmsg = err.reason
                            else:
                                errmsg = str(err)
                            logger.warn(
                                'Google Books API Error [%s]: Check your API key or wait a while' %
                                errmsg)
                            break

                        startindex += 40

                        for item in jsonresults['items']:

                            total_count += 1

                            # skip if no author, no author is no book.
                            try:
                                Author = item['volumeInfo']['authors'][0]
                            except KeyError:
                                logger.debug('Skipped a result without authorfield.')
                                no_author_count += 1
                                continue
                            try:
                                bookname = item['volumeInfo']['title']
                            except KeyError:
                                logger.debug('Skipped a result without title.')
                                continue

                            valid_langs = getList(lazylibrarian.CONFIG['IMP_PREFLANG'])
                            booklang = ''
                            if "All" not in valid_langs:  # don't care about languages, accept all
                                try:
                                    # skip if language is not in valid list -
                                    booklang = item['volumeInfo']['language']
                                    if booklang not in valid_langs:
                                        logger.debug(
                                            'Skipped %s with language %s' % (bookname, booklang))
                                        ignored += 1
                                        continue
                                except KeyError:
                                    ignored += 1
                                    logger.debug('Skipped %s where no language is found' % bookname)
                                    continue

                            try:
                                bookpub = item['volumeInfo']['publisher']
                            except KeyError:
                                bookpub = ""

                            try:
                                booksub = item['volumeInfo']['subtitle']
                            except KeyError:
                                booksub = ""

                            try:
                                bookdate = item['volumeInfo']['publishedDate']
                            except KeyError:
                                bookdate = '0000-00-00'
                            bookdate = bookdate[:4]

                            try:
                                bookimg = item['volumeInfo']['imageLinks']['thumbnail']
                            except KeyError:
                                bookimg = 'images/nocover.png'

                            try:
                                bookrate = item['volumeInfo']['averageRating']
                            except KeyError:
                                bookrate = 0

                            try:
                                bookpages = item['volumeInfo']['pageCount']
                            except KeyError:
                                bookpages = '0'

                            try:
                                bookgenre = item['volumeInfo']['categories'][0]
                            except KeyError:
                                bookgenre = ""

                            try:
                                bookdesc = item['volumeInfo']['description']
                            except KeyError:
                                bookdesc = 'Not available'

                            try:
                                num_reviews = item['volumeInfo']['ratingsCount']
                            except KeyError:
                                num_reviews = 0

                            try:
                                if item['volumeInfo']['industryIdentifiers'][0]['type'] == 'ISBN_10':
                                    bookisbn = item['volumeInfo'][
                                        'industryIdentifiers'][0]['identifier']
                                else:
                                    bookisbn = 0
                            except KeyError:
                                bookisbn = 0

                            if authorname:
                                author_fuzz = fuzz.ratio(Author, authorname)
                            else:
                                author_fuzz = fuzz.ratio(Author, fullterm)

                            if title:
                                book_fuzz = fuzz.token_set_ratio(bookname, title)
                                # lose a point for each extra word in the fuzzy matches so we get the closest match
                                words = len(getList(bookname))
                                words -= len(getList(title))
                                book_fuzz -= abs(words)
                            else:
                                book_fuzz = fuzz.token_set_ratio(bookname, fullterm)

                            isbn_fuzz = 0
                            if is_valid_isbn(fullterm):
                                isbn_fuzz = 100

                            highest_fuzz = max((author_fuzz + book_fuzz) / 2, isbn_fuzz)

                            dic = {':': '.', '"': '', '\'': ''}
                            bookname = replace_all(bookname, dic)

                            bookname = unaccented(bookname)
                            bookname = bookname.strip()  # strip whitespace
                            bookid = item['id']

                            author = myDB.select(
                                'SELECT AuthorID FROM authors WHERE AuthorName=?', (Author.replace('"', '""'),))
                            if author:
                                AuthorID = author[0]['authorid']
                            else:
                                AuthorID = ''

                            resultlist.append({
                                'authorname': Author,
                                'authorid': AuthorID,
                                'bookid': bookid,
                                'bookname': bookname,
                                'booksub': booksub,
                                'bookisbn': bookisbn,
                                'bookpub': bookpub,
                                'bookdate': bookdate,
                                'booklang': booklang,
                                'booklink': item['volumeInfo']['canonicalVolumeLink'],
                                'bookrate': float(bookrate),
                                'bookimg': bookimg,
                                'bookpages': bookpages,
                                'bookgenre': bookgenre,
                                'bookdesc': bookdesc,
                                'author_fuzz': author_fuzz,
                                'book_fuzz': book_fuzz,
                                'isbn_fuzz': isbn_fuzz,
                                'highest_fuzz': highest_fuzz,
                                'num_reviews': num_reviews
                            })

                            resultcount += 1

                except KeyError:
                    break

                logger.debug("Returning %s result%s for (%s) with keyword: %s" %
                             (resultcount, plural(resultcount), api_value, searchterm))

            logger.debug("Found %s result%s" % (total_count, plural(total_count)))
            logger.debug("Removed %s unwanted language result%s" % (ignored, plural(ignored)))
            logger.debug("Removed %s book%s with no author" % (no_author_count, plural(no_author_count)))
            logger.debug('The Google Books API was hit %s time%s for searchterm: %s' %
                         (api_hits, plural(api_hits), fullterm))
            queue.put(resultlist)

        except Exception:
            logger.error('Unhandled exception in GB.find_results: %s' % traceback.format_exc())

    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped",
                         entrystatus='Active', refresh=False):
        # noinspection PyBroadException
        try:
            logger.debug('[%s] Now processing books with Google Books API' % authorname)
            # google doesnt like accents in author names
            set_url = self.url + urllib.quote('inauthor:"%s"' % unaccented_str(authorname))

            api_hits = 0
            gr_lang_hits = 0
            lt_lang_hits = 0
            gb_lang_change = 0
            cache_hits = 0
            not_cached = 0
            startindex = 0
            resultcount = 0
            removedResults = 0
            duplicates = 0
            ignored = 0
            added_count = 0
            updated_count = 0
            book_ignore_count = 0
            total_count = 0
            number_results = 1

            valid_langs = getList(lazylibrarian.CONFIG['IMP_PREFLANG'])
            # Artist is loading
            myDB = database.DBConnection()
            controlValueDict = {"AuthorID": authorid}
            newValueDict = {"Status": "Loading"}
            myDB.upsert("authors", newValueDict, controlValueDict)

            try:
                while startindex < number_results:

                    self.params['startIndex'] = startindex
                    URL = set_url + '&' + urllib.urlencode(self.params)

                    try:
                        jsonresults, in_cache = get_json_request(URL, useCache=not refresh)
                        if jsonresults is None:
                            number_results = 0
                        else:
                            if not in_cache:
                                api_hits += 1
                            number_results = jsonresults['totalItems']
                    except Exception as err:
                        if hasattr(err, 'reason'):
                            errmsg = err.reason
                        else:
                            errmsg = str(err)
                        logger.warn('Google Books API Error [%s]: Check your API key or wait a while' % errmsg)
                        break

                    if number_results == 0:
                        logger.warn('Found no results for %s' % authorname)
                        break
                    else:
                        logger.debug('Found %s result%s for %s' % (number_results, plural(number_results), authorname))

                    startindex += 40

                    for item in jsonresults['items']:

                        total_count += 1

                        # skip if no author, no author is no book.
                        try:
                            _ = item['volumeInfo']['authors'][0]
                        except KeyError:
                            logger.debug('Skipped a result without authorfield.')
                            continue

                        try:
                            if item['volumeInfo']['industryIdentifiers'][0]['type'] == 'ISBN_10':
                                bookisbn = item['volumeInfo'][
                                    'industryIdentifiers'][0]['identifier']
                            else:
                                bookisbn = ""
                        except KeyError:
                            bookisbn = ""

                        isbnhead = ""
                        if len(bookisbn) == 10:
                            isbnhead = bookisbn[0:3]
                        elif len(bookisbn) == 13:
                            isbnhead = bookisbn[3:6]

                        try:
                            booklang = item['volumeInfo']['language']
                        except KeyError:
                            booklang = "Unknown"

                        # do we care about language?
                        if "All" not in valid_langs:
                            if bookisbn != "":
                                # seems google lies to us, sometimes tells us books are in english when they are not
                                if booklang == "Unknown" or booklang == "en":
                                    googlelang = booklang
                                    match = False
                                    lang = myDB.match('SELECT lang FROM languages where isbn=?', (isbnhead,))
                                    if lang:
                                        booklang = lang['lang']
                                        cache_hits += 1
                                        logger.debug("Found cached language [%s] for [%s]" % (booklang, isbnhead))
                                        match = True
                                    if not match:  # no match in cache, try lookup dict
                                        if isbnhead:
                                            if len(bookisbn) == 13 and bookisbn.startswith('979'):
                                                for lang in lazylibrarian.isbn_979_dict:
                                                    if isbnhead.startswith(lang):
                                                        booklang = lazylibrarian.isbn_979_dict[lang]
                                                        logger.debug("ISBN979 returned %s for %s" %
                                                                     (booklang, isbnhead))
                                                        match = True
                                                        break
                                            elif (len(bookisbn) == 10) or \
                                                    (len(bookisbn) == 13 and bookisbn.startswith('978')):
                                                for lang in lazylibrarian.isbn_978_dict:
                                                    if isbnhead.startswith(lang):
                                                        booklang = lazylibrarian.isbn_978_dict[lang]
                                                        logger.debug("ISBN979 returned %s for %s" %
                                                                     (booklang, isbnhead))
                                                        match = True
                                                        break
                                            if match:
                                                myDB.action('insert into languages values (?, ?)',
                                                            (isbnhead, booklang))
                                                logger.debug("GB language: " + booklang)

                                    if not match:
                                        # try searching librarything for a language code using the isbn
                                        # if no language found, librarything return value is "invalid" or "unknown"
                                        # librarything returns plain text, not xml
                                        BOOK_URL = 'http://www.librarything.com/api/thingLang.php?isbn=' + bookisbn
                                        proxies = proxyList()
                                        try:
                                            librarything_wait()
                                            timeout = check_int(lazylibrarian.CONFIG['HTTP_TIMEOUT'], 30)
                                            r = requests.get(BOOK_URL, timeout=timeout, proxies=proxies)
                                            resp = r.text
                                            lt_lang_hits += 1
                                            logger.debug(
                                                "LibraryThing reports language [%s] for %s" % (resp, isbnhead))
                                            if resp != 'invalid' and resp != 'unknown':
                                                booklang = resp  # found a language code
                                                match = True
                                                myDB.action('insert into languages values (?, ?)',
                                                            (isbnhead, booklang))
                                                logger.debug("LT language: " + booklang)
                                        except Exception as e:
                                            booklang = ""
                                            logger.error("%s finding language: %s" % (type(e).__name__, str(e)))

                                    if match:
                                        # We found a better language match
                                        if googlelang == "en" and booklang not in ["en-US", "en-GB", "eng"]:
                                            # these are all english, may need to expand this list
                                            booknamealt = item['volumeInfo']['title']
                                            logger.debug("%s Google thinks [%s], we think [%s]" %
                                                         (booknamealt, googlelang, booklang))
                                            gb_lang_change += 1
                                    else:  # No match anywhere, accept google language
                                        booklang = googlelang

                            # skip if language is in ignore list
                            if booklang not in valid_langs:
                                booknamealt = item['volumeInfo']['title']
                                logger.debug(
                                    'Skipped [%s] with language %s' %
                                    (booknamealt, booklang))
                                ignored += 1
                                continue

                        try:
                            bookpub = item['volumeInfo']['publisher']
                        except KeyError:
                            bookpub = ""

                        try:
                            booksub = item['volumeInfo']['subtitle']
                        except KeyError:
                            booksub = ""

                        if not booksub:
                            series = ""
                            seriesNum = ""
                        else:
                            try:
                                series = booksub.split('(')[1].split(' Series ')[0]
                            except IndexError:
                                series = ""
                            if series.endswith(')'):
                                series = series[:-1]
                            try:
                                seriesNum = booksub.split('(')[1].split(' Series ')[1].split(')')[0]
                                if seriesNum[0] == '#':
                                    seriesNum = seriesNum[1:]
                            except IndexError:
                                seriesNum = ""

                            if not seriesNum and '#' in series:
                                words = series.rsplit('#', 1)
                                series = words[0].strip()
                                seriesNum = words[1].strip()
                            if not seriesNum and ' ' in series:
                                words = series.rsplit(' ', 1)
                                # has to be unicode for isnumeric()
                                if (u"%s" % words[1]).isnumeric():
                                    series = words[0]
                                    seriesNum = words[1]

                        try:
                            bookdate = item['volumeInfo']['publishedDate']
                        except KeyError:
                            bookdate = '0000-00-00'

                        try:
                            bookimg = item['volumeInfo']['imageLinks']['thumbnail']
                        except KeyError:
                            bookimg = 'images/nocover.png'

                        try:
                            bookrate = item['volumeInfo']['averageRating']
                        except KeyError:
                            bookrate = 0

                        try:
                            bookpages = item['volumeInfo']['pageCount']
                        except KeyError:
                            bookpages = 0

                        try:
                            bookgenre = item['volumeInfo']['categories'][0]
                        except KeyError:
                            bookgenre = ""

                        try:
                            bookdesc = item['volumeInfo']['description']
                        except KeyError:
                            bookdesc = ""

                        rejected = False
                        check_status = False

                        bookname = item['volumeInfo']['title']

                        if not bookname:
                            logger.debug('Rejecting bookid %s for %s, no bookname' % (bookid, authorname))
                            removedResults += 1
                            rejected = True
                        else:
                            bookname = replace_all(unaccented(bookname), {':': '.', '"': '', '\'': ''}).strip()
                            booklink = item['volumeInfo']['canonicalVolumeLink']
                            bookrate = float(bookrate)
                            bookid = item['id']

                            # GoodReads sometimes has multiple bookids for the same book (same author/title, different
                            # editions) and sometimes uses the same bookid if the book is the same but the title is
                            # slightly different. Not sure if googlebooks does too, but we only want one...
                            existing_book = myDB.match('SELECT Status,Manual,BookAdded FROM books WHERE BookID=?',
                                                       (bookid,))
                            if existing_book:
                                book_status = existing_book['Status']
                                locked = existing_book['Manual']
                                added = existing_book['BookAdded']
                                if locked is None:
                                    locked = False
                                elif locked.isdigit():
                                    locked = bool(int(locked))
                            else:
                                book_status = bookstatus  # new_book status, or new_author status
                                added = today()
                                locked = False

                        if not rejected and re.match('[^\w-]', bookname):  # remove books with bad characters in title
                            logger.debug("[%s] removed book for bad characters" % bookname)
                            removedResults += 1
                            rejected = True

                        if not rejected and lazylibrarian.CONFIG['NO_FUTURE']:
                            # googlebooks sometimes gives yyyy, sometimes yyyy-mm, sometimes yyyy-mm-dd
                            if bookdate > today()[:len(bookdate)]:
                                logger.debug('Rejecting %s, future publication date %s' % (bookname, bookdate))
                                removedResults += 1
                                rejected = True

                        if not rejected:
                            cmd = 'SELECT BookID FROM books,authors WHERE books.AuthorID = authors.AuthorID'
                            cmd += ' and BookName=? COLLATE NOCASE and AuthorName=? COLLATE NOCASE'
                            match = myDB.match(cmd, (bookname.replace('"', '""'), authorname.replace('"', '""')))
                            if match:
                                if match['BookID'] != bookid:  # we have a different book with this author/title already
                                    logger.debug('Rejecting bookid %s for [%s][%s] already got %s' %
                                                 (match['BookID'], authorname, bookname, bookid))
                                    rejected = True
                                    duplicates += 1

                        if not rejected:
                            cmd = 'SELECT AuthorName,BookName FROM books,authors'
                            cmd += ' WHERE authors.AuthorID = books.AuthorID AND BookID=?'
                            match = myDB.match(cmd, (bookid,))
                            if match:  # we have a book with this bookid already
                                if bookname != match['BookName'] or authorname != match['AuthorName']:
                                    logger.debug('Rejecting bookid %s for [%s][%s] already got bookid for [%s][%s]' %
                                                 (bookid, authorname, bookname, match['AuthorName'], match['BookName']))
                                else:
                                    logger.debug('Rejecting bookid %s for [%s][%s] already got this book in database' %
                                                 (bookid, authorname, bookname))
                                    check_status = True
                                duplicates += 1
                                rejected = True

                        if check_status or not rejected:
                            if book_status != "Ignored" and not locked:
                                controlValueDict = {"BookID": bookid}
                                newValueDict = {
                                    "AuthorID": authorid,
                                    "BookName": bookname,
                                    "BookSub": booksub,
                                    "BookDesc": bookdesc,
                                    "BookIsbn": bookisbn,
                                    "BookPub": bookpub,
                                    "BookGenre": bookgenre,
                                    "BookImg": bookimg,
                                    "BookLink": booklink,
                                    "BookRate": bookrate,
                                    "BookPages": bookpages,
                                    "BookDate": bookdate,
                                    "BookLang": booklang,
                                    "Status": book_status,
                                    "AudioStatus": lazylibrarian.CONFIG['NEWAUDIO_STATUS'],
                                    "BookAdded": added
                                }
                                resultcount += 1

                                myDB.upsert("books", newValueDict, controlValueDict)
                                logger.debug("Book found: " + bookname + " " + bookdate)
                                updated = False
                                if 'nocover' in bookimg or 'nophoto' in bookimg:
                                    # try to get a cover from librarything
                                    workcover = getBookCover(bookid)
                                    if workcover:
                                        logger.debug('Updated cover for %s to %s' % (bookname, workcover))
                                        controlValueDict = {"BookID": bookid}
                                        newValueDict = {"BookImg": workcover}
                                        myDB.upsert("books", newValueDict, controlValueDict)
                                        updated = True

                                elif bookimg and bookimg.startswith('http'):
                                    link, success = cache_img("book", bookid, bookimg, refresh=refresh)
                                    if success:
                                        controlValueDict = {"BookID": bookid}
                                        newValueDict = {"BookImg": link}
                                        myDB.upsert("books", newValueDict, controlValueDict)
                                        updated = True
                                    else:
                                        logger.debug('Failed to cache image for %s' % bookimg)

                                seriesdict = {}
                                if lazylibrarian.CONFIG['ADD_SERIES']:  # prefer series info from librarything
                                    seriesdict = getWorkSeries(bookid)
                                    if seriesdict:
                                        logger.debug('Updated series: %s [%s]' % (bookid, seriesdict))
                                        updated = True
                                    # librarything doesn't have series info. Any in the title?
                                    elif series:
                                        seriesdict = {cleanName(unaccented(series)): seriesNum}
                                    setSeries(seriesdict, bookid)

                                new_status = setStatus(bookid, seriesdict, bookstatus)

                                if not new_status == book_status:
                                    book_status = new_status
                                    updated = True

                                worklink = getWorkPage(bookid)
                                if worklink:
                                    controlValueDict = {"BookID": bookid}
                                    newValueDict = {"WorkPage": worklink}
                                    myDB.upsert("books", newValueDict, controlValueDict)

                                if not existing_book:
                                    logger.debug("[%s] Added book: %s [%s] status %s" %
                                                 (authorname, bookname, booklang, book_status))
                                    added_count += 1
                                elif updated:
                                    logger.debug("[%s] Updated book: %s [%s] status %s" %
                                                 (authorname, bookname, booklang, book_status))
                                    updated_count += 1
                            else:
                                book_ignore_count += 1
            except KeyError:
                pass

            deleteEmptySeries()
            logger.debug('[%s] The Google Books API was hit %s time%s to populate book list' %
                         (authorname, api_hits, plural(api_hits)))
            cmd = 'SELECT BookName, BookLink, BookDate, BookImg from books WHERE AuthorID=?'
            cmd += ' AND Status != "Ignored" order by BookDate DESC'
            lastbook = myDB.match(cmd, (authorid,))

            if lastbook:  # maybe there are no books [remaining] for this author
                lastbookname = lastbook['BookName']
                lastbooklink = lastbook['BookLink']
                lastbookdate = lastbook['BookDate']
                lastbookimg = lastbook['BookImg']
            else:
                lastbookname = ""
                lastbooklink = ""
                lastbookdate = ""
                lastbookimg = ""

            controlValueDict = {"AuthorID": authorid}
            newValueDict = {
                "Status": entrystatus,
                "LastBook": lastbookname,
                "LastLink": lastbooklink,
                "LastDate": lastbookdate,
                "LastBookImg": lastbookimg
            }

            myDB.upsert("authors", newValueDict, controlValueDict)

            logger.debug("Found %s total book%s for author" % (total_count, plural(total_count)))
            logger.debug("Removed %s unwanted language result%s for author" % (ignored, plural(ignored)))
            logger.debug("Removed %s bad character or no-name result%s for author" %
                         (removedResults, plural(removedResults)))
            logger.debug("Removed %s duplicate result%s for author" % (duplicates, plural(duplicates)))
            logger.debug("Found %s book%s by author marked as Ignored" % (book_ignore_count, plural(book_ignore_count)))
            logger.debug("Imported/Updated %s book%s for author" % (resultcount, plural(resultcount)))

            myDB.action('insert into stats values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        (authorname.replace('"', '""'), api_hits, gr_lang_hits, lt_lang_hits, gb_lang_change,
                         cache_hits, ignored, removedResults, not_cached, duplicates))

            if refresh:
                logger.info("[%s] Book processing complete: Added %s book%s / Updated %s book%s" %
                            (authorname, added_count, plural(added_count), updated_count, plural(updated_count)))
            else:
                logger.info("[%s] Book processing complete: Added %s book%s to the database" %
                            (authorname, added_count, plural(added_count)))

        except Exception:
            logger.error('Unhandled exception in GB.get_author_books: %s' % traceback.format_exc())

    def find_book(self, bookid=None):
        myDB = database.DBConnection()
        if not lazylibrarian.CONFIG['GB_API']:
            logger.warn('No GoogleBooks API key, check config')
        URL = 'https://www.googleapis.com/books/v1/volumes/' + \
              str(bookid) + "?key=" + lazylibrarian.CONFIG['GB_API']
        jsonresults, in_cache = get_json_request(URL)

        if jsonresults is None:
            logger.debug('No results found for %s' % bookid)
            return

        bookname = jsonresults['volumeInfo']['title']
        dic = {':': '.', '"': '', '\'': ''}
        bookname = replace_all(bookname, dic)

        bookname = unaccented(bookname)
        bookname = bookname.strip()  # strip whitespace

        try:
            authorname = jsonresults['volumeInfo']['authors'][0]
        except KeyError:
            logger.debug('Book %s does not contain author field, skipping' % bookname)
            return
        try:
            # warn if language is in ignore list, but user said they wanted this book
            booklang = jsonresults['volumeInfo']['language']
            valid_langs = getList(lazylibrarian.CONFIG['IMP_PREFLANG'])
            if booklang not in valid_langs and 'All' not in valid_langs:
                logger.debug('Book %s googlebooks language does not match preference, %s' % (bookname, booklang))
        except KeyError:
            logger.debug('Book does not have language field')
            booklang = "Unknown"

        try:
            bookpub = jsonresults['volumeInfo']['publisher']
        except KeyError:
            bookpub = ""

        series = ""
        seriesNum = ""
        try:
            booksub = jsonresults['volumeInfo']['subtitle']
            try:
                series = booksub.split('(')[1].split(' Series ')[0]
            except IndexError:
                series = ""
            try:
                seriesNum = booksub.split('(')[1].split(' Series ')[1].split(')')[0]
                if seriesNum[0] == '#':
                    seriesNum = seriesNum[1:]
            except IndexError:
                seriesNum = ""
        except KeyError:
            booksub = ""

        try:
            bookdate = jsonresults['volumeInfo']['publishedDate']
        except KeyError:
            bookdate = '0000-00-00'

        try:
            bookimg = jsonresults['volumeInfo']['imageLinks']['thumbnail']
        except KeyError:
            bookimg = 'images/nocover.png'

        try:
            bookrate = jsonresults['volumeInfo']['averageRating']
        except KeyError:
            bookrate = 0

        try:
            bookpages = jsonresults['volumeInfo']['pageCount']
        except KeyError:
            bookpages = 0

        try:
            bookgenre = jsonresults['volumeInfo']['categories'][0]
        except KeyError:
            bookgenre = ""

        try:
            bookdesc = jsonresults['volumeInfo']['description']
        except KeyError:
            bookdesc = ""

        try:
            if jsonresults['volumeInfo']['industryIdentifiers'][0]['type'] == 'ISBN_10':
                bookisbn = jsonresults['volumeInfo']['industryIdentifiers'][0]['identifier']
            else:
                bookisbn = ""
        except KeyError:
            bookisbn = ""

        booklink = jsonresults['volumeInfo']['canonicalVolumeLink']
        bookrate = float(bookrate)

        GR = GoodReads(authorname)
        author = GR.find_author_id()
        if author:
            AuthorID = author['authorid']
            match = myDB.match('SELECT AuthorID from authors WHERE AuthorID=?', (AuthorID,))
            if not match:
                match = myDB.match('SELECT AuthorID from authors WHERE AuthorName=?', (author['authorname'],))
                if match:
                    logger.debug('%s: Changing authorid from %s to %s' %
                                 (author['authorname'], AuthorID, match['AuthorID']))
                    AuthorID = match['AuthorID']  # we have a different authorid for that authorname
                else:  # no author but request to add book, add author with newauthor status
                    # User hit "add book" button from a search or a wishlist import
                    newauthor_status = 'Active'
                    if lazylibrarian.CONFIG['NEWAUTHOR_STATUS'] in ['Skipped', 'Ignored']:
                        newauthor_status = 'Paused'
                    controlValueDict = {"AuthorID": AuthorID}
                    newValueDict = {
                        "AuthorName": author['authorname'],
                        "AuthorImg": author['authorimg'],
                        "AuthorLink": author['authorlink'],
                        "AuthorBorn": author['authorborn'],
                        "AuthorDeath": author['authordeath'],
                        "DateAdded": today(),
                        "Status": newauthor_status
                    }
                    authorname = author['authorname']
                    myDB.upsert("authors", newValueDict, controlValueDict)
                    if lazylibrarian.CONFIG['NEWAUTHOR_BOOKS']:
                        self.get_author_books(AuthorID, entrystatus=lazylibrarian.CONFIG['NEWAUTHOR_STATUS'])
        else:
            logger.warn("No AuthorID for %s, unable to add book %s" % (authorname, bookname))
            return

        controlValueDict = {"BookID": bookid}
        newValueDict = {
            "AuthorID": AuthorID,
            "BookName": bookname,
            "BookSub": booksub,
            "BookDesc": bookdesc,
            "BookIsbn": bookisbn,
            "BookPub": bookpub,
            "BookGenre": bookgenre,
            "BookImg": bookimg,
            "BookLink": booklink,
            "BookRate": bookrate,
            "BookPages": bookpages,
            "BookDate": bookdate,
            "BookLang": booklang,
            "Status": "Wanted",
            "AudioStatus": lazylibrarian.CONFIG['NEWAUDIO_STATUS'],
            "BookAdded": today()
        }

        myDB.upsert("books", newValueDict, controlValueDict)
        logger.info("%s by %s added to the books database" % (bookname, authorname))

        if 'nocover' in bookimg or 'nophoto' in bookimg:
            # try to get a cover from librarything
            workcover = getBookCover(bookid)
            if workcover:
                logger.debug('Updated cover for %s to %s' % (bookname, workcover))
                controlValueDict = {"BookID": bookid}
                newValueDict = {"BookImg": workcover}
                myDB.upsert("books", newValueDict, controlValueDict)

            elif bookimg and bookimg.startswith('http'):
                link, success = cache_img("book", bookid, bookimg)
                if success:
                    controlValueDict = {"BookID": bookid}
                    newValueDict = {"BookImg": link}
                    myDB.upsert("books", newValueDict, controlValueDict)
                else:
                    logger.debug('Failed to cache image for %s' % bookimg)

        if lazylibrarian.CONFIG['ADD_SERIES']:
            # prefer series info from librarything
            seriesdict = getWorkSeries(bookid)
            if seriesdict:
                logger.debug('Updated series: %s [%s]' % (bookid, seriesdict))
            else:
                if series:
                    seriesdict = {cleanName(unaccented(series)): seriesNum}
            setSeries(seriesdict, bookid)

        worklink = getWorkPage(bookid)
        if worklink:
            controlValueDict = {"BookID": bookid}
            newValueDict = {"WorkPage": worklink}
            myDB.upsert("books", newValueDict, controlValueDict)

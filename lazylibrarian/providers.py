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

import time
import datetime
import urllib
import threading
from xml.etree import ElementTree

import lazylibrarian
import lib.feedparser as feedparser
from lazylibrarian import logger
from lazylibrarian.cache import fetchURL
from lazylibrarian.directparser import GEN
from lazylibrarian.formatter import age, today, plural, cleanName, unaccented, getList, check_int, makeUnicode
from lazylibrarian.torrentparser import KAT, TPB, ZOO, TDL, LIME


def test_provider(name):
    book = {'searchterm': 'Agatha+Christie', 'library': 'eBook'}
    if name == 'TPB':
        return TPB(book, test=True), "Pirate Bay"
    if name == 'KAT':
        return KAT(book, test=True), "KickAss Torrents"
    if name == 'ZOO':
        return ZOO(book, test=True), "Zooqle"
    if name == 'LIME':
        return LIME(book, test=True), "LimeTorrents"
    if name == 'TDL':
        return TDL(book, test=True), "TorrentDownloads"
    if name == 'GEN':
        return GEN(book, prov='GEN', test=True), "LibGen 1"
    if name == 'GEN2':
        return GEN(book, prov='GEN2', test=True), "LibGen 2"
    if name.startswith('rss['):
        try:
            prov = name.split('[')[1].split(']')[0]
            for provider in lazylibrarian.RSS_PROV:
                if provider['NAME'] == 'RSS_%s' % prov and provider['HOST']:
                    return RSS(provider['HOST'], provider['NAME'], provider['DLPRIORITY']), provider['NAME']
        except IndexError:
            pass

    # for torznab/newznab try book search if enabled, fall back to general search
    book.update({'authorName': 'Agatha Christie', 'bookName': 'Poirot', 'bookSub': ''})
    if name.startswith('torznab['):
        try:
            prov = name.split('[')[1].split(']')[0]
            for provider in lazylibrarian.TORZNAB_PROV:
                if provider['NAME'] == 'Torznab%s' % prov and provider['HOST']:
                    return NewzNabPlus(book, provider, 'book', 'torznab', True), provider['NAME']
        except IndexError:
            pass
    if name.startswith('newznab['):
        try:
            prov = name.split('[')[1].split(']')[0]
            for provider in lazylibrarian.NEWZNAB_PROV:
                if provider['NAME'] == 'Newznab%s' % prov and provider['HOST']:
                    return NewzNabPlus(book, provider, 'book', 'nzb', True), provider['NAME']
        except IndexError:
            pass
    msg = "Unknown provider [%s]" % name
    logger.error(msg)
    return False, msg


def get_searchterm(book, searchType):
    authorname = cleanName(book['authorName'], "'")
    bookname = cleanName(book['bookName'], "'")
    if searchType in ['book', 'audio'] or 'short' in searchType:
        if bookname == authorname and book['bookSub']:
            # books like "Spike Milligan: Man of Letters"
            # where we split the title/subtitle on ':'
            bookname = cleanName(book['bookSub'])
        if bookname.startswith(authorname) and len(bookname) > len(authorname):
            # books like "Spike Milligan In his own words"
            # where we don't want to look for "Spike Milligan Spike Milligan In his own words"
            bookname = bookname[len(authorname) + 1:]
        bookname = bookname.strip()

        # no initials or extensions after surname eg L. E. Modesitt Jr. -> Modesitt
        # and Charles H. Elliott, Phd -> Charles Elliott
        # but Tom Holt -> Tom Holt
        # Calibre directories may have trailing '.' replaced by '_'  eg Jr_
        if ' ' in authorname:
            authorname_exploded = authorname.split(' ')
            authorname = ''
            postfix = getList(lazylibrarian.CONFIG['NAME_POSTFIX'])
            for word in authorname_exploded:
                word = word.strip('.').strip('_')
                if len(word) > 1 and word.lower() not in postfix:
                    if authorname:
                        authorname += ' '
                    authorname += word

        if 'short' in searchType and '(' in bookname:
            bookname = bookname.split('(')[0].strip()

    return authorname, bookname


def get_capabilities(provider, force=False):
    """
    query provider for caps if none loaded yet, or if config entry is too old and not set manually.
    """
    if not force and len(provider['UPDATED']) == 10:  # any stored values?
        match = True
        if (age(provider['UPDATED']) > lazylibrarian.CONFIG['CACHE_AGE']) and not provider['MANUAL']:
            logger.debug('Stored capabilities for %s are too old' % provider['HOST'])
            match = False
    else:
        match = False

    if match:
        logger.debug('Using stored capabilities for %s' % provider['HOST'])
    else:
        host = provider['HOST']
        if not str(host)[:4] == "http":
            host = 'http://' + host
        if host[-1:] == '/':
            host = host[:-1]
        URL = host + '/api?t=caps'

        # most providers will give you caps without an api key
        logger.debug('Requesting capabilities for %s' % URL)
        source_xml, success = fetchURL(URL)
        # If it failed, retry with api key
        if not success:
            if provider['API']:
                URL = URL + '&apikey=' + provider['API']
                logger.debug('Requesting capabilities for %s' % URL)
                source_xml, success = fetchURL(URL)
        if success:
            try:
                data = ElementTree.fromstring(source_xml)
            except ElementTree.ParseError:
                data = ''
                logger.debug("Error parsing xml from %s, %s" % (URL, source_xml))
        else:
            logger.debug("Error getting xml from %s, %s" % (URL, source_xml))
            data = ''
        if len(data):
            logger.debug("Parsing xml for capabilities of %s" % URL)

            #
            # book search isn't mentioned in the caps xml returned by
            # nzbplanet,jackett,oznzb,usenet-crawler, so we can't use it as a test
            # but the newznab+ ones usually support t=book and categories in 7000 range
            # whereas nZEDb ones don't support t=book and use categories in 8000 range
            # also some providers give searchtype but no supportedparams, so we still
            # can't tell what queries will be accepted
            # also category names can be lowercase or Mixed, magazine subcat name isn't
            # consistent, and subcat can be just subcat or category/subcat subcat > lang
            # eg "Magazines" "Mags" or "Books/Magazines" "Mags > French"
            # Load all languages for now as we don't know which the user might want
            #
            #
            #  set some defaults
            #
            provider['GENERALSEARCH'] = 'search'
            provider['EXTENDED'] = '1'
            provider['BOOKCAT'] = ''
            provider['MAGCAT'] = ''
            provider['AUDIOCAT'] = ''
            provider['BOOKSEARCH'] = ''
            provider['MAGSEARCH'] = ''
            provider['AUDIOSEARCH'] = ''
            #
            search = data.find('searching/search')
            if search is not None:
                # noinspection PyUnresolvedReferences
                if 'available' in search.attrib:
                    # noinspection PyUnresolvedReferences
                    if search.attrib['available'] == 'yes':
                        provider['GENERALSEARCH'] = 'search'
            categories = data.getiterator('category')
            for cat in categories:
                if 'name' in cat.attrib:
                    if cat.attrib['name'].lower() == 'audio':
                        provider['AUDIOCAT'] = cat.attrib['id']
                        subcats = cat.getiterator('subcat')
                        for subcat in subcats:
                            if 'audiobook' in subcat.attrib['name'].lower():
                                provider['AUDIOCAT'] = "%s,%s" % (provider['AUDIOCAT'], subcat.attrib['id'])

                    elif cat.attrib['name'].lower() == 'books':
                        bookcat = cat.attrib['id']  # keep main bookcat for starting magazines later
                        provider['BOOKCAT'] = bookcat
                        provider['MAGCAT'] = ''
                        # set default booksearch
                        if provider['BOOKCAT'] == '7000':
                            # looks like newznab+, should support book-search
                            provider['BOOKSEARCH'] = 'book'
                        else:
                            # looks like nZEDb, probably no book-search
                            provider['BOOKSEARCH'] = ''
                        # but check in case we got some settings back
                        search = data.find('searching/book-search')
                        if search:
                            # noinspection PyUnresolvedReferences
                            if 'available' in search.attrib:
                                # noinspection PyUnresolvedReferences
                                if search.attrib['available'] == 'yes':
                                    provider['BOOKSEARCH'] = 'book'
                                else:
                                    provider['BOOKSEARCH'] = ''

                        subcats = cat.getiterator('subcat')
                        for subcat in subcats:
                            if 'ebook' in subcat.attrib['name'].lower():
                                provider['BOOKCAT'] = "%s,%s" % (provider['BOOKCAT'], subcat.attrib['id'])
                            if 'magazines' in subcat.attrib['name'].lower() or 'mags' in subcat.attrib['name'].lower():
                                if provider['MAGCAT']:
                                    provider['MAGCAT'] = "%s,%s" % (provider['MAGCAT'], subcat.attrib['id'])
                                else:
                                    provider['MAGCAT'] = subcat.attrib['id']
                        # if no specific magazine subcategory, use books
                        if not provider['MAGCAT']:
                            provider['MAGCAT'] = bookcat
            logger.debug("Categories: Books %s : Mags %s : Audio %s" %
                         (provider['BOOKCAT'], provider['MAGCAT'], provider['AUDIOCAT']))
            provider['UPDATED'] = today()
            threadname = threading.currentThread().name
            lazylibrarian.config_write()
            threading.currentThread().name = threadname
        else:
            logger.warn("Unable to get capabilities for %s: No data returned" % URL)
    return provider


def ProviderIsBlocked(name):
    """ Check if provider is blocked because of previous errors """
    timenow = int(time.time())
    for entry in lazylibrarian.PROVIDER_BLOCKLIST:
        if entry["name"] == name:
            if timenow < int(entry['resume']):
                return True
            else:
                lazylibrarian.PROVIDER_BLOCKLIST.remove(entry)
    return False


def BlockProvider(who, why):
    delay = check_int(lazylibrarian.CONFIG['BLOCKLIST_TIMER'], 3600)
    if len(why) > 40:
        why = why[:40] + '...'
    if delay == 0:
        logger.debug('Not blocking %s,%s as timer is zero' % (who, why))
    else:
        mins = int(delay / 60) + (delay % 60 > 0)
        logger.info("Blocking provider %s for %s minutes because %s" % (who, mins, why))
        timenow = int(time.time())
        for entry in lazylibrarian.PROVIDER_BLOCKLIST:
            if entry["name"] == who:
                lazylibrarian.PROVIDER_BLOCKLIST.remove(entry)
        newentry = {"name": who, "resume": timenow + delay, "reason": why}
        lazylibrarian.PROVIDER_BLOCKLIST.append(newentry)
    logger.debug("Provider Blocklist contains %s entries" % len(lazylibrarian.PROVIDER_BLOCKLIST))


def IterateOverNewzNabSites(book=None, searchType=None):
    """
    Purpose of this function is to read the config file, and loop through all active NewsNab+
    sites and return the compiled results list from all sites back to the caller
    We get called with book[] and searchType of "book", "mag", "general" etc
    """

    resultslist = []
    providers = 0

    for provider in lazylibrarian.NEWZNAB_PROV:
        if provider['ENABLED']:
            if ProviderIsBlocked(provider['HOST']):
                logger.debug('[IterateOverNewzNabSites] - %s is BLOCKED' % provider['HOST'])
            else:
                provider = get_capabilities(provider)
                providers += 1
                logger.debug('[IterateOverNewzNabSites] - %s' % provider['HOST'])
                resultslist += NewzNabPlus(book, provider, searchType, "nzb")

    for provider in lazylibrarian.TORZNAB_PROV:
        if provider['ENABLED']:
            if ProviderIsBlocked(provider['HOST']):
                logger.debug('[IterateOverNewzNabSites] - %s is BLOCKED' % provider['HOST'])
            else:
                provider = get_capabilities(provider)
                providers += 1
                logger.debug('[IterateOverTorzNabSites] - %s' % provider['HOST'])
                resultslist += NewzNabPlus(book, provider, searchType, "torznab")

    return resultslist, providers


def IterateOverTorrentSites(book=None, searchType=None):
    resultslist = []
    providers = 0
    if searchType != 'mag' and searchType != 'general':
        authorname, bookname = get_searchterm(book, searchType)
        book['searchterm'] = authorname + ' ' + bookname

    for prov in ['KAT', 'TPB', 'ZOO', 'TDL', 'LIME']:
        if lazylibrarian.CONFIG[prov]:
            if ProviderIsBlocked(prov):
                logger.debug('[IterateOverTorrentSites] - %s is BLOCKED' % lazylibrarian.CONFIG[prov + '_HOST'])
            else:
                logger.debug('[IterateOverTorrentSites] - %s' % lazylibrarian.CONFIG[prov + '_HOST'])
                if prov == 'KAT':
                    results, error = KAT(book)
                elif prov == 'TPB':
                    results, error = TPB(book)
                elif prov == 'ZOO':
                    results, error = ZOO(book)
                # elif prov == 'EXTRA':
                #    results, error = EXTRA(book)
                elif prov == 'TDL':
                    results, error = TDL(book)
                elif prov == 'LIME':
                    results, error = LIME(book)
                else:
                    results = ''
                    error = ''
                    logger.error('IterateOverTorrentSites called with unknown provider [%s]' % prov)

                if error:
                    BlockProvider(prov, error)
                else:
                    resultslist += results
                    providers += 1

    return resultslist, providers


def IterateOverDirectSites(book=None, searchType=None):
    resultslist = []
    providers = 0
    if searchType != 'mag' and searchType != 'general':
        authorname, bookname = get_searchterm(book, searchType)
        book['searchterm'] = authorname + ' ' + bookname

    for prov in ['GEN', 'GEN2']:
        if lazylibrarian.CONFIG[prov]:
            if ProviderIsBlocked(prov):
                logger.debug('[IterateOverDirectSites] - %s %s is BLOCKED' % (lazylibrarian.CONFIG[prov + '_HOST'],
                                                                              lazylibrarian.CONFIG[prov + '_SEARCH']))
            else:
                logger.debug('[IterateOverDirectSites] - %s %s' % (lazylibrarian.CONFIG[prov + '_HOST'],
                                                                   lazylibrarian.CONFIG[prov + '_SEARCH']))
                results, error = GEN(book, prov)
                if error:
                    BlockProvider(prov, error)
                else:
                    resultslist += results
                    providers += 1

    return resultslist, providers


def IterateOverRSSSites():
    resultslist = []
    providers = 0
    for provider in lazylibrarian.RSS_PROV:
        if provider['ENABLED'] and 'goodreads' not in provider['HOST'] and 'list_rss' not in provider['HOST']:
            if ProviderIsBlocked(provider['HOST']):
                logger.debug('[IterateOverRSSSites] - %s is BLOCKED' % provider['HOST'])
            else:
                providers += 1
                logger.debug('[IterateOverRSSSites] - %s' % provider['HOST'])
                resultslist += RSS(provider['HOST'], provider['NAME'], provider['DLPRIORITY'])

    return resultslist, providers


def IterateOverWishLists():
    # Two types of wishlists handled
    # GoodReads rss feeds
    # GoodReads Listopia html pages
    resultslist = []
    providers = 0
    for provider in lazylibrarian.RSS_PROV:
        if provider['ENABLED'] and 'goodreads' in provider['HOST'] and 'list_rss' in provider['HOST']:
            if ProviderIsBlocked(provider['HOST']):
                logger.debug('[IterateOverWishLists] - %s is BLOCKED' % provider['HOST'])
            else:
                providers += 1
                logger.debug('[IterateOverWishLists] - %s' % provider['HOST'])
                resultslist += GOODREADS(provider['HOST'], provider['NAME'], provider['DLPRIORITY'])
        elif provider['ENABLED'] and 'goodreads' in provider['HOST'] and '/list/show/' in provider['HOST']:
            if ProviderIsBlocked(provider['HOST']):
                logger.debug('[IterateOverWishLists] - %s is BLOCKED' % provider['HOST'])
            else:
                providers += 1
                logger.debug('[IterateOverWishLists] - %s' % provider['HOST'])
                resultslist += LISTOPIA(provider['HOST'], provider['NAME'], provider['DLPRIORITY'])

    return resultslist, providers


def LISTOPIA(host=None, feednr=None, priority=0):
    """
    Goodreads Listopia query function, return all the results in a list
    """
    results = []
    maxpage = priority
    basehost = host
    if not str(host)[:4] == "http":
        host = 'http://' + host

    page = 0
    next_page = True

    while next_page:
        URL = host
        if page:
            URL = "%s?page=%i" % (host, page)

        result, success = fetchURL(URL)
        next_page = False

        if not success:
            logger.error('Error fetching data from %s: %s' % (URL, result))
            BlockProvider(basehost, result)

        elif result:
            logger.debug('Parsing results from %s' % URL)
            data = result.split('<td valign="top" class="number">')
            for entry in data[1:]:
                try:
                    # index = entry.split('<')[0]
                    title = entry.split('<a title="')[1].split('"')[0]
                    book_id = entry.split('data-resource-id="')[1].split('"')[0]
                    author_name = entry.split('<a class="authorName"')[1].split('"name">')[1].split('<')[0]
                    results.append({
                        'rss_prov': host.split('/list/show/')[1],
                        'rss_feed': feednr,
                        'rss_title': title,
                        'rss_author': author_name,
                        'rss_bookid': book_id,
                        'rss_isbn': '',
                        'priority': priority
                    })
                    next_page = True
                except IndexError:
                    pass
        else:
            logger.debug('No data returned from %s' % URL)

        page += 1
        if maxpage:
            if page >= maxpage:
                logger.warn('Maximum results page reached, still more results available')
                next_page = False

    logger.debug("Found %i result%s from %s" % (len(results), plural(len(results)), host))
    return results


def GOODREADS(host=None, feednr=None, priority=0):
    """
    Goodreads RSS query function, return all the results in a list, can handle multiple wishlists
    but expects goodreads format (looks for goodreads category names)
    """
    results = []
    basehost = host
    if not str(host)[:4] == "http":
        host = 'http://' + host

    URL = host

    result, success = fetchURL(URL)
    if success:
        data = feedparser.parse(result)
    else:
        logger.error('Error fetching data from %s: %s' % (host, result))
        BlockProvider(basehost, result)
        return []

    if data:
        logger.debug('Parsing results from %s' % URL)
        provider = data['feed']['link']
        logger.debug("RSS %s returned %i result%s" % (provider, len(data.entries), plural(len(data.entries))))
        for post in data.entries:
            title = ''
            book_id = ''
            author_name = ''
            isbn = ''
            if 'title' in post:
                title = post.title
            if 'book_id' in post:
                book_id = post.book_id
            if 'author_name' in post:
                author_name = post.author_name
            if 'isbn' in post:
                isbn = post.isbn
            if title and author_name:
                results.append({
                    'rss_prov': provider,
                    'rss_feed': feednr,
                    'rss_title': title,
                    'rss_author': author_name,
                    'rss_bookid': book_id,
                    'rss_isbn': isbn,
                    'priority': priority
                })
    else:
        logger.debug('No data returned from %s' % host)
    return results


def RSS(host=None, feednr=None, priority=0, test=False):
    """
    Generic RSS query function, just return all the results from the RSS feed in a list
    """
    results = []

    URL = host
    if not str(URL)[:4] == "http":
        URL = 'http://' + URL

    result, success = fetchURL(URL)

    if test:
        return success

    if success:
        data = feedparser.parse(result)
    else:
        logger.error('Error fetching data from %s: %s' % (host, result))
        BlockProvider(host, result)
        data = None

    if data:
        # to debug because of api
        logger.debug('Parsing results from %s' % URL)
        provider = data['feed']['link']
        logger.debug("RSS %s returned %i result%s" % (provider, len(data.entries), plural(len(data.entries))))
        for post in data.entries:
            title = None
            magnet = None
            size = None
            torrent = None
            nzb = None
            url = None
            tortype = 'torrent'

            if 'title' in post:
                title = post.title
            if 'links' in post:
                for f in post.links:
                    if 'x-bittorrent' in f['type']:
                        size = f['length']
                        torrent = f['href']
                        break
                    if 'x-nzb' in f['type']:
                        size = f['length']
                        nzb = f['href']
                        break

            if 'torrent_magneturi' in post:
                magnet = post.torrent_magneturi

            if torrent:
                url = torrent
                tortype = 'torrent'

            if magnet:
                if not url or (url and lazylibrarian.CONFIG['PREFER_MAGNET']):
                    url = magnet
                    tortype = 'magnet'

            if nzb:  # prefer nzb over torrent/magnet
                url = nzb
                tortype = 'nzb'

            if not url:
                if 'link' in post:
                    url = post.link

            tor_date = 'Fri, 01 Jan 1970 00:00:00 +0100'
            if 'newznab_attr' in post:
                if post.newznab_attr['name'] == 'usenetdate':
                    tor_date = post.newznab_attr['value']

            if not size:
                size = 1000
            if title and url:
                results.append({
                    'tor_prov': provider,
                    'tor_title': title,
                    'tor_url': url,
                    'tor_size': str(size),
                    'tor_date': tor_date,
                    'tor_feed': feednr,
                    'tor_type': tortype,
                    'priority': priority
                })
    else:
        logger.debug('No data returned from %s' % host)
    return results


def cancelSearchType(searchType, errorMsg, provider):
    """ See if errorMsg contains a known error response for an unsupported search function
        depending on which searchType. If it does, disable that searchtype for the relevant provider
        return True if cancelled
    """
    errorlist = ['no such function', 'unknown parameter', 'unknown function',
                 'bad request', 'bad_request', 'incorrect parameter', 'does not support']

    errormsg = errorMsg.lower()
    if (provider['BOOKSEARCH'] and searchType in ["book", "shortbook"]) or \
            (provider['AUDIOSEARCH'] and searchType in ["audio", "shortaudio"]):
        match = False
        for item in errorlist:
            if item in errormsg:
                match = True
                break
        if match:
            if searchType in ["book", "shortbook"]:
                msg = 'BOOKSEARCH'
            elif searchType in ["audio", "shortaudio"]:
                msg = 'AUDIOSEARCH'
            else:
                msg = ''

            if msg:
                for providerlist in [lazylibrarian.NEWZNAB_PROV, lazylibrarian.TORZNAB_PROV]:
                    count = 0
                    while count < len(providerlist):
                        if providerlist[count]['HOST'] == provider['HOST']:
                            if str(provider['MANUAL']) == 'False':
                                logger.error("Disabled %s=%s for %s" % (msg, provider[msg], provider['HOST']))
                                providerlist[count][msg] = ""
                                threadname = threading.currentThread().name
                                lazylibrarian.config_write()
                                threading.currentThread().name = threadname
                                return True
                        count += 1
            logger.error('Unable to disable searchtype [%s] for %s' % (searchType, provider['HOST']))
    return False


def NewzNabPlus(book=None, provider=None, searchType=None, searchMode=None, test=False):
    """
    Generic NewzNabplus query function
    takes in host+key+type and returns the result set regardless of who
    based on site running NewzNab+
    ref http://usenetreviewz.com/nzb-sites/
    """

    host = provider['HOST']
    api_key = provider['API']
    logger.debug('[NewzNabPlus] searchType [%s] with Host [%s] mode [%s] using api [%s] for item [%s]' % (
        searchType, host, searchMode, api_key, str(book)))

    results = []

    params = ReturnSearchTypeStructure(provider, api_key, book, searchType, searchMode)

    if params:
        if not str(host)[:4] == "http":
            host = 'http://' + host
        if host[-1:] == '/':
            host = host[:-1]
        URL = host + '/api?' + urllib.urlencode(params)

        sterm = makeUnicode(book['searchterm'])

        rootxml = None
        logger.debug("[NewzNabPlus] URL = %s" % URL)
        result, success = fetchURL(URL)

        if test:
            if result.startswith('<') and result.endswith('/>') and "error code" in result:
                result = result[1:-2]
                success = False
            if not success:
                logger.debug(result)
            return success

        if success:
            try:
                rootxml = ElementTree.fromstring(result)
            except Exception as e:
                logger.error('Error parsing data from %s: %s %s' % (host, type(e).__name__, str(e)))
                rootxml = None
        else:
            if not result or result == "''":
                result = "Got an empty response"
            logger.error('Error reading data from %s: %s' % (host, result))
            # maybe the host doesn't support the search type
            cancelled = cancelSearchType(searchType, result, provider)
            if not cancelled:  # it was some other problem
                BlockProvider(provider['HOST'], result)

        if rootxml is not None:
            # to debug because of api
            logger.debug('Parsing results from <a href="%s">%s</a>' % (URL, host))

            if rootxml.tag == 'error':
                errormsg = rootxml.get('description', default='unknown error')
                logger.error("%s - %s" % (host, errormsg))
                # maybe the host doesn't support the search type
                cancelled = cancelSearchType(searchType, errormsg, provider)
                if not cancelled:  # it was some other problem
                    BlockProvider(provider['HOST'], errormsg)
            else:
                resultxml = rootxml.getiterator('item')
                nzbcount = 0
                maxage = check_int(lazylibrarian.CONFIG['USENET_RETENTION'], 0)
                for nzb in resultxml:
                    try:
                        thisnzb = ReturnResultsFieldsBySearchType(book, nzb, host, searchMode, provider['DLPRIORITY'])
                        if not maxage:
                            nzbcount += 1
                            results.append(thisnzb)
                        else:
                            # example nzbdate format: Mon, 27 May 2013 02:12:09 +0200
                            nzbdate = thisnzb['nzbdate']
                            try:
                                parts = nzbdate.split(' ')
                                nzbdate = ' '.join(parts[:5])  # strip the +0200
                                dt = datetime.datetime.strptime(nzbdate, "%a, %d %b %Y %H:%M:%S").timetuple()
                                nzbage = age('%04d-%02d-%02d' % (dt.tm_year, dt.tm_mon, dt.tm_mday))
                            except Exception as e:
                                logger.debug('Unable to get age from [%s] %s %s' %
                                             (thisnzb['nzbdate'], type(e).__name__, str(e)))
                                nzbage = 0
                            if nzbage <= maxage:
                                nzbcount += 1
                                results.append(thisnzb)
                            else:
                                logger.debug('%s is too old (%s day%s)' % (thisnzb['nzbtitle'], nzbage, plural(nzbage)))

                    except IndexError:
                        logger.debug('No results from %s for %s' % (host, sterm))
                logger.debug('Found %s nzb at %s for: %s' % (nzbcount, host, sterm))
        else:
            logger.debug('No data returned from %s for %s' % (host, sterm))
    return results


def ReturnSearchTypeStructure(provider, api_key, book, searchType, searchMode):
    params = None
    if searchType in ["book", "shortbook"]:
        authorname, bookname = get_searchterm(book, searchType)
        if provider['BOOKSEARCH'] and provider['BOOKCAT']:  # if specific booksearch, use it
            params = {
                "t": provider['BOOKSEARCH'],
                "apikey": api_key,
                "title": bookname,
                "author": authorname,
                "cat": provider['BOOKCAT']
            }
        elif provider['GENERALSEARCH'] and provider['BOOKCAT']:  # if not, try general search
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "q": authorname + ' ' + bookname,
                "cat": provider['BOOKCAT']
            }
    elif searchType in ["audio", "shortaudio"]:
        authorname, bookname = get_searchterm(book, searchType)
        if provider['AUDIOSEARCH'] and provider['AUDIOCAT']:  # if specific audiosearch, use it
            params = {
                "t": provider['AUDIOSEARCH'],
                "apikey": api_key,
                "title": bookname,
                "author": authorname,
                "cat": provider['AUDIOCAT']
            }
        elif provider['GENERALSEARCH'] and provider['AUDIOCAT']:  # if not, try general search
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "q": authorname + ' ' + bookname,
                "cat": provider['AUDIOCAT']
            }
    elif searchType == "mag":
        if provider['MAGSEARCH'] and provider['MAGCAT']:  # if specific magsearch, use it
            params = {
                "t": provider['MAGSEARCH'],
                "apikey": api_key,
                "cat": provider['MAGCAT'],
                "q": unaccented(book['searchterm'].replace(':', '')),
                "extended": provider['EXTENDED'],
            }
        elif provider['GENERALSEARCH'] and provider['MAGCAT']:
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "cat": provider['MAGCAT'],
                "q": unaccented(book['searchterm'].replace(':', '')),
                "extended": provider['EXTENDED'],
            }
    else:
        if provider['GENERALSEARCH']:
            if searchType == "shortgeneral":
                searchterm = unaccented(book['searchterm'].split('(')[0].replace(':', ''))
            else:
                searchterm = unaccented(book['searchterm'].replace(':', ''))
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "q": searchterm,
                "extended": provider['EXTENDED'],
            }
    if params:
        logger.debug('[NewzNabPlus] - %s Search parameters set to %s' % (searchMode, str(params)))
    else:
        logger.debug('[NewzNabPlus] - %s No matching search parameters for %s' % (searchMode, searchType))

    return params


def ReturnResultsFieldsBySearchType(book=None, nzbdetails=None, host=None, searchMode=None, priority=0):
    """
    # searchType has multiple query params for t=, which return different results sets.
    # books have a dedicated check, so will use that.
    # mags don't so will have more generic search term.
    # http://newznab.readthedocs.org/en/latest/misc/api/#predefined-categories
    # results when searching for t=book
    #    <item>
    #       <title>David Gemmell - Troy 03 - Fall of Kings</title>
    #       <guid isPermaLink="true">
    #           https://www.usenet-crawler.com/details/091c8c0e18ca34201899b91add52e8c0
    #       </guid>
    #       <link>
    #           https://www.usenet-crawler.com/getnzb/091c8c0e18ca34201899b91add52e8c0.nzb&i=155518&r=78c0509
    #       </link>
    #       <comments>
    # https://www.usenet-crawler.com/details/091c8c0e18ca34201899b91add52e8c0#comments
    #       </comments>
    #       <pubDate>Fri, 11 Jan 2013 16:49:34 +0100</pubDate>
    #       <category>Books > Ebook</category>
    #       <description>David Gemmell - Troy 03 - Fall of Kings</description>
    #       <enclosure url="https://www.usenet-crawler.com/getnzb/091c8c0e18ca34201899b91add52e8c0.nzb&i=155518&r=78c0>
    #       <newznab:attr name="category" value="7000"/>
    #       <newznab:attr name="category" value="7020"/>
    #       <newznab:attr name="size" value="4909563"/>
    #       <newznab:attr name="guid" value="091c8c0e18ca34201899b91add52e8c0"/>
    #       </item>
    #
    # t=search results
    # <item>
    #   <title>David Gemmell - [Troy 03] - Fall of Kings</title>
    #   <guid isPermaLink="true">
    #       https://www.usenet-crawler.com/details/5d7394b2386683d079d8bd8f16652b18
    #   </guid>
    #   <link>
    #       https://www.usenet-crawler.com/getnzb/5d7394b2386683d079d8bd8f16652b18.nzb&i=155518&r=78c0509bc6bb9174
    #   </link>
    #   <comments>
    # https://www.usenet-crawler.com/details/5d7394b2386683d079d8bd8f16652b18#comments
    #   </comments>
    #   <pubDate>Mon, 27 May 2013 02:12:09 +0200</pubDate>
    #   <category>Books > Ebook</category>
    #   <description>David Gemmell - [Troy 03] - Fall of Kings</description>
    #   <enclosure url="https://www.usenet-crawler.com/getnzb/5d7394b2386683d079d8bd8f16652b18.nzb&i=155518&r=78c05>
    #   <newznab:attr name="category" value="7000"/>
    #   <newznab:attr name="category" value="7020"/>
    #   <newznab:attr name="size" value="4909563"/>
    #   <newznab:attr name="guid" value="5d7394b2386683d079d8bd8f16652b18"/>
    #   <newznab:attr name="files" value="2"/>
    #   <newznab:attr name="poster" value="nerdsproject@gmail.com (N.E.R.Ds)"/>
    #   <newznab:attr name="grabs" value="0"/>
    #   <newznab:attr name="comments" value="0"/>
    #   <newznab:attr name="password" value="0"/>
    #   <newznab:attr name="usenetdate" value="Fri, 11 Mar 2011 13:45:15 +0100"/>
    #   <newznab:attr name="group" value="alt.binaries.e-book.flood"/>
    # </item>
    # -------------------------------TORZNAB RETURN DATA-- book ---------------------------------------------
    # <item>
    #  <title>Tom Holt - Blonde Bombshell (Dystop; SFX; Humour) ePUB+MOBI</title>
    #  <guid>https://getstrike.net/torrents/1FDBE6466738EED3C7FD915E1376BA0A63088D4D</guid>
    #  <comments>https://getstrike.net/torrents/1FDBE6466738EED3C7FD915E1376BA0A63088D4D</comments>
    #  <pubDate>Sun, 27 Sep 2015 23:10:56 +0200</pubDate>
    #  <size>24628</size>
    #  <description>Tom Holt - Blonde Bombshell (Dystop; SFX; Humour) ePUB+MOBI</description>
    #  <link>http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2UubmV0L3RvcnJl
    #  bnRzL2FwaS9kb3dubG9hZC8xRkRCRTY0NjY3MzhFRUQzQzdGRDkxNUUxMzc2QkEwQTYzMDg4RDRELnRvcnJlbnQ1/t.torrent</link>
    #  <category>8000</category>
    #  <enclosure url="http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2UubmV
    #  0L3RvcnJlbnRzL2FwaS9kb3dubG9hZC8xRkRCRTY0NjY3MzhFRUQzQzdGRDkxNUUxMzc2QkEwQTYzMDg4RDRELnRvcnJlbnQ1/t.torrent"
    #  length="24628" type="application/x-bittorrent" />
    #  <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:1FDBE6466738EED3C7FD915E1376BA0A63088D4D&amp;
    #  dn=Tom+Holt+-+Blonde+Bombshell+(Dystop%3B+SFX%3B+Humour)+ePUB%2BMOBI&amp;tr=udp://open.demonii.com:1337&amp;
    #  tr=udp://tracker.coppersurfer.tk:6969&amp;tr=udp://tracker.leechers-paradise.org:6969&amp;
    #  tr=udp://exodus.desync.com:6969" />
    #  <torznab:attr name="seeders" value="1" />
    #  <torznab:attr name="peers" value="2" />
    #  <torznab:attr name="infohash" value="1FDBE6466738EED3C7FD915E1376BA0A63088D4D" />
    #  <torznab:attr name="minimumratio" value="1" />
    #  <torznab:attr name="minimumseedtime" value="172800" />
    # </item>
    # ---------------------------------------- magazine ----------------------------------------
    # <item>
    #  <title>Linux Format Issue 116 - KDE Issue</title>
    #  <guid>https://getstrike.net/torrents/f3fc8df4fdd850132072a435a7d112d6c9d77d16</guid>
    #  <comments>https://getstrike.net/torrents/f3fc8df4fdd850132072a435a7d112d6c9d77d16</comments>
    #  <pubDate>Wed, 04 Mar 2009 01:57:20 +0100</pubDate>
    #  <size>1309195</size>
    #  <description>Linux Format Issue 116 - KDE Issue</description>
    #  <link>http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2UubmV0L3R
    #  vcnJlbnRzL2FwaS9kb3dubG9hZC9mM2ZjOGRmNGZkZDg1MDEzMjA3MmE0MzVhN2QxMTJkNmM5ZDc3ZDE2LnRvcnJlbnQ1/t.torrent</link>
    #  <enclosure url="http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2Uubm
    #  V0L3RvcnJlbnRzL2FwaS9kb3dubG9hZC9mM2ZjOGRmNGZkZDg1MDEzMjA3MmE0MzVhN2QxMTJkNmM5ZDc3ZDE2LnRvcnJlbnQ1/t.torrent"
    #  length="1309195" type="application/x-bittorrent" />
    #  <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:f3fc8df4fdd850132072a435a7d112d6c9d77d16&amp;
    #  dn=Linux+Format+Issue+116+-+KDE+Issue&amp;tr=udp://open.demonii.com:1337&amp;tr=udp://tracker.coppersurfer.
    #  tk:6969&amp;tr=udp://tracker.leechers-paradise.org:6969&amp;tr=udp://exodus.desync.com:6969" />
    #  <torznab:attr name="seeders" value="2" />
    #  <torznab:attr name="peers" value="3" />
    #  <torznab:attr name="infohash" value="f3fc8df4fdd850132072a435a7d112d6c9d77d16" />
    #  <torznab:attr name="minimumratio" value="1" />
    #  <torznab:attr name="minimumseedtime" value="172800" />
    #  </item>
    """

    nzbtitle = ''
    nzbdate = ''
    nzburl = ''
    nzbsize = 0

    n = 0
    while n < len(nzbdetails):
        tag = str(nzbdetails[n].tag).lower()

        if tag == 'title':
            nzbtitle = nzbdetails[n].text
        elif tag == 'size':
            nzbsize = nzbdetails[n].text
        elif tag == 'pubdate':
            nzbdate = nzbdetails[n].text
        elif tag == 'link':
            if not nzburl or (nzburl and not lazylibrarian.CONFIG['PREFER_MAGNET']):
                nzburl = nzbdetails[n].text
        elif nzbdetails[n].attrib.get('name') == 'magneturl':
            nzburl = nzbdetails[n].attrib.get('value')
        elif nzbdetails[n].attrib.get('name') == 'size':
            nzbsize = nzbdetails[n].attrib.get('value')
        n += 1

    resultFields = {
        'bookid': book['bookid'],
        'nzbprov': host,
        'nzbtitle': nzbtitle,
        'nzburl': nzburl,
        'nzbdate': nzbdate,
        'nzbsize': nzbsize,
        'nzbmode': searchMode,
        'priority': priority
    }

    logger.debug('[NewzNabPlus] - result fields from NZB are ' + str(resultFields))
    return resultFields

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

import datetime
import os
import platform
import re
import shutil
import subprocess
import traceback
from hashlib import sha1

import lazylibrarian
import lib.zipfile as zipfile
from lazylibrarian import database, logger
from lazylibrarian.common import setperm
from lazylibrarian.formatter import getList, is_valid_booktype, plural, unaccented_str, makeUnicode, makeBytestr


def create_covers(refresh=False):
    if lazylibrarian.CONFIG['IMP_CONVERT'] == 'None':  # special flag to say "no covers required"
        logger.info('Cover creation is disabled in config')
        return
    myDB = database.DBConnection()
    #  <> '' ignores empty string or NULL
    issues = myDB.select("SELECT IssueFile from issues WHERE IssueFile <> ''")
    if refresh:
        logger.info("Creating covers for %s issue%s" % (len(issues), plural(len(issues))))
    else:
        logger.info("Checking covers for %s issue%s" % (len(issues), plural(len(issues))))
    cnt = 0
    for item in issues:
        try:
            create_cover(item['IssueFile'], refresh=refresh)
            cnt += 1
        except Exception as why:
            logger.debug('Unable to create cover for %s, %s %s' % (item['IssueFile'], type(why).__name__, str(why)))
    logger.info("Cover creation completed")
    if refresh:
        return "Created covers for %s issue%s" % (cnt, plural(cnt))
    return "Checked covers for %s issue%s" % (cnt, plural(cnt))


def create_cover(issuefile=None, refresh=False):
    if lazylibrarian.CONFIG['IMP_CONVERT'] == 'None':  # special flag to say "no covers required"
        return
    if issuefile is None or not os.path.isfile(issuefile):
        logger.debug('No issuefile %s' % issuefile)
        return

    base, extn = os.path.splitext(issuefile)
    if not extn:
        logger.debug('Unable to create cover for %s, no extension?' % issuefile)
        return

    coverfile = base + '.jpg'

    if os.path.isfile(coverfile):
        if refresh:
            os.remove(coverfile)
        else:
            logger.debug('Cover for %s exists' % issuefile)
            return  # quit if cover already exists and we didn't want to refresh

    logger.debug('Creating cover for %s' % issuefile)
    data = ''  # result from unzip or unrar
    extn = extn.lower()
    if extn in ['.cbz', '.epub']:
        try:
            data = zipfile.ZipFile(issuefile)
        except Exception as why:
            logger.debug("Failed to read zip file %s, %s %s" % (issuefile, type(why).__name__, str(why)))
            data = ''
    elif extn in ['.cbr']:
        try:
            # unrar will complain if the library isn't installed, needs to be compiled separately
            # see https://pypi.python.org/pypi/unrar/ for instructions
            # Download source from http://www.rarlab.com/rar_add.htm
            # note we need LIBRARY SOURCE not a binary package
            # make lib; sudo make install-lib; sudo ldconfig
            # lib.unrar should then be able to find libunrar.so
            from lib.unrar import rarfile
            data = rarfile.RarFile(issuefile)
        except Exception as why:
            logger.debug("Failed to read rar file %s, %s %s" % (issuefile, type(why).__name__, str(why)))
            data = ''
    if data:
        img = ''
        try:
            for member in data.namelist():
                memlow = member.lower()
                if '-00.' in memlow or '000.' in memlow or 'cover.' in memlow:
                    if memlow.endswith('.jpg') or memlow.endswith('.jpeg'):
                        img = data.read(member)
                        break
            if img:
                with open(coverfile, 'wb') as f:
                    f.write(img)
                return
            else:
                logger.debug("Failed to find image in %s" % issuefile)
        except Exception as why:
            logger.debug("Failed to extract image from %s, %s %s" % (issuefile, type(why).__name__, str(why)))

    elif extn == '.pdf':
        generator = ""
        if len(lazylibrarian.CONFIG['IMP_CONVERT']):  # allow external convert to override libraries
            generator = "external program: %s" % lazylibrarian.CONFIG['IMP_CONVERT']
            if "gsconvert.py" in lazylibrarian.CONFIG['IMP_CONVERT']:
                msg = "Use of gsconvert.py is deprecated, equivalent functionality is now built in. "
                msg += "Support for gsconvert.py may be removed in a future release. See wiki for details."
                logger.warn(msg)
            converter = lazylibrarian.CONFIG['IMP_CONVERT']
            postfix = ''
            # if not os.path.isfile(converter):  # full path given, or just program_name?
            #     converter = os.path.join(os.getcwd(), lazylibrarian.CONFIG['IMP_CONVERT'])
            if 'convert' in converter and 'gs' not in converter:
                # tell imagemagick to only convert first page
                postfix = '[0]'
            try:
                params = [converter, '%s%s' % (issuefile, postfix), '%s' % coverfile]
                res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                if res:
                    logger.debug('%s reports: %s' % (lazylibrarian.CONFIG['IMP_CONVERT'], res))
            except Exception as e:
                # logger.debug(params)
                logger.debug('External "convert" failed %s %s' % (type(e).__name__, str(e)))

        elif platform.system() == "Windows":
            GS = os.path.join(os.getcwd(), "gswin64c.exe")
            generator = "local gswin64c"
            if not os.path.isfile(GS):
                GS = os.path.join(os.getcwd(), "gswin32c.exe")
                generator = "local gswin32c"
            if not os.path.isfile(GS):
                params = ["where", "gswin64c"]
                try:
                    GS = subprocess.check_output(params, stderr=subprocess.STDOUT).strip()
                    generator = "gswin64c"
                except Exception as e:
                    logger.debug("where gswin64c failed: %s %s" % (type(e).__name__, str(e)))
            if not os.path.isfile(GS):
                params = ["where", "gswin32c"]
                try:
                    GS = subprocess.check_output(params, stderr=subprocess.STDOUT).strip()
                    generator = "gswin32c"
                except Exception as e:
                    logger.debug("where gswin32c failed: %s %s" % (type(e).__name__, str(e)))
            if not os.path.isfile(GS):
                logger.debug("No gswin found")
                generator = "(no windows ghostscript found)"
            else:
                # noinspection PyBroadException
                try:
                    params = [GS, "--version"]
                    res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                    logger.debug("Found %s [%s] version %s" % (generator, GS, res))
                    generator = "%s version %s" % (generator, res)
                    issuefile = issuefile.split('[')[0]
                    params = [GS, "-sDEVICE=jpeg", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dFirstPage=1", "-dLastPage=1",
                              "-dUseCropBox", "-sOutputFile=%s" % coverfile, issuefile]
                    res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                    if not os.path.isfile(coverfile):
                        logger.debug("Failed to create jpg: %s" % res)
                except Exception:  # as why:
                    logger.debug("Failed to create jpg for %s" % issuefile)
                    logger.debug('Exception in gswin create_cover: %s' % traceback.format_exc())
        else:  # not windows
            try:
                # noinspection PyUnresolvedReferences
                from wand.image import Image
                interface = "wand"
            except ImportError:
                try:
                    # No PythonMagick in python3
                    # noinspection PyUnresolvedReferences
                    import PythonMagick
                    interface = "pythonmagick"
                except ImportError:
                    interface = ""
            try:
                if interface == 'wand':
                    generator = "wand interface"
                    with Image(filename=issuefile + '[0]') as img:
                        img.save(filename=coverfile)

                elif interface == 'pythonmagick':
                    generator = "pythonmagick interface"
                    img = PythonMagick.Image()
                    # PythonMagick requires filenames to be str, not unicode
                    if type(issuefile) is unicode:
                        issuefile = unaccented_str(issuefile)
                    if type(coverfile) is unicode:
                        coverfile = unaccented_str(coverfile)
                    img.read(issuefile + '[0]')
                    img.write(coverfile)

                else:
                    GS = os.path.join(os.getcwd(), "gs")
                    generator = "local gs"
                    if not os.path.isfile(GS):
                        GS = ""
                        params = ["which", "gs"]
                        try:
                            GS = subprocess.check_output(params, stderr=subprocess.STDOUT).strip()
                            generator = GS
                        except Exception as e:
                            logger.debug("which gs failed: %s %s" % (type(e).__name__, str(e)))
                        if not os.path.isfile(GS):
                            logger.debug("Cannot find gs")
                            generator = "(no gs found)"
                        else:
                            params = [GS, "--version"]
                            res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                            logger.debug("Found gs [%s] version %s" % (GS, res))
                            generator = "%s version %s" % (generator, res)
                            issuefile = issuefile.split('[')[0]
                            params = [GS, "-sDEVICE=jpeg", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dFirstPage=1",
                                      "-dLastPage=1", "-dUseCropBox", "-sOutputFile=%s" % coverfile, issuefile]
                            res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                            if not os.path.isfile(coverfile):
                                logger.debug("Failed to create jpg: %s" % res)
            except Exception as e:
                logger.debug("Unable to create cover for %s using %s %s" % (issuefile, type(e).__name__, generator))
                logger.debug('Exception in create_cover: %s' % traceback.format_exc())

        if os.path.isfile(coverfile):
            setperm(coverfile)
            logger.debug("Created cover for %s using %s" % (issuefile, generator))
            return

    # if not recognised extension or cover creation failed
    try:
        shutil.copyfile(os.path.join(lazylibrarian.PROG_DIR, 'data/images/nocover.jpg'), coverfile)
        setperm(coverfile)
    except Exception as why:
        logger.debug("Failed to copy nocover file, %s %s" % (type(why).__name__, str(why)))
    return


def create_id(issuename=None):
    hashID = sha1(issuename).hexdigest()
    # logger.debug('Issue %s Hash: %s' % (issuename, hashID))
    return hashID


def magazineScan():
    lazylibrarian.MAG_UPDATE = 1
    # noinspection PyBroadException
    try:
        myDB = database.DBConnection()

        mag_path = lazylibrarian.CONFIG['MAG_DEST_FOLDER']
        mag_path = mag_path.split('$')[0]

        if lazylibrarian.CONFIG['MAG_RELATIVE']:
            if mag_path[0] not in '._':
                mag_path = '_' + mag_path
            mag_path = os.path.join(lazylibrarian.DIRECTORY('eBook'), mag_path)

        mag_path = mag_path.encode(lazylibrarian.SYS_ENCODING)
        if lazylibrarian.CONFIG['FULL_SCAN']:
            mags = myDB.select('select * from Issues')
            # check all the issues are still there, delete entry if not
            for mag in mags:
                title = mag['Title']
                issuedate = mag['IssueDate']
                issuefile = mag['IssueFile']

                if issuefile and not os.path.isfile(issuefile):
                    myDB.action('DELETE from Issues where issuefile=?', (issuefile,))
                    logger.info('Issue %s - %s deleted as not found on disk' % (title, issuedate))
                    controlValueDict = {"Title": title}
                    newValueDict = {
                        "LastAcquired": None,  # clear magazine dates
                        "IssueDate": None,  # we will fill them in again later
                        "LatestCover": None,
                        "IssueStatus": "Skipped"  # assume there are no issues now
                    }
                    myDB.upsert("magazines", newValueDict, controlValueDict)
                    logger.debug('Magazine %s details reset' % title)

            mags = myDB.select('SELECT * from magazines')
            # now check the magazine titles and delete any with no issues
            for mag in mags:
                title = mag['Title']
                count = myDB.select('SELECT COUNT(Title) as counter FROM issues WHERE Title=?', (title,))
                issues = count[0]['counter']
                if not issues:
                    logger.debug('Magazine %s deleted as no issues found' % title)
                    myDB.action('DELETE from magazines WHERE Title=?', (title,))

        logger.info(' Checking [%s] for magazines' % mag_path)

        matchString = ''
        for char in lazylibrarian.CONFIG['MAG_DEST_FILE']:
            matchString = matchString + '\\' + char
        # massage the MAG_DEST_FILE config parameter into something we can use
        # with regular expression matching
        booktypes = ''
        count = -1
        booktype_list = getList(lazylibrarian.CONFIG['MAG_TYPE'])
        for book_type in booktype_list:
            count += 1
            if count == 0:
                booktypes = book_type
            else:
                booktypes = booktypes + '|' + book_type
        match = matchString.replace("\\$\\I\\s\\s\\u\\e\\D\\a\\t\\e", "(?P<issuedate>.*?)").replace(
            "\\$\\T\\i\\t\\l\\e", "(?P<title>.*?)") + '\.[' + booktypes + ']'
        title_pattern = re.compile(match, re.VERBOSE)
        match = matchString.replace("\\$\\I\\s\\s\\u\\e\\D\\a\\t\\e", "(?P<issuedate>.*?)").replace(
            "\\$\\T\\i\\t\\l\\e", "") + '\.[' + booktypes + ']'
        date_pattern = re.compile(match, re.VERBOSE)

        # try to ensure startdir is str as os.walk can fail if it tries to convert a subdir or file
        # to utf-8 and fails (eg scandinavian characters in ascii 8bit)
        for rootdir, dirnames, filenames in os.walk(makeBytestr(mag_path)):
            rootdir = makeUnicode(rootdir)
            filenames = [makeUnicode(item) for item in filenames]            
            for fname in filenames:
                # maybe not all magazines will be pdf?
                if is_valid_booktype(fname, booktype='mag'):
                    issuedate = ''
                    # noinspection PyBroadException
                    try:
                        match = title_pattern.match(fname)
                        if match:
                            issuedate = match.group("issuedate")
                            title = match.group("title")
                            match = True
                        else:
                            match = False
                    except Exception:
                        match = False

                    if not match:
                        try:
                            match = date_pattern.match(fname)
                            if match:
                                issuedate = match.group("issuedate")
                                title = os.path.basename(rootdir)
                            else:
                                logger.debug("Pattern match failed for [%s]" % fname)
                                continue
                        except Exception as e:
                            logger.debug("Invalid name format for [%s] %s %s" % (fname, type(e).__name__, str(e)))
                            continue

                    logger.debug("Found %s Issue %s" % (title, fname))

                    issuefile = os.path.join(rootdir, fname)  # full path to issue.pdf
                    mtime = os.path.getmtime(issuefile)
                    iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))

                    controlValueDict = {"Title": title}

                    # is this magazine already in the database?
                    mag_entry = myDB.match(
                        'SELECT LastAcquired, IssueDate, MagazineAdded from magazines WHERE Title=?', (title,))
                    if not mag_entry:
                        # need to add a new magazine to the database
                        newValueDict = {
                            "Reject": None,
                            "Status": "Active",
                            "MagazineAdded": None,
                            "LastAcquired": None,
                            "LatestCover": None,
                            "IssueDate": None,
                            "IssueStatus": "Skipped",
                            "Regex": None
                        }
                        logger.debug("Adding magazine %s" % title)
                        myDB.upsert("magazines", newValueDict, controlValueDict)
                        magissuedate = None
                        magazineadded = None
                    else:
                        maglastacquired = mag_entry['LastAcquired']
                        magissuedate = mag_entry['IssueDate']
                        magazineadded = mag_entry['MagazineAdded']
                        magissuedate = str(magissuedate).zfill(4)

                    issuedate = str(issuedate).zfill(4)  # for sorting issue numbers

                    # is this issue already in the database?
                    controlValueDict = {"Title": title, "IssueDate": issuedate}
                    issue_id = create_id("%s %s" % (title, issuedate))
                    iss_entry = myDB.match('SELECT Title from issues WHERE Title=? and IssueDate=?',
                                           (title, issuedate))
                    if not iss_entry:
                        newValueDict = {
                            "IssueAcquired": iss_acquired,
                            "IssueID": issue_id,
                            "IssueFile": issuefile
                        }
                        myDB.upsert("Issues", newValueDict, controlValueDict)
                        logger.debug("Adding issue %s %s" % (title, issuedate))

                    create_cover(issuefile)
                    lazylibrarian.postprocess.processMAGOPF(issuefile, title, issuedate, issue_id)

                    # see if this issues date values are useful
                    controlValueDict = {"Title": title}
                    if not mag_entry:  # new magazine, this is the only issue
                        newValueDict = {
                            "MagazineAdded": iss_acquired,
                            "LastAcquired": iss_acquired,
                            "LatestCover": os.path.splitext(issuefile)[0] + '.jpg',
                            "IssueDate": issuedate,
                            "IssueStatus": "Open"
                        }
                        myDB.upsert("magazines", newValueDict, controlValueDict)
                    else:
                        # Set magazine_issuedate to issuedate of most recent issue we have
                        # Set latestcover to most recent issue cover
                        # Set magazine_added to acquired date of earliest issue we have
                        # Set magazine_lastacquired to acquired date of most recent issue we have
                        # acquired dates are read from magazine file timestamps
                        newValueDict = {"IssueStatus": "Open"}
                        if not magazineadded or iss_acquired < magazineadded:
                            newValueDict["MagazineAdded"] = iss_acquired
                        if not maglastacquired or iss_acquired > maglastacquired:
                            newValueDict["LastAcquired"] = iss_acquired
                        if not magissuedate or issuedate >= magissuedate:
                            newValueDict["IssueDate"] = issuedate
                            newValueDict["LatestCover"] = os.path.splitext(issuefile)[0] + '.jpg'
                        myDB.upsert("magazines", newValueDict, controlValueDict)

        magcount = myDB.match("select count(*) from magazines")
        isscount = myDB.match("select count(*) from issues")

        logger.info("Magazine scan complete, found %s magazine%s, %s issue%s" %
                    (magcount['count(*)'], plural(magcount['count(*)']),
                     isscount['count(*)'], plural(isscount['count(*)'])))
        lazylibrarian.MAG_UPDATE = 0

    except Exception:
        lazylibrarian.MAG_UPDATE = 0
        logger.error('Unhandled exception in magazineScan: %s' % traceback.format_exc())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import json
import urlparse
import tldextract
import time
import cherrypy
import rocksdb
from collections import defaultdict


def split_uri(uri, encoding='idna'):
    parsed_uri = urlparse.urlparse(uri)

    if not parsed_uri.netloc:
        parsed_uri = urlparse.urlparse("http://%s" + uri)
    netloc = parsed_uri.netloc
    assert netloc, "Cannot parse uri:%s\n" % uri
    extracted = tldextract.extract(netloc)
    # tld = .domain.encode(encoding)
    # suffix = tldextract.extract(netloc).suffix
    path = "%s" % (parsed_uri.path)
    if parsed_uri.query:
        path = "%s?%s" % (path, parsed_uri.query)
    return extracted.domain.encode(encoding), extracted.suffix, path


def json_error(status, message, traceback, version):
    err = {"status": status, "message": message,
           "traceback": traceback, "version": version}
    return json.dumps(err, sort_keys=True, indent=4)


class DBInterface(object):

    def __init__(self, db_directories, pretty=False, verbose=0,
                 max_results=10000):

        self.dbs = {}

        for db_directory in db_directories:
            opts = rocksdb.Options()
            opts.create_if_missing = False
            opts.max_open_files = 1000
            opts.num_levels = 6
            db = rocksdb.DB(db_directory, opts, read_only=True)
            it = db.iterkeys()
            it.seek_to_first()
            key = it.next()
            tld, url, crawl = key.split(" ", 2)
            assert crawl not in self.dbs, "Multiple dbs for %s\n" % crawl
            sys.stderr.write("DB at %s holds crawl %s\n" %
                             (db_directory, crawl))
            self.dbs[crawl] = db

        self.pretty = pretty
        self.verbose = verbose
        self.max_results = max_results

    def _dump_json(self, data, pretty=False):
        if self.pretty or pretty:
            return json.dumps(data, indent=2) + "\n"
        return json.dumps(data) + "\n"

    @cherrypy.expose
    def crawls(self, **kwargs):
        cherrypy.response.headers['Content-Type'] = 'application/json'
        pretty = kwargs.get("pretty", 0) > 0
        result = {"crawls": sorted(self.dbs.keys())}
        return self._dump_json(result, pretty)

    @cherrypy.expose
    def query_domain(self, **kwargs):
        cherrypy.response.headers['Content-Type'] = 'application/json'
        start_time = time.time()
        domain = kwargs["domain"]
        send_data = kwargs.get("full", 0) > 0
        query_crawl = kwargs.get("crawl", "")
        pretty = kwargs.get("pretty", 0) > 0
        max_results = int(kwargs.get("max_results", self.max_results))

        if not domain.startswith("http://"):
            domain = "http://%s" % domain
        # if domain.startswith("https://"):
        #     domain = "http://%s" % domain[8:]

        query_domain, query_suffix, query_path = split_uri(domain)

        result = {"query_domain": query_domain,
                  "query_crawl": query_crawl,
                  "query_path": query_path}

        uri2crawl = defaultdict(list)
        n_results = 0
        n_skipped = 0
        # print "query:", "%s%s " % (self.keyprefix, query_domain)

        relevant_crawls = [query_crawl]
        if not query_crawl:
            relevant_crawls = self.dbs.keys()
        else:
            assert query_crawl in self.dbs.keys()

        db_key = "%s %s" % (query_domain, domain)
        result["db_key"] = db_key
        result["skipped_keys"] = []

        for db_crawl in relevant_crawls:
            db = self.dbs[db_crawl]
            it = db.iteritems()
            it.seek(db_key)
            for key, value in it:
                n_skipped += 1
                tld, uri, crawl = key.split(" ", 2)
                if 'exact' in kwargs and uri != domain:
                    break
                assert crawl == db_crawl
                if query_domain != tld:  # went too far
                    break
                suffix, path = split_uri(uri)[1:]
                if query_suffix and query_suffix != suffix:
                    result["skipped_keys"].append(key)
                    continue
                if query_path and not path.startswith(query_path):
                    result["skipped_keys"].append(key)
                    continue
                n_results += 1
                if n_results > max_results:
                    break
                data = json.loads(value)
                uri2crawl[uri].append((crawl, data))

        result["unique_urls"] = uri2crawl.keys()
        if send_data:
            result["data"] = uri2crawl

        if "verbose" in kwargs:
            result["time"] = "%.2fs" % (time.time() - start_time)
            result["skipped"] = n_skipped - n_results
        return self._dump_json(result, pretty)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-ip',
                        help='server ip to bind to, default: localhost',
                        default="127.0.0.1")
    parser.add_argument('-port',
                        help='server port to bind to, default: 8080',
                        type=int,
                        default=8080)
    parser.add_argument('-nthreads',
                        help='number of server threads, default: 8',
                        type=int,
                        default=8)
    parser.add_argument('-maxresults',
                        help='maximum number of return results.',
                        type=int,
                        default=10000)
    parser.add_argument('-pretty',
                        action='store_true',
                        help='pretty print json')
    parser.add_argument('-logprefix',
                        help='logfile prefix, default: write to stderr')
    parser.add_argument('-verbose',
                        help='verbosity level, default: 0',
                        type=int,
                        default=0)
    parser.add_argument('db', nargs='+', help='leveldb root directories')
    # parser.add_argument('url', help='url to search for')

    args = parser.parse_args(sys.argv[1:])

    cherrypy.config.update({'server.request_queue_size': 1000,
                            'server.socket_port': args.port,
                            'server.thread_pool': args.nthreads,
                            'server.socket_host': args.ip})
    cherrypy.config.update({'error_page.default': json_error})
    cherrypy.config.update({'log.screen': True})
    if args.logprefix:
        cherrypy.config.update({'log.access_file': "%s.access.log"
                                % args.logprefix,
                                'log.error_file': "%s.error.log"
                                % args.logprefix})
    cherrypy.quickstart(DBInterface(args.db,
                                    pretty=args.pretty,
                                    verbose=args.verbose,
                                    max_results=args.maxresults))

# legacy-webcache

A small application for adding a caching layer over a legacy web service, and a suite of apache configurations and mod_wsgi scripts for evaluating caching properties.

## Webcache

`webcache/webcache.py` is a wsgi application for rewriting external requests to
check a cache for the URL, and re-requesting the URL locally, on the 127.0.0.1 address, if not present.

Able to appropriately overwrite and set Last-Modified headers to reflect
actual changes in server content.

Intended for use in grotesquely legacy applications, where modifying
the headers is not an option, and workarounds with apache
configuration directives are insufficiently expressive. (Niche.)

### Motivation

If a URL's corresponding content is in our cache, then we can potentially
resolve the request much faster than the origin would.

Otherwise, if an externally requested URL can't be found, we open a request
from a localhost address and re-request the same URL, with little additional
overhead.

Once a URL is in the cache or passing through the webcache, we can also issue
the kind of headers we might want for this request; caching headers like Last-
Modified and Expire have limitations in Apache that respect the origin
application's intent. If the origin's logic is insufficient to track and set
these headers properly, adding a small amount of logic above the application can
enable more efficient server- side and client-side caching.

### Request Flow, Before and After

An overall map of the request flow might have originally looked like this:

    external requests   -->   | apache |   -->   legacy application

With the addition of this caching app, we have something like this:

    external requests   -->   | apache |
                                 *non-cacheable requests   -->   legacy application
                                 *cacheable requests       -->   caching application

    caching application    -->    | cache |
                                     *present in cache       -->   fulfill from cache
                                     *not present in cache   -->   fulfill with internal server request, repopulate cache

    internal requests   -->   | apache |   -->   legacy application

where the caching application checks memcached for the requested URL,
and issues internal requests for any URLs that cannot be located.

### Apache Setup for Webcache

In an apache site configuration or .htaccess file, a rewrite condition
and rule that matches an external request and forwards it to our
caching app will look like this:

    RewriteCond "%{REMOTE_ADDR}" "!=127.0.0.1"
    RewriteRule "(<cache-url-match>)" "/<cache-base-path>/$1"

 * The RewriteCond applies the rule only if the remote address is not local
(127.0.0.1).
 * The RewriteRule matches the path of the requested, cacheable URL, and places
it on top of a known, fixed path. This rewrites the request to direct it to the
application, and allows the application to examine the requested URL.

In an apache virtualhost entry, our application must be mounted with the same
`<cache-base-path>` as given in the RewriteRule (example above). Fragments
from a host configuration that maps this `<cache-base-path>` to this wsgi app
look like this:

    <VirtualHost myhost:80>
        ...
        WSGIDaemonProcess webcache_wsgi

        WSGIProcessGroup webcache_wsgi
        WSGIApplicationGroup %{GLOBAL}

        WSGIScriptAlias /<cache-base-path> /usr/local/www/webcache/webcache.wsgi

        <Directory /usr/local/www/webcache>
        <IfVersion < 2.4>
                   Order allow,deny
                   Allow from all
        </IfVersion>
        <IfVersion >= 2.4>
                   Require all granted
        </IfVersion>
        </Directory>
        ...
    </VirtualHost>

Note that this assumes the 127.0.0.1 address is privileged: accesses to
`/<cache-base-path>` from the localhost creates an infinite loop, if the caching
application has to issue a request for the resource requested.

### Application Logic and Cache Structure
In our cache layout, each URL corresponds to a key that references a metadata entry, and a chained reference to the content body.

    URL key   -->   | metadata entry |   -->    | content entry |

The metadata itself will contain:

 * url: the URL the metadata is about
 * fetched: when we last fetched the corresponding body, in utc
    unixtime
 * last_modified: when we noticed the resource as being last modified
 * content_key: the cache key for the current body, if valid
 * sha256_digest: the sha256 digest of the current body. None if not valid.

The existance of a metadata entry tells the application something about the
current state of a URL in the webcache. These particular fields will allow the
application to determine if the content is present, if the cache entry has
expired, if it's changed, or whether a client request can be quickly resolved
with a 304.

In addition, the metadata needs a few fields to govern consistency logic for
cached entries:

 * reservation: the count of the reservation when this metadata was last updated. This field begins at 1, and is incremented once by each thread trying to update the metadata in some way
 * session: the unixtime (including microseconds) of when the metadata
    was created. Together with the reservation count, this pair forms a unique token
    across threads for each metadata entry on creation, and upon update
 * last_noted: the value of the reservation field when the metadata was last
    successfully updated with a valid server response. Begins at 0
 * valid: a flag indicating whether the entry is a reservation (a placeholder for a
    thread currently making a request to the server) or an entry that holds content.

The contents will contain:

 * url: the url the content is about
 * status: the status code and response message from the origin
 * headers: the headers that this app will return, drawn from the
    origin or application logic
 * content: the body itself.

With this layout, the metadata and content separation will:

a] let us check if the client needs to be served any content without
pulling the content out of memory (304 Not Modified); if the entry has not
expired (an internal concept of how frequently the application needs to
check the server's output), then the Last-Modified date in the metadata
can be compared against a request's If-Modified-Since header.

b] let us check if the current server contents differs from our cached content,
and accumulate information that will preserve efficiency if it doesn't (the
fetched time will be updated, and the last_modified time won't be), and if it
does (we won't have to pull the cache entry to know this). We only incur a minor
penalty for examining the metadata for a body, if the body turns out not to be
present.

Coordinating across threads with memcached presents several challenges in
formulating a valid approach to consistency.

On the one hand, memcached is simple to set up, performs well, has a mature
client for python, and provides support for atomic operations.

But entries can be evicted from memcached with no ability to specify certain
entries as having priority; while the LRU algorithm will perform well in most
cases, the potential outcomes need to be considered.

The following consistency logic tries to balance handling these difficult cases
with performance, without assuming that all threads will make progress.

Scenarios:

1. Valid Entry

    An entry can be used to serve a response if the metadata is present in the
    cache, if it has the valid flag set, if the fetched time doesn't indicate an
    expired entry, and either the last-modified time indicates a not-modified
    condition or the metadata's content body is present in the cachen.

    The request is served from cache, and the webcache is done with this request.

2. Invalid Entry:

    An entry is invalid if any one of the "valid" criteria are not met: if the
    metadata or body are not present in the cache, the metadata is expired, or
    the valid flag isn't set.

    In this case, a thread will compete to update the cache by incrementing
    the reservation field and updating the metadata entry, if it exists in any
    form in the cache, or inserting a new one (with the "valid" field unset),
    if it doesn't.

    If a thread wins the update contest, by inserting a new metadata entry or
    by updating an existing metadata entry with the reservation field
    incremented, then it immediately issues a request to the server for new
    content. If a thread doesn't win, then it has updated a metadata entry
    with a reservation field such that the value of "reservation" exceeds
    the value of "last_noted" by more than one. It then waits for some period
    of time governed by (reservation - last_noted) before making any request to
    the server.

    Once a thread has made a request to the server and has its updated content,
    it updates the cache until either the cache's content is valid, or it
    reflects what the thread has written. To update the response's content in
    cache, a thread writes its content body into an entry keyed by the URL,
    session, and reservation. This keying ensures that the metadata entry always
    holds the correct key for its content entry. Then it attempts to update or
    add the metadata entry.

    (Because a thread's reservation is taken from a single, successful
    initialization or update to a metadata entry, it is unique to lifetime of
    that metadata entry. To guard against the case where another thread has
    the the same reservation, but drawn from a different instance of the
    metadata entry, the session defined as the system clock when the metadata
    is first successfully inserted into the cache. This overlapping
    reservation case could occur if the metadata entry is evicted, and a later
    thread gets the same reservation.)

    Once the cache is updated, or an updated entry is retrieved, it is used to
    issue a fresh response.

## Setup and Mockout Resources
The folders `apache_confs` and `mockout_wsgis` contain a suite of barebones mod_wsgi scripts and apache configurations for:

 * mocking out server headers and page content
 * modifying server and client headers from the web server
 * testing server and client caching properties

### Apache

Setup details are system-dependent and vary wildly; on a debian
system, the `www-data` user had read, write, and execute permissions
to the cache and script directories, and read permissions on all
directories upwards.

Apache configurations should be enabled as the primary site. Mods
enabled should include:

 * headers
 * mod_cache
 * mod_disk_cache

#### disk_cache_access.conf

The apache configuration drops/overwrites headers under /cacheme to
make the page cacheable from within apache, and uses a disk cache
sourced in /usr/local/www/cache. Disk has one level, with an expire
time of 60 seconds.

All other locations return the mod_wsgi script's contents without any modifications.

#### mem_cache_access.conf

Contains a setup for using socache/memcache.

#### webcache_site.conf

Example configuration for using the webcache. See the docs in
src/webcache.wsgi for more information about how the application works
and how requests should be routed.

### mod_wsgi mockouts

Wsgi scripts are sourced from `/usr/local/www/test_redirect/`. For
setups with one script, scripts are named `test_wsgi.wsgi`. For two
scripts, the name `test_wsgi2.wsgi` is used.

#### mock_headers.wsgi

The mod_wsgi script issues timestamped content (text is the unixtime)
with headers that make it difficult for the client and server to cache
the content.

#### linked_content_mockout.wsgi

Similar mod_wsgi script to mock_headers.wsgi, but embeds a (hopefully)
cacheable resource within an html page, and contains a link to /b for
a convenient way of testing browser-side caching (browsers issue
no-cache messages on refresh, but not when a link is followed).

Should use same apache configuration as in `basic_headers_rewrite`.

#### linked_content_options.wsgi

Commented-out options for setting last-modified and expires according
to experiments with socache for in-memory caching.

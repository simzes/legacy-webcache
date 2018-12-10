# legacy-webcache

A suite of barebones mod_wsgi scripts and apache configurations for:
 * adding a caching layer over a legacy web service
 * mocking out server headers and page content
 * modifying server and client headers from the web server
 * testing server and client caching properties

## Setup
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

### Webcache

The legacy-webcache application is contained in
src/webcache.wsgi. When external requests to a cacheable resource are
rewritten to the webcache, it adds a low-profile caching layer above
the application. Requests for original content are reissued on the
127.0.0.1 address, with the originally requested URL.

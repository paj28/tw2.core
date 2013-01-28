import types
import warnings
import webob as wo
from pkg_resources import iter_entry_points, DistributionNotFound
from paste.deploy.converters import asbool, asint

import core
import resources

import logging
log = logging.getLogger(__name__)


class Config(object):
    '''
    ToscaWidgets Configuration Set

    `translator`
        The translator function to use. (default: no-op)

    `default_engine`
        The main template engine in use by the application. Widgets with no
        parent will display correctly inside this template engine. Other
        engines may require passing displays_on to :meth:`Widget.display`.
        (default:string)

    `inject_resoures`
        Whether to inject resource links in output pages. (default: True)

    `inject_resources_location`
        A location where the resources should be injected. (default: head)

    `serve_resources`
        Whether to serve static resources. (default: True)

    `res_prefix`
        The prefix under which static resources are served. This must start
        and end with a slash. (default: /resources/)

    `res_max_age`
        The maximum time a cache can hold the resource. This is used to
        generate a Cache-control header. (default: 3600)

    `root_controller`
        The root widget, that will be rendered when the root URL is accessed.
        (default: None)
    
    `controller_prefix`
        The prefix under which controllers are served. This must start
        and end with a slash. (default: /controllers/)

    `bufsize`
        Buffer size used by static resource server. (default: 4096)

    `params_as_vars`
        Whether to present parameters as variables in widget templates. This
        is the behaviour from ToscaWidgets 0.9. (default: False)

    `debug`
        Whether the app is running in development or production mode.
        (default: True)

    `validator_msgs`
        A dictionary that maps validation message names to messages. This lets
        you override validation messages on a global basis. (default: {})

    `encoding`
        The encoding to decode when performing validation (default: utf-8)

    `auto_reload_templates`
        Whether to automatically reload changed templates. Set this to False in
        production for efficiency. If this is None, it takes the same value as
        debug. (default: None)

    `preferred_rendering_engines`
        List of rendering engines in order of preference.
        (default: ['mako','genshi','jinja','kajiki'])

    `strict_engine_selection`
        If set to true, TW2 will only select rendering engines from within your
        preferred_rendering_engines, otherwise, it will try the default list if
        it does not find a template within your preferred list. (default: True)

    `rendering_engine_lookup`
        A dictionary of file extensions you expect to use for each type of
        template engine.
        (default: {
            'mako':['mak', 'mako'],
            'genshi':['genshi', 'html'],
            'jinja':['jinja', 'html'],
            'kajiki':['kajiki', 'html'],
        })

    `script_name`
        A name to prepend to the url for all resource links (different from
        res_prefix, as it may be shared across and entire wsgi app.
        (default: '')
        
    `unauth_response`
        The response to return if access to a controller method is not 
        authorized.
        (default: wo.Response(status="401 Unauthorized"))
    '''

    translator = lambda self, s: s
    default_engine = 'string'
    inject_resources_location = 'head'
    inject_resources = True
    serve_resources = True
    res_prefix = '/resources/'
    res_max_age = 3600
    controller_prefix = '/controllers/'
    bufsize = 4 * 1024
    params_as_vars = False
    debug = True
    validator_msgs = {}
    encoding = 'utf-8'
    auto_reload_templates = None
    preferred_rendering_engines = ['mako', 'genshi', 'jinja', 'kajiki']
    strict_engine_selection = True
    rendering_extension_lookup = {
        'mako': ['mak', 'mako'],
        'genshi': ['genshi', 'html'],
        'genshi_abs': ['genshi', 'html'], # just for backwards compatibility with tw2 2.0.0
        'jinja':['jinja', 'html'],
        'kajiki':['kajiki', 'html'],
        'chameleon': ['pt']
    }
    script_name = ''
    unauth_response = wo.Response(status="401 Unauthorized")
    root_controller = None

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

        # Set boolean properties
        boolean_props = (
            'inject_resources',
            'serve_resources',
            'params_as_vars',
            'strict_engine_selection',
            'debug',
        )
        for prop in boolean_props:
            setattr(self, prop, asbool(getattr(self, prop)))

        # Set integer properties
        for prop in ('res_max_age', 'bufsize'):
            setattr(self, prop, asint(getattr(self, prop)))

        if self.auto_reload_templates is None:
            self.auto_reload_templates = self.debug


class TwMiddleware(object):
    """ToscaWidgets middleware

    This performs three tasks:
     * Clear request-local storage before and after each request. At the start
       of a request, a reference to the middleware instance is stored in
       request-local storage.
     * Proxy resource requests to ResourcesApp
     * Inject resources
    """
    def __init__(self, app, controllers=None, **config):
        self.app = app
        self.config = Config(**config)
        self.resources = resources.ResourcesApp(self.config)

        rl = core.request_local()

        # Load up resources that wanted to be registered before we were ready
        for modname, filename, whole_dir in rl.get('queued_resources', []):
            self.resources.register(modname, filename, whole_dir)

        rl['queued_resources'] = []

    def __call__(self, environ, start_response):
        rl = core.request_local()
        rl.clear()
        rl['middleware'] = self
        req = wo.Request(environ)

        path = req.path_info
        if self.config.serve_resources and \
           path.startswith(self.config.res_prefix):
            return self.resources(environ, start_response)
        else:
            if path.startswith(self.config.controller_prefix) and self.config.root_controller:
                path = req.path_info[len(self.config.controller_prefix):]
                parts = path and path.split('_')
                resp = self.config.root_controller.proc_url(req, parts)
            else:
                if self.app:
                    resp = req.get_response(self.app, catch_exc_info=True)
                else:
                    resp = wo.Response(status="404 Not Found")

            ct = resp.headers.get('Content-Type', 'text/plain').lower()

            should_inject = (
                self.config.inject_resources
                and 'html' in ct
                and not isinstance(resp.app_iter, types.GeneratorType)
            )
            if should_inject:
                body = resources.inject_resources(
                    resp.body,
                    encoding=resp.charset,
                )
                if isinstance(body, unicode):
                    resp.unicode_body = body
                else:
                    resp.body = body
        core.request_local().clear()
        return resp(environ, start_response)


def register_resource(modname, filename, whole_dir):
    """ API function for registering resources *for serving*.

    This should not be confused with resource registration for *injection*.
    A resource must be registered for serving for it to be also registered for
    injection.

    If the middleware is available, the resource is directly registered with
    the ResourcesApp.

    If the middleware is not available, the resource is stored in the
    request_local dict.  When the middleware is later initialized, those
    waiting registrations are processed.
    """

    rl = core.request_local()
    mw = rl.get('middleware')
    if mw:
        mw.resources.register(modname, filename, whole_dir)
    else:
        rl['queued_resources'] = rl.get('queued_resources', []) + [
            (modname, filename, whole_dir)
        ]
        log.debug("No middleware in place.  Queued %r->%r(%r) registration." %
                  (modname, filename, whole_dir))


def make_middleware(app=None, config=None, **kw):
    config = (config or {}).copy()
    config.update(kw)
    app = TwMiddleware(app, **config)
    return app


def dev_server(*args, **kwargs):
    """
    Deprecated; use tw2.devtools.dev_server insteads.
    """
    import tw2.devtools
    warnings.warn(
        'tw2.core.dev_server is deprecated; ' +
        'Use tw2.devtools.dev_server instead.'
    )
    tw2.devtools.dev_server(*args, **kwargs)
